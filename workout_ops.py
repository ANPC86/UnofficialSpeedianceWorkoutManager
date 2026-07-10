"""
Deterministic saved-workout operations for Speediance: schedule, unschedule,
remove (and recreate-from-backup). Building blocks for an eventual AI fitness
coach — every operation is dry-run by default, exact-match only, and verified
by fetching remote state back after a mutation.

Evidence-backed API contracts (see WORKOUT_OPERATIONS.md):
    schedule/unschedule: POST /api/app/templateReservation
                         {status: 1|0, deviceType, thatDay: "YYYY-MM-DD",
                          templateCode} — date-only, keyed by (date, code);
                         the API exposes no per-entry identifier for deletes.
    schedule list:       GET /api/app/v5/trainingCalendar/monthNew?date=YYYY-MM
                         -> [{date, trainingPlanList: [{title, code|templateCode,
                             isReservation, ...}]}]
    delete template:     DELETE /api/app/customTrainingTemplate?ids=<numeric id>
    recreate (re-create): POST /api/app/v2/customTrainingTemplate (no id)

Semantics:
    - Multiple workouts on one date are VALID. The idempotency/identity key for
      schedule entries is (thatDay, templateCode). Duplicated same-code entries
      on one date are an ambiguous remote state and block mutation.
    - Remote workout removal is IDENTITY-DESTRUCTIVE: the backup permits
      content recreation, generally under a NEW remote id/code. It is not a
      reversible delete.
    - The remote API is date-only; the canonical application timezone
      (America/Edmonton) determines the intended local calendar date via
      zoneinfo/tzdata. There is no silent fallback to system time or UTC.

Write interlocks:
    schedule/unschedule/recreate: --apply AND SPEEDIANCE_WRITE_ENABLED=true
    remove: additionally SPEEDIANCE_DESTRUCTIVE_WRITE_ENABLED=true,
            --confirm-title equal to the remote title, and
            --expected-id equal to the resolved remote id.
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from workout_update import (
    DEFAULT_RUNTIME_DIR,
    WRITE_ENABLE_ENV,
    WorkoutUpdateError,
    _assert_no_secrets,
    _redact_user_ids,
    build_save_payload,
    canonicalize_detail,
    find_workout_by_exact_title,
    sanitized_hash,
    write_enabled,
)

DESTRUCTIVE_ENV = "SPEEDIANCE_DESTRUCTIVE_WRITE_ENABLED"
SCHEDULE_PLAN_SCHEMA = "speediance-schedule-plan/v1"
REMOVAL_PLAN_SCHEMA = "speediance-workout-removal-plan/v1"
REMOVAL_BACKUP_SCHEMA = "speediance-workout-removal-backup/v2"
_REMOVAL_BACKUP_SCHEMAS_ACCEPTED = (
    REMOVAL_BACKUP_SCHEMA,
    "speediance-workout-removal-backup/v1",  # pre-correction backups
)
DEFAULT_TIMEZONE = "America/Edmonton"

# Routine read windows — for ordinary display/lookup/coaching/sync only.
# NEVER use these to prove a workout has no future schedule references;
# removal uses its own distinct scan horizon below.
ROUTINE_HISTORY_DAYS_BACK = 30
ROUTINE_CALENDAR_DAYS_BACK = 7
ROUTINE_CALENDAR_DAYS_FORWARD = 30

# Destructive-removal future-reference scan (separate from routine windows).
# Operator policy: scheduling normally reaches at most 7 days ahead; 30 days
# gives buffer without a monthly-request fan-out. The result is always bounded
# evidence for the recorded horizon, never proof that no later reference
# exists. Operators who scheduled further ahead should pass a larger
# --schedule-scan-days.
DEFAULT_REMOVAL_SCAN_DAYS = 30
MAX_REMOVAL_SCAN_DAYS = 3650


def destructive_write_enabled():
    return os.environ.get(DESTRUCTIVE_ENV, "").strip().lower() == "true"


# ── timezone (canonical: America/Edmonton, zoneinfo/tzdata only) ──

def get_zone(tz_name):
    """Resolve an IANA timezone strictly. No silent fallback to system-local
    time and no silent UTC-as-Edmonton: unresolvable timezones are an error."""
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError, ValueError, TypeError):
        raise WorkoutUpdateError(
            f"Timezone {tz_name!r} could not be resolved via zoneinfo. Install "
            "the 'tzdata' package (see requirements.txt) or correct the "
            "timezone name; silent fallback is not permitted."
        )


def local_now(tz_name=DEFAULT_TIMEZONE):
    return datetime.now(get_zone(tz_name))


def local_today(tz_name=DEFAULT_TIMEZONE):
    return local_now(tz_name).date()


def tz_offset_minutes(tz_name, on_date):
    """Effective UTC offset (minutes) for a local calendar date, computed at
    local noon to stay clear of DST transition instants. Edmonton: -420 in
    winter (MST), -360 in summer (MDT)."""
    tz = get_zone(tz_name)
    at_noon = datetime(on_date.year, on_date.month, on_date.day, 12, tzinfo=tz)
    return int(at_noon.utcoffset().total_seconds() // 60)


def timezone_plan_fields(tz_name):
    """The timezone evidence block recorded in every generated plan."""
    now = local_now(tz_name)
    return {
        "timezone_name": tz_name,
        "local_date": now.strftime("%Y-%m-%d"),
        "effective_utc_offset_minutes": int(now.utcoffset().total_seconds() // 60),
        "timezone_source": "zoneinfo",
    }


def parse_utc_timestamp(value):
    """Parse an explicit-UTC Speediance timestamp into an aware UTC datetime,
    preserving the original instant. Accepts epoch seconds/milliseconds,
    ISO-8601 (Z or offset), naive 'YYYY-MM-DD HH:MM:SS' server time (UTC),
    and datetime objects (naive treated as UTC)."""
    if isinstance(value, datetime):
        return (value.astimezone(dt_timezone.utc) if value.tzinfo
                else value.replace(tzinfo=dt_timezone.utc))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000.0 if abs(value) > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, tz=dt_timezone.utc)
    s = str(value).strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(s)
    except ValueError:
        raise WorkoutUpdateError(f"Unparseable UTC timestamp {value!r}.")
    return (parsed.astimezone(dt_timezone.utc) if parsed.tzinfo
            else parsed.replace(tzinfo=dt_timezone.utc))


def utc_to_local(value, tz_name=DEFAULT_TIMEZONE):
    """UTC timestamp -> aware datetime in the canonical timezone."""
    return parse_utc_timestamp(value).astimezone(get_zone(tz_name))


def local_training_date(value, tz_name=DEFAULT_TIMEZONE):
    """The local calendar date a UTC instant belongs to. Never derived by
    truncating the UTC timestamp: a late-evening Edmonton workout whose UTC
    time has crossed midnight stays on the Edmonton date."""
    return utc_to_local(value, tz_name).date()


# ── routine read windows ─────────────────────────────────────────

def routine_history_window(tz_name=DEFAULT_TIMEZONE):
    """Default training-history window: local today - 30 days -> today."""
    today = local_today(tz_name)
    return today - timedelta(days=ROUTINE_HISTORY_DAYS_BACK), today


def routine_calendar_window(tz_name=DEFAULT_TIMEZONE):
    """Default calendar/schedule window: local today - 7 days -> today + 30."""
    today = local_today(tz_name)
    return (today - timedelta(days=ROUTINE_CALENDAR_DAYS_BACK),
            today + timedelta(days=ROUTINE_CALENDAR_DAYS_FORWARD))


# ── date validation ──────────────────────────────────────────────

def validate_date(date_str):
    """Strict YYYY-MM-DD (zero-padded, real calendar date). The Speediance
    schedule payload is date-only; this exact string becomes `thatDay`."""
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise WorkoutUpdateError(f"Invalid date {date_str!r}: expected YYYY-MM-DD.")
    if parsed.strftime("%Y-%m-%d") != date_str:
        raise WorkoutUpdateError(f"Invalid date {date_str!r}: expected zero-padded YYYY-MM-DD.")
    return parsed


def validate_scan_days(days):
    try:
        parsed = int(days)
    except (ValueError, TypeError):
        raise WorkoutUpdateError(f"Invalid schedule scan horizon {days!r}: expected days as an integer.")
    if isinstance(days, bool) or parsed < 1 or parsed > MAX_REMOVAL_SCAN_DAYS:
        raise WorkoutUpdateError(
            f"Invalid schedule scan horizon {days!r}: must be 1..{MAX_REMOVAL_SCAN_DAYS} days."
        )
    return parsed


def _month_key(d):
    return f"{d.year:04d}-{d.month:02d}"


def months_in_range(start, end):
    """Every YYYY-MM month key intersecting [start, end]."""
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return months


# ── calendar helpers ─────────────────────────────────────────────

def _entry_code(plan_entry):
    # The calendar API returns either 'code' or 'templateCode' (see index.html).
    return str(plan_entry.get("code") or plan_entry.get("templateCode") or "")


def _is_reservation(plan_entry):
    # isReservation === false marks official programs / completed history
    # entries, which must never be touched (see index.html renderCalendarGrid).
    return plan_entry.get("isReservation") is not False


def day_entries(month_days, date_str):
    for day in month_days or []:
        if day.get("date") == date_str:
            return day.get("trainingPlanList") or []
    return []


def reservations_for(month_days, date_str, code=None):
    return [
        p for p in day_entries(month_days, date_str)
        if _is_reservation(p) and (code is None or _entry_code(p) == str(code))
    ]


def canonicalize_schedule(month_days, exclude=None):
    """Canonical month view for unrelated-change comparison. `exclude` is an
    optional (date_str, code) reservation to drop from the view so before/after
    differ only outside the planned change. Other workouts on the SAME date are
    part of the compared state and must survive mutations untouched."""
    out = {}
    for day in month_days or []:
        entries = []
        for p in day.get("trainingPlanList") or []:
            key = (_entry_code(p), str(p.get("title") or ""), _is_reservation(p))
            if (exclude and day.get("date") == exclude[0]
                    and key[0] == str(exclude[1]) and key[2]):
                continue
            entries.append(key)
        out[day.get("date")] = sorted(entries)
    return out


def _sanitize_entry(date_str, plan_entry):
    return {
        "date": date_str,
        "entry_id": plan_entry.get("id"),
        "code": _entry_code(plan_entry),
        "title": plan_entry.get("title"),
    }


# ── shared plumbing ──────────────────────────────────────────────

def _require_auth(client):
    if not (client.credentials or {}).get("token"):
        raise WorkoutUpdateError(
            "Authentication is not configured (no token). Aborting before any lookup."
        )


def _resolve_workout(client, title):
    workouts = client.get_user_workouts()
    source = find_workout_by_exact_title(workouts, title)
    code = source.get("code")
    if not code or source.get("id") is None:
        raise WorkoutUpdateError(
            f"Workout {title!r} is missing its immutable identifiers (id/code)."
        )
    return workouts, source


def _write_artifact(runtime_dir, client, prefix, payload):
    os.makedirs(runtime_dir, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    _assert_no_secrets(text, client)
    path = os.path.join(runtime_dir, f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _require_standard_interlock():
    if not write_enabled():
        raise WorkoutUpdateError(
            f"--apply given but {WRITE_ENABLE_ENV} is not 'true'. "
            "Both interlocks are required; no write performed."
        )


# ── schedule ─────────────────────────────────────────────────────

def run_schedule(client, title, date_str, timezone=DEFAULT_TIMEZONE,
                 apply=False, runtime_dir=DEFAULT_RUNTIME_DIR):
    _require_auth(client)
    target_date = validate_date(date_str)
    tz_fields = timezone_plan_fields(timezone)
    if target_date < local_today(timezone):
        raise WorkoutUpdateError(
            f"Refusing to schedule in the past ({date_str} < local today "
            f"{tz_fields['local_date']} in {timezone})."
        )

    _, source = _resolve_workout(client, title)
    code, wid = source["code"], source["id"]

    before = client.get_calendar_month(_month_key(target_date)) or []
    state = "ok" if before else "empty-or-unavailable"
    same = reservations_for(before, date_str, code)
    # Multiple workouts on one date are VALID; other entries are recorded as
    # context, never treated as conflicts. Identity key: thatDay+templateCode.
    others = [p for p in reservations_for(before, date_str) if _entry_code(p) != str(code)]

    plan = {
        "schema_version": SCHEDULE_PLAN_SCHEMA,
        "operation": "schedule_workout",
        "workout_title": title,
        "workout_id": wid,
        "workout_code": code,
        "scheduled_date": date_str,
        **tz_fields,
        "schedule_state": state,
        "existing_same_entries": len(same),
        "existing_other_entries": [_sanitize_entry(date_str, p) for p in others],
        "apply_requested": bool(apply),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    plan_path = _write_artifact(runtime_dir, client, "schedule-plan", plan)
    result = {"operation": "schedule_workout", "mode": "apply" if apply else "dry-run",
              "plan": plan, "plan_path": plan_path}

    if len(same) == 1:
        # Identical (thatDay, templateCode) reservation exists: idempotent no-op.
        result["outcome"] = "ALREADY_SCHEDULED - idempotent no-op, no write performed"
        return result
    if len(same) > 1:
        raise WorkoutUpdateError(
            f"Remote state is ambiguous: {len(same)} schedule entries for "
            f"template code {code} on {date_str}. Refusing to add another."
        )
    if not apply:
        result["outcome"] = "DRY-RUN ONLY - no write performed"
        if state != "ok":
            result["outcome"] += " (warning: schedule state was empty/unavailable)"
        return result

    _require_standard_interlock()
    if state != "ok":
        raise WorkoutUpdateError(
            f"Schedule for {_month_key(target_date)} could not be read (empty response); "
            "state is ambiguous, refusing to mutate."
        )

    ok = client.schedule_workout(date_str, code, 1)
    if not ok:
        raise WorkoutUpdateError("Schedule API did not confirm the reservation.")

    after = client.get_calendar_month(_month_key(target_date)) or []
    now_entries = reservations_for(after, date_str, code)
    unrelated_ok = (
        canonicalize_schedule(before, exclude=(date_str, code))
        == canonicalize_schedule(after, exclude=(date_str, code))
    )
    if len(now_entries) == 1 and unrelated_ok:
        result["outcome"] = "SCHEDULED AND VERIFIED"
        result["entry"] = _sanitize_entry(date_str, now_entries[0])
    else:
        result["outcome"] = (
            f"SCHEDULE STATE UNCERTAIN - expected exactly 1 entry, found "
            f"{len(now_entries)}; unrelated entries unchanged: {unrelated_ok}"
        )
    return result


# ── unschedule ───────────────────────────────────────────────────

def run_unschedule(client, title, date_str, timezone=DEFAULT_TIMEZONE,
                   apply=False, runtime_dir=DEFAULT_RUNTIME_DIR):
    _require_auth(client)
    target_date = validate_date(date_str)
    tz_fields = timezone_plan_fields(timezone)

    _, source = _resolve_workout(client, title)
    code, wid = source["code"], source["id"]

    before = client.get_calendar_month(_month_key(target_date)) or []
    matches = reservations_for(before, date_str, code)

    plan = {
        "schema_version": SCHEDULE_PLAN_SCHEMA,
        "operation": "unschedule_workout",
        "workout_title": title,
        "workout_id": wid,
        "workout_code": code,
        "scheduled_date": date_str,
        **tz_fields,
        # The proven API contract deletes by (thatDay, templateCode); the
        # calendar's optional display id is recorded as evidence only and is
        # never used as the delete key.
        "schedule_entry_id": matches[0].get("id") if len(matches) == 1 else None,
        "delete_key": {"thatDay": date_str, "templateCode": code},
        "matching_entries": len(matches),
        "apply_requested": bool(apply),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    plan_path = _write_artifact(runtime_dir, client, "unschedule-plan", plan)
    result = {"operation": "unschedule_workout", "mode": "apply" if apply else "dry-run",
              "plan": plan, "plan_path": plan_path}

    if not before:
        # Cannot distinguish "nothing scheduled" from a failed read.
        if apply:
            raise WorkoutUpdateError(
                f"Schedule for {_month_key(target_date)} could not be read (empty "
                "response); state is ambiguous, refusing a blind delete."
            )
        result["outcome"] = "SCHEDULE STATE AMBIGUOUS - empty month response, no-op"
        return result

    if len(matches) == 0:
        # No blind deletes: a missing entry is a clear no-op in BOTH modes.
        result["outcome"] = "NOT_SCHEDULED - no matching entry, no-op"
        return result
    if len(matches) > 1:
        raise WorkoutUpdateError(
            f"{len(matches)} schedule entries match template code {code} on {date_str}; "
            "the delete key (thatDay+templateCode) would remove ambiguously. Refusing."
        )
    if not apply:
        result["outcome"] = "DRY-RUN ONLY - no write performed"
        return result

    _require_standard_interlock()
    ok = client.schedule_workout(date_str, code, 0)
    if not ok:
        raise WorkoutUpdateError("Schedule API did not confirm the unschedule.")

    after = client.get_calendar_month(_month_key(target_date)) or []
    remaining = reservations_for(after, date_str, code)
    unrelated_ok = (
        canonicalize_schedule(before, exclude=(date_str, code))
        == canonicalize_schedule(after, exclude=(date_str, code))
    )
    if len(remaining) == 0 and unrelated_ok:
        result["outcome"] = "UNSCHEDULED AND VERIFIED"
    else:
        result["outcome"] = (
            f"UNSCHEDULE STATE UNCERTAIN - {len(remaining)} matching entries remain; "
            f"unrelated entries unchanged: {unrelated_ok}"
        )
    return result


# ── remove (identity-destructive) ────────────────────────────────

def find_schedule_references(client, code, scan_start, scan_end):
    """Reservations referencing the workout within [scan_start, scan_end].
    Every calendar month intersecting the horizon is queried. The result is
    BOUNDED evidence: absence means no reference was found within the recorded
    scan horizon, not that no future reference exists."""
    refs = []
    months = months_in_range(scan_start, scan_end)
    start_str = scan_start.strftime("%Y-%m-%d")
    end_str = scan_end.strftime("%Y-%m-%d")
    for month in months:
        month_days = client.get_calendar_month(month) or []
        for day in month_days:
            d = day.get("date") or ""
            if not (start_str <= d <= end_str):
                continue
            for p in day.get("trainingPlanList") or []:
                if _is_reservation(p) and _entry_code(p) == str(code):
                    refs.append(_sanitize_entry(d, p))
    return refs, months


def run_remove(client, title, expected_id=None, confirm_title=None,
               apply=False, runtime_dir=DEFAULT_RUNTIME_DIR,
               timezone=DEFAULT_TIMEZONE, scan_days=DEFAULT_REMOVAL_SCAN_DAYS):
    """Remote removal is IDENTITY-DESTRUCTIVE: the backup written here permits
    content recreation via the proven save endpoint, generally under a NEW
    remote id/code. It does not preserve the original remote identity."""
    _require_auth(client)
    scan_days = validate_scan_days(scan_days)
    workouts, source = _resolve_workout(client, title)
    code, wid = source["code"], source["id"]

    detail = client.get_workout_detail(code)
    if not detail:
        raise WorkoutUpdateError(f"Could not fetch detail for workout {title!r}.")
    if detail.get("name") != title or detail.get("id") != wid:
        raise WorkoutUpdateError(
            "Fetched detail does not match the selected workout (title/id mismatch); "
            "refusing to continue."
        )

    tz_fields = timezone_plan_fields(timezone)
    scan_start = local_today(timezone)
    scan_end = scan_start + timedelta(days=scan_days)
    refs, months = find_schedule_references(client, code, scan_start, scan_end)

    # Recreation payload: same proven save endpoint, WITHOUT id -> re-creates
    # equivalent content under a new remote identity.
    recreation_payload = build_save_payload(detail, getattr(client, "device_type", 1),
                                            detail.get("name"))
    recreation_payload.pop("id", None)

    doc_stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    backup = {
        "schema_version": REMOVAL_BACKUP_SCHEMA,
        "created_at": doc_stamp,
        "workout_id": wid,
        "workout_code": code,
        "workout_title": title,
        "sanitized_hash": sanitized_hash(detail),
        "adapter": "api_client.SpeedianceClient/v2-customTrainingTemplate",
        "remote_identity_preserved": False,
        "recreation_expected_new_remote_identity": True,
        "detail": _redact_user_ids(detail),
        "recreation_payload": recreation_payload,
    }
    backup_path = _write_artifact(runtime_dir, client, "remove-backup", backup)

    plan = {
        "schema_version": REMOVAL_PLAN_SCHEMA,
        "operation": "remove_custom_workout",
        "workout_title": title,
        "workout_id": wid,
        "workout_code": code,
        **tz_fields,
        "schedule_scan": {
            "scan_days": scan_days,
            "scan_start_date": scan_start.strftime("%Y-%m-%d"),
            "scan_end_date": scan_end.strftime("%Y-%m-%d"),
            "months_requested": len(months),
            "months": months,
            "result": (
                "schedule references found within the recorded scan horizon"
                if refs else
                "no schedule reference was found within the recorded scan horizon"
            ),
        },
        "scheduled_reference_count": len(refs),
        "scheduled_references": refs,
        "remote_identity_preserved": False,
        "recreation_expected_new_remote_identity": True,
        "sanitized_hash": backup["sanitized_hash"],
        "backup_path": backup_path,
        "apply_requested": bool(apply),
        "created_at": doc_stamp,
    }
    plan_path = _write_artifact(runtime_dir, client, "remove-plan", plan)
    result = {"operation": "remove_custom_workout",
              "mode": "apply" if apply else "dry-run",
              "plan": plan, "plan_path": plan_path, "backup_path": backup_path}

    if refs:
        if apply:
            raise WorkoutUpdateError(
                f"Workout {title!r} has {len(refs)} schedule reference(s) within the "
                f"scan horizon {plan['schedule_scan']['scan_start_date']}..{plan['schedule_scan']['scan_end_date']} "
                f"({', '.join(r['date'] for r in refs)}); unschedule them explicitly "
                "first. Removal will not unschedule automatically."
            )
        result["outcome"] = (
            f"BLOCKED - {len(refs)} schedule reference(s) within the scan horizon; "
            "unschedule explicitly before removal"
        )
        return result

    if not apply:
        result["outcome"] = "DRY-RUN ONLY - no delete performed"
        return result

    # Destructive interlocks: every one of these must pass.
    _require_standard_interlock()
    if not destructive_write_enabled():
        raise WorkoutUpdateError(
            f"--apply given but {DESTRUCTIVE_ENV} is not 'true'. Destructive "
            "removal requires the destructive interlock; no delete performed."
        )
    if confirm_title != title:
        raise WorkoutUpdateError(
            f"--confirm-title {confirm_title!r} does not exactly match the remote "
            f"title {title!r}; no delete performed."
        )
    if expected_id is None or str(expected_id) != str(wid):
        raise WorkoutUpdateError(
            f"--expected-id {expected_id!r} does not match the resolved remote id "
            f"{wid!r}; no delete performed."
        )

    resp = client.delete_workout_checked(wid)
    if isinstance(resp, dict) and resp.get("code") not in (0, None):
        raise WorkoutUpdateError(
            f"Delete rejected by API (code={resp.get('code')}); workout left intact. "
            "Backup retained at " + backup_path
        )

    after = client.get_user_workouts()
    still_there = [w for w in after if w.get("id") == wid or w.get("code") == code]
    others_before = {(w.get("id"), w.get("name"), w.get("code"))
                     for w in workouts if w.get("id") != wid}
    others_after = {(w.get("id"), w.get("name"), w.get("code"))
                    for w in after if w.get("id") != wid}
    if still_there:
        raise WorkoutUpdateError(
            f"Delete did not take effect: workout id {wid} still present. "
            "State intact; no retry attempted."
        )
    if others_before != others_after:
        result["outcome"] = (
            "REMOVE STATE UNCERTAIN - target absent but unrelated workouts changed; "
            "evidence preserved, no speculative writes. Backup: " + backup_path
        )
        return result
    result["outcome"] = (
        "REMOVED AND VERIFIED (identity-destructive; backup permits content "
        "recreation under a new remote identity)"
    )
    return result


# ── recreate from a removal backup ───────────────────────────────

def run_recreate_from_backup(client, backup_path, apply=False):
    """Re-creates equivalent workout CONTENT from a remove-backup artifact via
    the proven save endpoint (POST without id). The recreated workout receives
    a NEW remote id/code — the original remote identity is NOT restored, and
    this is never a rollback of the deletion."""
    _require_auth(client)
    with open(backup_path, encoding="utf-8") as f:
        backup = json.load(f)
    if backup.get("schema_version") not in _REMOVAL_BACKUP_SCHEMAS_ACCEPTED:
        raise WorkoutUpdateError(
            f"{backup_path} is not a recognized removal backup "
            f"(accepted: {', '.join(_REMOVAL_BACKUP_SCHEMAS_ACCEPTED)})."
        )
    payload = backup.get("recreation_payload") or backup.get("restore_create_payload")
    if not payload or "id" in payload:
        raise WorkoutUpdateError("Backup recreation payload missing or not a create payload.")

    result = {
        "operation": "recreate_workout_from_backup",
        "mode": "apply" if apply else "dry-run",
        "workout_title": backup.get("workout_title"),
        "backup_path": backup_path,
        "deleted_remote_id": backup.get("workout_id"),
        "deleted_remote_code": backup.get("workout_code"),
        "old_canonical_hash": backup.get("sanitized_hash"),
        "remote_identity_preserved": False,
    }
    if not apply:
        result["outcome"] = "DRY-RUN ONLY - no write performed"
        return result

    _require_standard_interlock()
    resp = client.save_workout_payload(payload)
    if not isinstance(resp, dict) or resp.get("code") != 0:
        raise WorkoutUpdateError(
            f"Recreation rejected by API (code={resp.get('code') if isinstance(resp, dict) else resp})."
        )

    old_id = backup.get("workout_id")
    candidates = [
        w for w in client.get_user_workouts()
        if w.get("name") == backup.get("workout_title") and w.get("id") != old_id
    ]
    if len(candidates) != 1:
        result["outcome"] = (
            f"RECREATE STATE UNCERTAIN - expected exactly 1 new workout titled "
            f"{backup.get('workout_title')!r}, found {len(candidates)}"
        )
        return result

    new_id, new_code = candidates[0].get("id"), candidates[0].get("code")
    result["new_remote_id"] = new_id
    result["new_remote_code"] = new_code
    # Identity and content equivalence are verified SEPARATELY: a content match
    # under a new id/code is content restoration, never identity restoration.
    result["identity_preserved"] = (
        new_id == old_id and new_code == backup.get("workout_code")
    )
    new_detail = client.get_workout_detail(new_code)
    result["recreated_canonical_hash"] = sanitized_hash(new_detail) if new_detail else None
    content_equivalent = bool(
        new_detail
        and canonicalize_detail(new_detail) == canonicalize_detail(backup["detail"])
    )
    result["content_equivalent"] = content_equivalent
    if content_equivalent:
        result["outcome"] = (
            "RECREATED FROM BACKUP - content verified equivalent under a NEW "
            f"remote identity (old id={old_id} -> new id={new_id}); "
            "original remote identity was not restored"
        )
    else:
        result["outcome"] = "RECREATE STATE UNCERTAIN - recreated workout differs canonically"
    return result


# Compatibility alias (pre-correction name). Documentation and CLI use the
# corrected recreate-from-backup terminology.
run_restore = run_recreate_from_backup


# ── CLI ──────────────────────────────────────────────────────────

def _print_result(result):
    print(f"Operation:  {result['operation']}")
    print(f"Mode:       {result['mode']}")
    for key in ("plan_path", "backup_path"):
        if result.get(key):
            print(f"{'Plan:' if key == 'plan_path' else 'Backup:':11} {result[key]}")
    plan = result.get("plan") or {}
    if plan.get("workout_id") is not None:
        print(f"Workout:    {plan.get('workout_title')!r} "
              f"(id={plan.get('workout_id')}, code={plan.get('workout_code')})")
    if plan.get("scheduled_date"):
        print(f"Date:       {plan['scheduled_date']} "
              f"(tz {plan.get('timezone_name')}, offset "
              f"{plan.get('effective_utc_offset_minutes')} min, "
              f"{plan.get('timezone_source')})")
    scan = plan.get("schedule_scan")
    if scan:
        print(f"Scan:       {scan['scan_start_date']}..{scan['scan_end_date']} "
              f"({scan['scan_days']} days, {scan['months_requested']} months) - "
              f"{scan['result']}")
    for r in plan.get("scheduled_references") or []:
        print(f"  scheduled: {r['date']} (entry_id={r['entry_id']})")
    print(f"Outcome:    {result['outcome']}")


def _make_client():
    from api_client import SpeedianceClient
    client = SpeedianceClient()
    env_uid = os.environ.get("SPEEDIANCE_USER_ID")
    env_tok = os.environ.get("SPEEDIANCE_TOKEN")
    if env_uid and env_tok:
        client.credentials["user_id"] = env_uid
        client.credentials["token"] = env_tok
    return client


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Deterministic Speediance saved-workout operations "
                    "(dry-run by default).")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, with_date=True):
        p.add_argument("--title", required=True, help="Exact saved-workout title")
        if with_date:
            p.add_argument("--date", required=True, help="Date, strict YYYY-MM-DD")
            p.add_argument("--timezone", default=DEFAULT_TIMEZONE,
                           help="IANA timezone for LOCAL date interpretation only "
                                "(the API is date-only)")
        p.add_argument("--apply", action="store_true",
                       help=f"Perform the live write (requires {WRITE_ENABLE_ENV}=true)")
        p.add_argument("--runtime-dir", default=DEFAULT_RUNTIME_DIR)

    common(sub.add_parser("schedule", help="Schedule a saved workout on a date "
                                           "(multiple workouts per date are valid)"))
    common(sub.add_parser("unschedule", help="Remove one exact schedule entry "
                                             "(keyed by thatDay+templateCode)"))

    p_rm = sub.add_parser("remove",
                          help="Delete one exact custom saved workout "
                               "(identity-destructive)")
    common(p_rm, with_date=False)
    p_rm.add_argument("--expected-id", default=None,
                      help="Resolved remote workout id (required for --apply)")
    p_rm.add_argument("--confirm-title", default=None,
                      help="Must exactly repeat the remote title (required for --apply)")
    p_rm.add_argument("--schedule-scan-days", type=int,
                      default=DEFAULT_REMOVAL_SCAN_DAYS,
                      help=f"Future-reference scan horizon in days "
                           f"(default {DEFAULT_REMOVAL_SCAN_DAYS}; the scanned "
                           "window is recorded in the removal plan)")

    p_rc = sub.add_parser("recreate-from-backup", aliases=["restore"],
                          help="Re-create workout CONTENT from a removal backup "
                               "under a NEW remote identity")
    p_rc.add_argument("--backup", required=True, help="Path to a remove-backup-*.json")
    p_rc.add_argument("--apply", action="store_true")

    args = parser.parse_args(argv)
    client = _make_client()

    try:
        if args.command == "schedule":
            result = run_schedule(client, args.title, args.date, args.timezone,
                                  apply=args.apply, runtime_dir=args.runtime_dir)
        elif args.command == "unschedule":
            result = run_unschedule(client, args.title, args.date, args.timezone,
                                    apply=args.apply, runtime_dir=args.runtime_dir)
        elif args.command == "remove":
            result = run_remove(client, args.title, expected_id=args.expected_id,
                                confirm_title=args.confirm_title, apply=args.apply,
                                runtime_dir=args.runtime_dir,
                                scan_days=args.schedule_scan_days)
        else:  # recreate-from-backup / restore alias
            result = run_recreate_from_backup(client, args.backup, apply=args.apply)
    except WorkoutUpdateError as e:
        print(f"SAFETY STOP: {e}")
        return 2

    _print_result(result)
    outcome = result["outcome"]
    if "UNCERTAIN" in outcome:
        return 3
    if outcome.startswith("BLOCKED"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
