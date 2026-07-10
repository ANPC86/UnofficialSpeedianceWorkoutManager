"""Read-only custom-workout export, snapshot, and diff utilities.

The module deliberately reuses ``SpeedianceClient.get_user_workouts`` and
``SpeedianceClient.get_workout_detail``: the same two read paths used by the
dashboard and edit page.  It never calls any create, update, schedule, or
delete client method.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from api_client import SpeedianceClient


EXPORT_SCHEMA = "speediance-custom-workout-export/v1"
MANIFEST_SCHEMA = "speediance-custom-workout-snapshot-manifest/v1"
DIFF_SCHEMA = "speediance-custom-workout-diff/v1"
DEFAULT_TIMEZONE = "America/Edmonton"
VERSION_SUFFIX_RE = re.compile(r"(?:-v|\sv)([0-9]+)$", re.IGNORECASE)

# These are the exercised builder/detail fields.  The exporter intentionally
# does not carry arbitrary API response fields through to a local artifact.
ACTION_SOURCE_FIELDS = (
    "actionLibraryId", "groupId", "templatePresetId", "title", "setsAndReps",
    "weights", "counterweight", "counterweight2", "sportMode", "breakTime",
    "breakTime2", "leftRight", "selectCompletionMethod", "completionMethod",
    "countType", "level", "capacity", "dataStatType", "isLeftRight", "isBarbell",
)


class ExportError(RuntimeError):
    """Raised when a read-only export cannot produce a safe artifact."""


def parse_title_version(title: str) -> tuple[str, Optional[int]]:
    """Return a conservative title family and terminal version number.

    ``I-BK-v9`` and ``B REC-LB 1/1 v1`` are versioned; arbitrary embedded
    numbers are not.  Remote IDs/codes remain the identity used for snapshots.
    """
    original = str(title or "")
    match = VERSION_SUFFIX_RE.search(original)
    if not match:
        return original, None
    return original[:match.start()].rstrip(), int(match.group(1))


def _number(value: Any) -> Any:
    """Parse a numeric API value without inventing a value for blanks."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ExportError("non-finite numeric value is not exportable")
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return text
    if not math.isfinite(number):
        raise ExportError("non-finite numeric value is not exportable")
    return int(number) if number.is_integer() else number


def _csv(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return list(value)
    return str(value).split(",")


def _at(values: list[Any], index: int) -> Any:
    return values[index] if index < len(values) else None


def _field(action: Mapping[str, Any], name: str, index: int) -> Any:
    value = action.get(name)
    if isinstance(value, str) and "," in value:
        return _number(_at(_csv(value), index))
    if isinstance(value, list):
        return _number(_at(value, index))
    return _number(value)


def _nonempty_source_fields(action: Mapping[str, Any]) -> dict[str, Any]:
    return {key: action[key] for key in ACTION_SOURCE_FIELDS if key in action}


def normalize_workout_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only list metadata required to resolve a detail request safely."""
    return {
        "workout_id": summary.get("id") or summary.get("templateId"),
        "workout_code": summary.get("code") or summary.get("templateCode"),
        "title": summary.get("name") or summary.get("title") or "",
    }


def _identity(summary: Mapping[str, Any], detail: Mapping[str, Any]) -> tuple[Any, Any]:
    workout_id = detail.get("id") or detail.get("templateId") or summary.get("workout_id")
    workout_code = detail.get("code") or detail.get("templateCode") or summary.get("workout_code")
    if workout_id in (None, "") and workout_code in (None, ""):
        raise ExportError("custom workout has no immutable id or code")
    return workout_id, workout_code


def _exercise_sets(action: Mapping[str, Any]) -> list[dict[str, Any]]:
    reps = _csv(action.get("setsAndReps"))
    weights = _csv(action.get("weights"))
    counters = _csv(action.get("counterweight2") or action.get("counterweight"))
    modes = _csv(action.get("sportMode"))
    rests = _csv(action.get("breakTime2") or action.get("breakTime"))
    sides = _csv(action.get("leftRight"))
    completion_methods = _csv(action.get("completionMethod"))
    count_types = _csv(action.get("countType"))
    levels = _csv(action.get("level"))
    preset = _number(action.get("templatePresetId"))
    count = max(len(reps), len(weights), len(counters), len(modes), len(rests), len(sides), 0)

    result = []
    for index in range(count):
        completion_method = _number(_at(completion_methods, index))
        count_type = _number(_at(count_types, index))
        target = _number(_at(reps, index))
        is_duration = completion_method == 2 or count_type == 2
        # Preset templates use counterweight2; custom templates use weights.
        capacity = _number(_at(counters if preset not in (None, -1) else weights, index))
        result.append({
            "set_number": index + 1,
            "target_value": target,
            "repetitions": None if is_duration else target,
            "duration_seconds": target if is_duration else None,
            "capacity": capacity,
            "mode": _number(_at(modes, index)),
            "rest_seconds": _number(_at(rests, index)),
            "side": _number(_at(sides, index)),
            "completion_method": completion_method,
            "count_type": count_type,
            "level": _number(_at(levels, index)),
        })
    return result


def build_workout_definition(
    summary: Mapping[str, Any], detail: Mapping[str, Any], captured_at: str,
) -> dict[str, Any]:
    """Build the sanitized, versioned custom-workout export contract."""
    summary_info = normalize_workout_summary(summary)
    workout_id, workout_code = _identity(summary_info, detail)
    title = str(detail.get("name") or detail.get("title") or summary_info["title"] or "")
    exercises = []
    for order, action in enumerate(detail.get("actionLibraryList") or [], start=1):
        if not isinstance(action, Mapping):
            continue
        exercises.append({
            "order": order,
            "exercise_id": action.get("actionLibraryId") or action.get("id"),
            "group_id": action.get("groupId"),
            "name": action.get("title") or action.get("name") or "",
            "template_preset_id": _number(action.get("templatePresetId")),
            "is_unilateral": any(str(side) in {"1", "2"} for side in _csv(action.get("leftRight"))),
            "sets": _exercise_sets(action),
            "source_fields": _nonempty_source_fields(action),
        })

    definition = {
        "schema_version": EXPORT_SCHEMA,
        "captured_at": captured_at,
        "source": {
            "system": "speediance",
            "workout_id": workout_id,
            "workout_code": workout_code,
        },
        "workout": {
            "title": title,
            "description": detail.get("description") or detail.get("desc"),
            "exercises": exercises,
        },
    }
    definition["canonical_sha256"] = canonical_sha256(definition)
    return definition


def _hashable_definition(definition: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": definition["schema_version"],
        "source": definition["source"],
        "workout": definition["workout"],
    }


def canonical_json(value: Mapping[str, Any]) -> bytes:
    """UTF-8 JSON with sorted object keys and original array order preserved."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_sha256(definition: Mapping[str, Any]) -> str:
    """Hash definition content without capture timestamps or existing hash fields."""
    source = _hashable_definition(definition) if "schema_version" in definition else dict(definition)
    return hashlib.sha256(canonical_json(source)).hexdigest()


def safe_filename(workout_id: Any, title: str) -> str:
    raw_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(workout_id or "unknown")).strip(".-") or "unknown"
    slug = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")[:80] or "untitled"
    return f"{raw_id}__{slug}.json"


def _now(timezone_name: str, clock: Callable[[], datetime] | None = None) -> datetime:
    value = (clock or datetime.now)()
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, KeyError, ValueError, TypeError):
        # Approved timezone policy (PH-SPEEDIANCE-001 correction phase): fail
        # explicitly; never fall back silently to system-local time or UTC.
        # tzdata is a declared dependency in requirements.txt.
        raise ExportError(
            f"timezone {timezone_name!r} could not be resolved via zoneinfo; "
            "install the 'tzdata' package (see requirements.txt) or correct the name"
        )
    return value.astimezone(zone) if value.tzinfo else value.replace(tzinfo=zone)


def _snapshot_id(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H%M%S%z")


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    copy = dict(manifest)
    copy.pop("manifest_sha256", None)
    return hashlib.sha256(canonical_json(copy)).hexdigest()


def _artifact_path(root: Path, relative: str) -> Path:
    return root / relative.replace("/", os.sep)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _workout_key(entry: Mapping[str, Any]) -> str:
    source = entry.get("source") or entry
    value = source.get("workout_id") or source.get("workout_code") or entry.get("workout_id") or entry.get("workout_code")
    if value in (None, ""):
        raise ExportError("snapshot entry lacks a remote identity")
    return str(value)


def _exercise_key(exercise: Mapping[str, Any]) -> str:
    value = exercise.get("exercise_id") or exercise.get("group_id")
    return str(value) if value not in (None, "") else f"order:{exercise.get('order')}"


def _change(path: str, before: Any, after: Any) -> dict[str, Any]:
    return {"path": path, "from": before, "to": after}


def derive_workout_diff(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """Produce deterministic factual structural changes for one workout."""
    old_workout = previous["workout"]
    new_workout = current["workout"]
    changes: list[dict[str, Any]] = []
    if old_workout.get("title") != new_workout.get("title"):
        changes.append(_change("title", old_workout.get("title"), new_workout.get("title")))
    old_family, old_version = parse_title_version(old_workout.get("title") or "")
    new_family, new_version = parse_title_version(new_workout.get("title") or "")
    if (old_family, old_version) != (new_family, new_version):
        changes.append(_change("title_version", {"family": old_family, "version": old_version}, {"family": new_family, "version": new_version}))

    old_exercises = old_workout.get("exercises") or []
    new_exercises = new_workout.get("exercises") or []
    old_keys = [_exercise_key(exercise) for exercise in old_exercises]
    new_keys = [_exercise_key(exercise) for exercise in new_exercises]
    if set(old_keys) == set(new_keys) and old_keys != new_keys:
        changes.append(_change("exercises.order", old_keys, new_keys))
    for key in old_keys:
        if key not in new_keys:
            changes.append(_change(f"exercises[{key}]", "present", "removed"))
    for key in new_keys:
        if key not in old_keys:
            changes.append(_change(f"exercises[{key}]", "absent", "added"))

    old_by_key = {_exercise_key(exercise): exercise for exercise in old_exercises}
    new_by_key = {_exercise_key(exercise): exercise for exercise in new_exercises}
    # Action-library IDs may be replaced while the stable exercise group keeps
    # its position.  Preserve that as an explicit identity change, not only as
    # a remove/add pair.
    old_by_group = {str(exercise.get("group_id")): exercise for exercise in old_exercises if exercise.get("group_id") not in (None, "")}
    new_by_group = {str(exercise.get("group_id")): exercise for exercise in new_exercises if exercise.get("group_id") not in (None, "")}
    for group_id in sorted(set(old_by_group) & set(new_by_group)):
        if old_by_group[group_id].get("exercise_id") != new_by_group[group_id].get("exercise_id"):
            changes.append(_change(
                f"exercises[group_id={group_id}].exercise_id",
                old_by_group[group_id].get("exercise_id"),
                new_by_group[group_id].get("exercise_id"),
            ))
    for key in sorted(set(old_by_key) & set(new_by_key)):
        old_exercise, new_exercise = old_by_key[key], new_by_key[key]
        for field in ("exercise_id", "group_id", "name", "template_preset_id", "is_unilateral"):
            if old_exercise.get(field) != new_exercise.get(field):
                changes.append(_change(f"exercises[{key}].{field}", old_exercise.get(field), new_exercise.get(field)))
        old_sets, new_sets = old_exercise.get("sets") or [], new_exercise.get("sets") or []
        max_sets = max(len(old_sets), len(new_sets))
        for index in range(max_sets):
            path = f"exercises[{key}].sets[{index + 1}]"
            if index >= len(old_sets):
                changes.append(_change(path, "absent", "added"))
                continue
            if index >= len(new_sets):
                changes.append(_change(path, "present", "removed"))
                continue
            for field in ("target_value", "repetitions", "duration_seconds", "capacity", "mode", "rest_seconds", "side", "completion_method", "count_type", "level"):
                if old_sets[index].get(field) != new_sets[index].get(field):
                    changes.append(_change(f"{path}.{field}", old_sets[index].get(field), new_sets[index].get(field)))
    return {
        "schema_version": DIFF_SCHEMA,
        "workout_id": current["source"].get("workout_id"),
        "workout_code": current["source"].get("workout_code"),
        "from": {"title": old_workout.get("title"), "hash": previous.get("canonical_sha256")},
        "to": {"title": new_workout.get("title"), "hash": current.get("canonical_sha256")},
        "changes": changes,
    }


def _load_manifest(path: Path) -> tuple[dict[str, Any], Path]:
    manifest_path = path / "manifest.json" if path.is_dir() else path
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not data.get("completed"):
        raise ExportError(f"previous snapshot is not completed: {manifest_path}")
    return data, manifest_path.parent


def latest_completed_snapshot(output_root: Path) -> Optional[Path]:
    if not output_root.exists():
        return None
    candidates = []
    for child in output_root.iterdir():
        manifest = child / "manifest.json"
        if child.is_dir() and not child.name.startswith(".staging-") and manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if data.get("completed"):
                    candidates.append(child)
            except (OSError, ValueError):
                continue
    return sorted(candidates)[-1] if candidates else None


@dataclass(frozen=True)
class SnapshotResult:
    path: Path
    manifest: Mapping[str, Any]


class CustomWorkoutExporter:
    """State-free adapter around the existing read-only Speediance client methods."""

    def __init__(self, client: Any, *, timezone: str = DEFAULT_TIMEZONE, clock: Callable[[], datetime] | None = None):
        self.client = client
        self.timezone = timezone
        self.clock = clock

    def _capture_time(self) -> tuple[datetime, str]:
        moment = _now(self.timezone, self.clock)
        return moment, moment.isoformat()

    def list_summaries(self) -> list[dict[str, Any]]:
        summaries = []
        seen: set[str] = set()
        for raw in self.client.get_user_workouts() or []:
            summary = normalize_workout_summary(raw)
            key = str(summary["workout_id"] or summary["workout_code"] or "")
            if not key:
                raise ExportError("custom workout list item has no id or code")
            if key in seen:
                raise ExportError(f"duplicate remote workout identity: {key}")
            seen.add(key)
            if not summary["workout_code"]:
                raise ExportError(f"custom workout {key} has no code for the detail endpoint")
            summaries.append(summary)
        return summaries

    def export_one(self, summary: Mapping[str, Any]) -> dict[str, Any]:
        _, captured_at = self._capture_time()
        detail = self.client.get_workout_detail(summary["workout_code"])
        if not isinstance(detail, Mapping):
            raise ExportError(f"detail unavailable for workout code {summary['workout_code']}")
        return build_workout_definition(summary, detail, captured_at)

    def export_single(self, *, title: str | None = None, remote_id: str | None = None, remote_code: str | None = None) -> dict[str, Any]:
        selectors = [value for value in (title, remote_id, remote_code) if value]
        if len(selectors) != 1:
            raise ExportError("provide exactly one of title, remote_id, or remote_code")
        matches = [
            summary for summary in self.list_summaries()
            if (title and summary["title"] == title)
            or (remote_id and str(summary["workout_id"]) == str(remote_id))
            or (remote_code and str(summary["workout_code"]) == str(remote_code))
        ]
        if len(matches) != 1:
            raise ExportError(f"exact lookup requires one match; found {len(matches)}")
        return self.export_one(matches[0])

    def export_all(self, output_root: Path, *, previous_snapshot: Path | None = None, fail_fast: bool = False) -> SnapshotResult:
        output_root = Path(output_root)
        moment, captured_at = self._capture_time()
        snapshot_id = _snapshot_id(moment)
        final_path = output_root / snapshot_id
        if final_path.exists():
            raise ExportError(f"snapshot already exists: {final_path}")
        output_root.mkdir(parents=True, exist_ok=True)
        staging = output_root / f".staging-{snapshot_id}-{uuid.uuid4().hex[:8]}"
        staging.mkdir(parents=False)

        previous_root: Optional[Path] = None
        previous_manifest: Optional[dict[str, Any]] = None
        if previous_snapshot:
            previous_manifest, previous_root = _load_manifest(Path(previous_snapshot))
        else:
            latest = latest_completed_snapshot(output_root)
            if latest:
                previous_manifest, previous_root = _load_manifest(latest)
        previous_entries = {str(entry["workout_id"] or entry["workout_code"]): entry for entry in (previous_manifest or {}).get("workouts", [])}
        previous_definitions: dict[str, Mapping[str, Any]] = {}
        if previous_root:
            for entry in previous_entries.values():
                try:
                    previous_definitions[str(entry["workout_id"] or entry["workout_code"])] = json.loads(_artifact_path(previous_root, entry["file"]).read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise ExportError(f"cannot read previous workout artifact: {exc}") from exc

        summaries = self.list_summaries()
        manifest: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA,
            "snapshot_id": snapshot_id,
            "captured_at": captured_at,
            "timezone": self.timezone,
            "source_count": len(summaries),
            "exported_count": 0,
            "failed_count": 0,
            "new_count": 0,
            "unchanged_count": 0,
            "changed_count": 0,
            "completed": False,
            "workouts": [],
            "missing_from_remote": [],
            "failures": [],
        }

        seen_keys: set[str] = set()
        for summary in summaries:
            list_key = str(summary["workout_id"] or summary["workout_code"])
            try:
                definition = self.export_one(summary)
                key = _workout_key(definition)
                if key in seen_keys:
                    raise ExportError(f"duplicate resolved remote identity: {key}")
                seen_keys.add(key)
                previous_entry = previous_entries.get(key)
                if previous_entry is None:
                    status = "new"
                elif previous_entry.get("canonical_sha256") == definition["canonical_sha256"]:
                    status = "unchanged"
                else:
                    status = "changed"
                relative_file = f"workouts/{safe_filename(definition['source'].get('workout_id') or definition['source'].get('workout_code'), definition['workout']['title'])}"
                _write_json(_artifact_path(staging, relative_file), definition)
                entry = {
                    "workout_id": definition["source"].get("workout_id"),
                    "workout_code": definition["source"].get("workout_code"),
                    "title": definition["workout"]["title"],
                    "title_family": parse_title_version(definition["workout"]["title"])[0],
                    "title_version": parse_title_version(definition["workout"]["title"])[1],
                    "canonical_sha256": definition["canonical_sha256"],
                    "file": relative_file,
                    "previous_snapshot_hash": previous_entry.get("canonical_sha256") if previous_entry else None,
                    "change_status": status,
                }
                if status == "changed":
                    diff = derive_workout_diff(previous_definitions[key], definition)
                    change_file = f"changes/{safe_filename(definition['source'].get('workout_id') or definition['source'].get('workout_code'), definition['workout']['title'])}"
                    _write_json(_artifact_path(staging, change_file), diff)
                    entry["change_file"] = change_file
                manifest["workouts"].append(entry)
                manifest["exported_count"] += 1
                manifest[f"{status}_count"] += 1
            except Exception as exc:
                manifest["failed_count"] += 1
                manifest["failures"].append({"workout_id": summary.get("workout_id"), "workout_code": summary.get("workout_code"), "error": str(exc)})
                if fail_fast:
                    _write_json(staging / "manifest.json", manifest)
                    raise

        for key, entry in previous_entries.items():
            if key not in seen_keys:
                missing = dict(entry)
                missing["change_status"] = "missing_from_remote"
                manifest["missing_from_remote"].append(missing)
                previous_definition = previous_definitions.get(key)
                if previous_definition:
                    diff = {
                        "schema_version": DIFF_SCHEMA,
                        "workout_id": entry.get("workout_id"),
                        "workout_code": entry.get("workout_code"),
                        "from": {"title": entry.get("title"), "hash": entry.get("canonical_sha256")},
                        "to": None,
                        "changes": [_change("workout", "present", "missing_from_remote")],
                    }
                    missing_file = f"changes/{safe_filename(entry.get('workout_id') or entry.get('workout_code'), entry.get('title') or 'missing')}"
                    _write_json(_artifact_path(staging, missing_file), diff)
                    missing["change_file"] = missing_file

        manifest["completed"] = manifest["failed_count"] == 0
        manifest["status"] = "complete" if manifest["completed"] else "partial"
        manifest["manifest_sha256"] = _manifest_hash(manifest)
        _write_json(staging / "manifest.json", manifest)
        validate_manifest(staging, manifest)
        os.replace(staging, final_path)
        return SnapshotResult(path=final_path, manifest=manifest)


def validate_manifest(snapshot_path: Path, manifest: Mapping[str, Any] | None = None) -> None:
    """Verify that every artifact referenced by a finalized manifest exists."""
    manifest = dict(manifest or json.loads((snapshot_path / "manifest.json").read_text(encoding="utf-8")))
    for entry in manifest.get("workouts", []):
        if not _artifact_path(snapshot_path, entry["file"]).is_file():
            raise ExportError(f"missing workout artifact: {entry['file']}")
        if entry.get("change_file") and not _artifact_path(snapshot_path, entry["change_file"]).is_file():
            raise ExportError(f"missing change artifact: {entry['change_file']}")
    for entry in manifest.get("missing_from_remote", []):
        if entry.get("change_file") and not _artifact_path(snapshot_path, entry["change_file"]).is_file():
            raise ExportError(f"missing missing-from-remote artifact: {entry['change_file']}")


def _credentials_present(client: Any) -> bool:
    credentials = getattr(client, "credentials", {}) or {}
    return bool(credentials.get("user_id") and credentials.get("token"))


def _make_client() -> SpeedianceClient:
    """Client factory honoring the repo-wide SPEEDIANCE_USER_ID/SPEEDIANCE_TOKEN
    environment override convention (see workout_ops.py, test_e2e_workouts.py)."""
    client = SpeedianceClient()
    env_uid = os.environ.get("SPEEDIANCE_USER_ID")
    env_tok = os.environ.get("SPEEDIANCE_TOKEN")
    if env_uid and env_tok:
        client.credentials["user_id"] = env_uid
        client.credentials["token"] = env_tok
    return client


def _cli_export_all(args: argparse.Namespace) -> int:
    client = _make_client()
    if not _credentials_present(client):
        print("credentials unavailable; no remote request attempted")
        return 2
    exporter = CustomWorkoutExporter(client, timezone=args.timezone)
    try:
        result = exporter.export_all(Path(args.output_root), previous_snapshot=Path(args.previous_snapshot) if args.previous_snapshot else None, fail_fast=args.fail_fast)
    except Exception as exc:
        print(f"export failed safely: {exc}")
        return 1
    manifest = result.manifest
    print(json.dumps({
        "snapshot_path": str(result.path), "source_count": manifest["source_count"],
        "exported_count": manifest["exported_count"], "failed_count": manifest["failed_count"],
        "new_count": manifest["new_count"], "unchanged_count": manifest["unchanged_count"],
        "changed_count": manifest["changed_count"], "completed": manifest["completed"],
    }))
    return 0 if manifest["completed"] else 1


def _cli_export_single(args: argparse.Namespace) -> int:
    client = _make_client()
    if not _credentials_present(client):
        print("credentials unavailable; no remote request attempted")
        return 2
    try:
        definition = CustomWorkoutExporter(client, timezone=args.timezone).export_single(title=args.title, remote_id=args.id, remote_code=args.code)
        output = Path(args.output)
        _write_json(output, definition)
        print(json.dumps({"output": str(output), "workout_id": definition["source"].get("workout_id"), "canonical_sha256": definition["canonical_sha256"]}))
        return 0
    except Exception as exc:
        print(f"single export failed safely: {exc}")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Speediance custom-workout exporter")
    subcommands = parser.add_subparsers(dest="command", required=True)
    bulk = subcommands.add_parser("export-all-custom-workouts")
    bulk.add_argument("--output-root", default="runtime/custom-workout-snapshots")
    bulk.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    bulk.add_argument("--previous-snapshot")
    bulk.add_argument("--fail-fast", action="store_true")
    bulk.add_argument("--include-unchanged", action="store_true", help="accepted for automation compatibility; artifacts are always complete snapshots")
    bulk.set_defaults(handler=_cli_export_all)
    single = subcommands.add_parser("export-custom-workout")
    selector = single.add_mutually_exclusive_group(required=True)
    selector.add_argument("--title")
    selector.add_argument("--id")
    selector.add_argument("--code")
    single.add_argument("--output", default="runtime/custom-workout-export.json")
    single.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    single.set_defaults(handler=_cli_export_single)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
