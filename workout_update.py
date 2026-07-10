"""
Safe, surgical updater for saved Speediance custom workout templates.

Operating model:
    update request -> exact workout lookup -> validation -> original-workout
    backup -> normalized write plan -> dry-run by default -> explicitly
    enabled write -> fetch-back verification -> sanitized evidence

Write interlocks (BOTH are required for a live write):
    1. --apply flag on the command line
    2. environment variable SPEEDIANCE_WRITE_ENABLED=true

IMPORTANT: "capacity" here is the Speediance resistance/weight value stored in
the per-set `weights` CSV of a template action. It is NOT the repetition count
(`setsAndReps`). Changing --capacity never touches reps, mode, rest or order.

Example (dry-run):
    python workout_update.py --title "I-BK-v8" --new-title "I-BK-v9" ^
        --exercise-id 321 --expected-capacity 49 --capacity 51

This module only modifies the saved custom workout TEMPLATE
(/api/app/v2/customTrainingTemplate). It never touches workout history,
completed training records, or the exercise library.
"""
import argparse
import copy
import hashlib
import json
import os
import sys
import time

WRITE_ENABLE_ENV = "SPEEDIANCE_WRITE_ENABLED"
PLAN_SCHEMA_VERSION = "speediance-workout-write-plan/v1"
BACKUP_SCHEMA_VERSION = "speediance-workout-backup/v1"
DEFAULT_RUNTIME_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "runtime", "speediance-write-plans"
)

# Per-set / per-action fields that must round-trip unchanged (except `weights`
# on the targeted exercise). Used both to build the write payload and for the
# canonical fetch-back comparison.
CANONICAL_ACTION_FIELDS = (
    "groupId",
    "actionLibraryId",
    "templatePresetId",
    "setsAndReps",       # repetitions (or seconds for timed moves) - never changed here
    "breakTime2",        # rest
    "sportMode",         # mode
    "leftRight",
    "selectCompletionMethod",
    "counterweight2",
    "level",
    "weights",           # the capacity/weight field - the ONLY per-set field we change
)

PRESERVED_PROPERTIES = [
    "repetitions",
    "mode",
    "rest",
    "exercise_order",
    "other_exercises",
]


class WorkoutUpdateError(Exception):
    """Raised on any validation/safety-stop condition. No write happens after this."""


# ── small helpers ────────────────────────────────────────────────

def _split_csv(value):
    s = "" if value is None else str(value)
    return s.split(",") if s != "" else []


def _csv_str(value):
    return "" if value is None else str(value)


def _format_weight_like(old_token, new_value):
    """Format new_value in the same style as the token it replaces ("49" -> "51",
    "49.0" -> "51.0") so untouched formatting conventions survive the write."""
    old_token = old_token.strip()
    if "." in old_token:
        decimals = len(old_token.split(".", 1)[1])
        return f"{float(new_value):.{decimals}f}"
    if float(new_value).is_integer():
        return str(int(new_value))
    return repr(float(new_value))


def sanitized_hash(detail):
    """Stable sha256 of the full template detail, for the write plan."""
    canonical = json.dumps(detail, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _redact_user_ids(obj):
    """Recursively redact account-identifier fields from a payload copy."""
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if "userid" in k.lower() else _redact_user_ids(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_user_ids(v) for v in obj]
    return obj


def _sorted_actions(detail):
    actions = detail.get("actionLibraryList") or []
    return sorted(actions, key=lambda a: a.get("sort", 0))


# ── lookup & validation ──────────────────────────────────────────

def find_workout_by_exact_title(workouts, title):
    """Exact (==) title match only. Fuzzy matching is deliberately unsupported."""
    matches = [w for w in workouts if w.get("name") == title]
    if len(matches) == 0:
        raise WorkoutUpdateError(f"No saved workout has the exact title {title!r}.")
    if len(matches) > 1:
        raise WorkoutUpdateError(
            f"{len(matches)} saved workouts share the exact title {title!r}; refusing to guess."
        )
    return matches[0]


def ensure_target_title_available(workouts, new_title, source_id):
    clashes = [
        w for w in workouts
        if w.get("name") == new_title and w.get("id") != source_id
    ]
    if clashes:
        raise WorkoutUpdateError(
            f"A different saved workout (id={clashes[0].get('id')}) already uses the title {new_title!r}."
        )


def find_target_action(detail, exercise_id):
    """Exactly one action in the template must match the exercise (group) ID."""
    actions = _sorted_actions(detail)
    matches = [
        (idx, a) for idx, a in enumerate(actions)
        if str(a.get("groupId")) == str(exercise_id)
    ]
    if len(matches) == 0:
        raise WorkoutUpdateError(
            f"Exercise ID {exercise_id} not found in workout {detail.get('name')!r}."
        )
    if len(matches) > 1:
        raise WorkoutUpdateError(
            f"Exercise ID {exercise_id} appears {len(matches)} times in workout "
            f"{detail.get('name')!r}; refusing an ambiguous update."
        )
    return matches[0]


# ── plan ─────────────────────────────────────────────────────────

def build_update_plan(detail, exercise_id, expected_capacity, new_capacity, new_title):
    """Validates the requested change and returns the internal plan dict.

    Only sets whose current weight equals expected_capacity EXACTLY are changed.
    Mixed-load sets with other values are left untouched.
    """
    action_index, action = find_target_action(detail, exercise_id)

    weights = _split_csv(action.get("weights"))
    reps = _split_csv(action.get("setsAndReps"))
    if not weights or len(weights) != len(reps):
        raise WorkoutUpdateError(
            f"Schema mismatch on exercise {exercise_id}: weights CSV has {len(weights)} "
            f"entries but setsAndReps has {len(reps)}."
        )

    changed_indices = [
        i for i, tok in enumerate(weights)
        if float(tok) == float(expected_capacity)
    ]
    if not changed_indices:
        raise WorkoutUpdateError(
            f"No set on exercise {exercise_id} has capacity/weight exactly "
            f"{expected_capacity} (current: {action.get('weights')!r})."
        )

    new_weights = list(weights)
    for i in changed_indices:
        new_weights[i] = _format_weight_like(weights[i], new_capacity)

    # Capacity totals: shift by the delta only, preserving whatever baseline the
    # server/app previously computed for untouched sets and exercises.
    delta = sum(
        int(float(reps[i])) * (float(new_capacity) - float(expected_capacity))
        for i in changed_indices
    )
    old_action_capacity = float(action.get("capacity") or 0.0)
    old_total_capacity = float(detail.get("totalCapacity") or 0.0)

    return {
        "workout_id": detail.get("id"),
        "workout_code": detail.get("code"),
        "source_title": detail.get("name"),
        "target_title": new_title,
        "exercise_id": exercise_id,
        "exercise_title": action.get("title"),
        "action_index": action_index,
        "expected_capacity": expected_capacity,
        "new_capacity": new_capacity,
        "old_weights_csv": ",".join(weights),
        "new_weights_csv": ",".join(new_weights),
        "sets_changed": [i + 1 for i in changed_indices],  # 1-based for review
        "new_action_capacity": old_action_capacity + delta,
        "new_total_capacity": old_total_capacity + delta,
        "sanitized_hash": sanitized_hash(detail),
    }


def plan_artifact(plan, apply_requested):
    """The minimal, sanitized on-disk write plan (schema: PLAN_SCHEMA_VERSION)."""
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "operation": "update_saved_workout",
        "source_workout": {
            "id": plan["workout_id"],
            "title": plan["source_title"],
            "sanitized_hash": plan["sanitized_hash"],
        },
        "target_title": plan["target_title"],
        "changes": [
            {
                "exercise_id": str(plan["exercise_id"]),
                "exercise_title": plan["exercise_title"],
                "field": "weights",
                "from": plan["expected_capacity"],
                "to": plan["new_capacity"],
                "sets_changed": plan["sets_changed"],
                "weights_before": plan["old_weights_csv"],
                "weights_after": plan["new_weights_csv"],
            }
        ],
        "preserved": list(PRESERVED_PROPERTIES),
        "apply_requested": bool(apply_requested),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


# ── payload construction (mirrors SpeedianceClient.save_workout contract) ──

def _infer_set_unit(action):
    """Mirror of the UI's getSetGoalUnit() (templates/create.html): decide whether
    an exercise is time-based or rep-based from library-level completion fields."""
    cm = action.get("completionMethod")
    scm = _split_csv(action.get("selectCompletionMethod"))
    first = scm[0] if scm else ""
    if cm == 0 and first == "1":
        return "sec"
    if cm == 2:
        return "sec"
    return "reps"


def build_save_payload(detail, device_type, name,
                       action_index=None, new_weights_csv=None,
                       new_action_capacity=None, new_total_capacity=None):
    """Builds the POST /api/app/v2/customTrainingTemplate payload by echoing the
    fetched template detail verbatim, changing only the name and (optionally) the
    weights/capacity of one action. With action_index=None this reproduces the
    original workout, which is exactly the rollback/restore payload.
    """
    action_list = []
    for idx, a in enumerate(_sorted_actions(detail)):
        weights_csv = _csv_str(a.get("weights"))
        capacity = a.get("capacity")
        if action_index is not None and idx == action_index:
            weights_csv = new_weights_csv
            capacity = new_action_capacity

        n_sets = len(_split_csv(a.get("setsAndReps")))
        cm = "2" if _infer_set_unit(a) == "sec" else "1"
        breaks = _csv_str(a.get("breakTime2"))
        counter = _csv_str(a.get("counterweight2"))

        action_list.append({
            "groupId": int(a["groupId"]),
            "actionLibraryId": int(a["actionLibraryId"]),
            "templatePresetId": int(a.get("templatePresetId", -1)),
            "setsAndReps": _csv_str(a.get("setsAndReps")),
            "breakTime": breaks,
            "breakTime2": breaks,
            "sportMode": _csv_str(a.get("sportMode")),
            "leftRight": _csv_str(a.get("leftRight")),
            "selectCompletionMethod": _csv_str(a.get("selectCompletionMethod")) or ",".join(["1"] * n_sets),
            "completionMethod": ",".join([cm] * n_sets),
            "countType": ",".join([cm] * n_sets),
            "weights": weights_csv,
            "counterweight2": counter,
            "counterweight": counter,
            "level": _csv_str(a.get("level")) or ",".join(["0"] * n_sets),
            "capacity": capacity,
        })

    total = new_total_capacity if new_total_capacity is not None else detail.get("totalCapacity")
    return {
        "id": int(detail["id"]),
        "name": name,
        "actionLibraryList": action_list,
        "totalCapacity": total,
        "deviceType": device_type,
        "bgColor": detail.get("bgColor", 0),
    }


# ── canonical fetch-back comparison ──────────────────────────────

def canonicalize_detail(detail):
    """Reduce a template detail to the fields that must be preserved, in order."""
    return {
        "name": detail.get("name"),
        "actions": [
            {f: _csv_str(a.get(f)) for f in CANONICAL_ACTION_FIELDS}
            for a in _sorted_actions(detail)
        ],
    }


def expected_canonical_after(before_detail, plan):
    expected = canonicalize_detail(before_detail)
    expected["name"] = plan["target_title"]
    expected["actions"][plan["action_index"]]["weights"] = plan["new_weights_csv"]
    return expected


def verify_fetch_back(before_detail, after_detail, plan):
    """Deterministic comparison: the fetched-back workout must equal the original
    plus EXACTLY the planned change. Returns (ok, list_of_mismatch_strings)."""
    expected = expected_canonical_after(before_detail, plan)
    actual = canonicalize_detail(after_detail)
    mismatches = []

    if actual["name"] != expected["name"]:
        mismatches.append(f"name: expected {expected['name']!r}, got {actual['name']!r}")

    target_count = sum(
        1 for a in actual["actions"]
        if str(a.get("groupId")) == str(plan["exercise_id"])
    )
    if target_count != 1:
        mismatches.append(
            f"exercise {plan['exercise_id']}: expected exactly 1 occurrence, got {target_count}"
        )

    if len(actual["actions"]) != len(expected["actions"]):
        mismatches.append(
            f"exercise count: expected {len(expected['actions'])}, got {len(actual['actions'])}"
        )
    else:
        for idx, (exp_a, act_a) in enumerate(zip(expected["actions"], actual["actions"])):
            for field in CANONICAL_ACTION_FIELDS:
                if act_a[field] != exp_a[field]:
                    mismatches.append(
                        f"action[{idx}].{field}: expected {exp_a[field]!r}, got {act_a[field]!r}"
                    )
    return (len(mismatches) == 0, mismatches)


# ── artifacts ────────────────────────────────────────────────────

def _assert_no_secrets(text, client):
    """Refuse to write an artifact that contains configured credential values."""
    for key in ("token", "user_id"):
        value = (client.credentials or {}).get(key) or ""
        if value and value in text:
            raise WorkoutUpdateError(
                f"Refusing to write artifact: serialized content contains the configured {key}."
            )


def write_artifacts(runtime_dir, client, detail, plan, apply_requested):
    """Writes the sanitized write plan and the rollback backup. Returns paths."""
    os.makedirs(runtime_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    wid = detail.get("id")

    restore_payload = build_save_payload(detail, getattr(client, "device_type", 1),
                                         detail.get("name"))
    backup = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "workout_id": wid,
        "workout_title": detail.get("name"),
        "sanitized_hash": plan["sanitized_hash"],
        "detail": _redact_user_ids(detail),
        "restore_payload": restore_payload,
    }
    backup_text = json.dumps(backup, indent=2, ensure_ascii=False, default=str)
    _assert_no_secrets(backup_text, client)
    backup_path = os.path.join(runtime_dir, f"backup-{wid}-{stamp}.json")
    with open(backup_path, "w", encoding="utf-8") as f:
        f.write(backup_text)

    artifact = plan_artifact(plan, apply_requested)
    plan_text = json.dumps(artifact, indent=2, ensure_ascii=False, default=str)
    _assert_no_secrets(plan_text, client)
    plan_path = os.path.join(runtime_dir, f"plan-{wid}-{stamp}.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        f.write(plan_text)

    return plan_path, backup_path, restore_payload


# ── orchestration ────────────────────────────────────────────────

def write_enabled():
    return os.environ.get(WRITE_ENABLE_ENV, "").strip().lower() == "true"


def run_update(client, title, new_title, exercise_id, expected_capacity,
               new_capacity, apply=False, runtime_dir=DEFAULT_RUNTIME_DIR):
    """Full pipeline. Returns a result dict; raises WorkoutUpdateError on any
    validation/safety-stop condition (never mid-write: all validation happens
    before the single POST)."""
    if not (client.credentials or {}).get("token"):
        raise WorkoutUpdateError(
            "Authentication is not configured (no token). Aborting before any lookup."
        )

    workouts = client.get_user_workouts()
    source = find_workout_by_exact_title(workouts, title)
    ensure_target_title_available(workouts, new_title, source.get("id"))

    detail = client.get_workout_detail(source.get("code"))
    if not detail:
        raise WorkoutUpdateError(f"Could not fetch detail for workout {title!r}.")
    if detail.get("name") != title or detail.get("id") != source.get("id"):
        raise WorkoutUpdateError(
            "Fetched detail does not match the selected workout (title/id mismatch); "
            "refusing to continue."
        )

    plan = build_update_plan(detail, exercise_id, expected_capacity, new_capacity, new_title)
    plan_path, backup_path, restore_payload = write_artifacts(
        runtime_dir, client, detail, plan, apply_requested=apply
    )

    result = {
        "mode": "dry-run",
        "plan": plan,
        "plan_path": plan_path,
        "backup_path": backup_path,
        "outcome": "DRY-RUN ONLY - no write performed",
    }
    if not apply:
        return result

    # Live write: both interlocks required.
    if not write_enabled():
        raise WorkoutUpdateError(
            f"--apply given but {WRITE_ENABLE_ENV} is not 'true'. "
            "Both interlocks are required; no write performed."
        )

    payload = build_save_payload(
        detail, getattr(client, "device_type", 1), new_title,
        action_index=plan["action_index"],
        new_weights_csv=plan["new_weights_csv"],
        new_action_capacity=plan["new_action_capacity"],
        new_total_capacity=plan["new_total_capacity"],
    )
    resp = client.save_workout_payload(payload)
    if not isinstance(resp, dict) or resp.get("code") != 0:
        raise WorkoutUpdateError(
            f"Write rejected by API (code={resp.get('code') if isinstance(resp, dict) else resp}); "
            "no retry attempted. Backup retained at " + backup_path
        )

    result["mode"] = "apply"

    # Fetch-back verification.
    after_workouts = client.get_user_workouts()
    after_entry = next(
        (w for w in after_workouts if w.get("id") == source.get("id")), None
    )
    after_detail = client.get_workout_detail(
        (after_entry or {}).get("code") or source.get("code")
    )
    ok, mismatches = (False, ["could not fetch workout back"]) if not after_detail \
        else verify_fetch_back(detail, after_detail, plan)

    if ok:
        result["outcome"] = "LIVE UPDATE VERIFIED"
        result["mismatches"] = []
        return result

    # Rollback from backup via the same evidence-backed endpoint.
    result["mismatches"] = mismatches
    rb_resp = client.save_workout_payload(restore_payload)
    rb_detail = client.get_workout_detail(source.get("code"))
    restored = (
        isinstance(rb_resp, dict) and rb_resp.get("code") == 0 and rb_detail
        and canonicalize_detail(rb_detail) == canonicalize_detail(detail)
    )
    if restored:
        result["outcome"] = "LIVE UPDATE ROLLED BACK"
    else:
        result["outcome"] = (
            "LIVE UPDATE STATE UNCERTAIN - fetch-back verification failed and "
            "rollback could not be confirmed. Backup retained at " + backup_path
        )
    return result


# ── CLI ──────────────────────────────────────────────────────────

def _print_result(result):
    plan = result["plan"]
    print(f"Workout:        {plan['source_title']!r} (id={plan['workout_id']})")
    print(f"New title:      {plan['target_title']!r}")
    print(f"Exercise:       {plan['exercise_title']!r} (id={plan['exercise_id']})")
    print(f"Weights:        {plan['old_weights_csv']} -> {plan['new_weights_csv']}"
          f"  (sets changed: {plan['sets_changed']})")
    print(f"Preserved:      {', '.join(PRESERVED_PROPERTIES)}")
    print(f"Write plan:     {result['plan_path']}")
    print(f"Backup:         {result['backup_path']}")
    print(f"Mode:           {result['mode']}")
    print(f"Outcome:        {result['outcome']}")
    for m in result.get("mismatches", []):
        print(f"  MISMATCH: {m}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Safely update one exercise's capacity/weight in a saved "
                    "Speediance workout template (dry-run by default).")
    parser.add_argument("--title", required=True,
                        help="Exact title of the saved workout to update")
    parser.add_argument("--new-title", required=True,
                        help="New exact title for the workout")
    parser.add_argument("--exercise-id", required=True, type=int,
                        help="Exercise (group) ID, e.g. 321 for Barbell Bent Over Row")
    parser.add_argument("--expected-capacity", required=True, type=float,
                        help="Current capacity/weight the sets must have (NOT reps)")
    parser.add_argument("--capacity", required=True, type=float,
                        help="New capacity/weight for the matching sets (NOT reps)")
    parser.add_argument("--apply", action="store_true",
                        help=f"Perform the live write (also requires {WRITE_ENABLE_ENV}=true)")
    parser.add_argument("--runtime-dir", default=DEFAULT_RUNTIME_DIR,
                        help="Folder for write-plan/backup artifacts (git-ignored)")
    args = parser.parse_args(argv)

    from api_client import SpeedianceClient
    client = SpeedianceClient()
    # Same env-var credential override convention as test_e2e_workouts.py.
    env_uid = os.environ.get("SPEEDIANCE_USER_ID")
    env_tok = os.environ.get("SPEEDIANCE_TOKEN")
    if env_uid and env_tok:
        client.credentials["user_id"] = env_uid
        client.credentials["token"] = env_tok

    try:
        result = run_update(
            client,
            title=args.title,
            new_title=args.new_title,
            exercise_id=args.exercise_id,
            expected_capacity=args.expected_capacity,
            new_capacity=args.capacity,
            apply=args.apply,
            runtime_dir=args.runtime_dir,
        )
    except WorkoutUpdateError as e:
        print(f"SAFETY STOP: {e}")
        return 2

    _print_result(result)
    if result["outcome"] == "LIVE UPDATE VERIFIED" or result["mode"] == "dry-run":
        return 0
    return 3


if __name__ == "__main__":
    sys.exit(main())
