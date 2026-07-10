"""Focused mock-only tests for the read-only custom-workout export layer."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_workout_export import (
    CustomWorkoutExporter,
    ExportError,
    _load_manifest,
    build_workout_definition,
    canonical_sha256,
    derive_workout_diff,
    normalize_workout_summary,
    parse_title_version,
    validate_manifest,
)


def summary(workout_id="101", code="CODE101", title="I-BK-v9"):
    return {"id": workout_id, "code": code, "name": title, "accountId": "must-not-appear"}


def detail(workout_id="101", code="CODE101", title="I-BK-v9"):
    return {
        "id": workout_id, "code": code, "name": title,
        "description": "Back and biceps", "token": "must-not-appear", "appUserId": "must-not-appear",
        "actionLibraryList": [
            {"groupId": 321, "actionLibraryId": 9001, "title": "Barbell Bent Over Row", "templatePresetId": -1,
             "setsAndReps": "12,10", "weights": "51,52", "counterweight2": "", "sportMode": "1,2",
             "breakTime2": "90,75", "leftRight": "0,0", "completionMethod": "1,1", "countType": "1,1", "capacity": 1132},
            {"groupId": 555, "actionLibraryId": 9002, "title": "Timed Core Hold", "templatePresetId": -1,
             "setsAndReps": "30", "weights": "0", "sportMode": "1", "breakTime2": "60", "leftRight": "1",
             "completionMethod": "2", "countType": "2"},
        ],
    }


class FakeClient:
    def __init__(self, summaries, details):
        self.summaries = summaries
        self.details = details
        self.detail_calls = []
        self.credentials = {"user_id": "fake", "token": "fake"}

    def get_user_workouts(self):
        return deepcopy(self.summaries)

    def get_workout_detail(self, code):
        self.detail_calls.append(code)
        return deepcopy(self.details.get(code))

    def save_workout(self, *args, **kwargs):
        raise AssertionError("write method must never be called")


def exporter(client, second=0):
    return CustomWorkoutExporter(client, clock=lambda: datetime(2026, 7, 10, 0, 15, second, tzinfo=timezone.utc))


class TestContractAndVersioning(unittest.TestCase):
    def test_dashboard_summary_and_detail_mapping_preserve_orders(self):
        self.assertEqual(normalize_workout_summary(summary()), {"workout_id": "101", "workout_code": "CODE101", "title": "I-BK-v9"})
        exported = build_workout_definition(summary(), detail(), "time")
        exercises = exported["workout"]["exercises"]
        self.assertEqual([ex["group_id"] for ex in exercises], [321, 555])
        self.assertEqual([item["set_number"] for item in exercises[0]["sets"]], [1, 2])
        self.assertEqual(exercises[0]["sets"][0]["capacity"], 51)
        self.assertEqual(exercises[0]["sets"][1]["rest_seconds"], 75)
        self.assertEqual(exercises[1]["sets"][0]["duration_seconds"], 30)
        self.assertIsNone(exercises[1]["sets"][0]["repetitions"])

    def test_sensitive_fields_are_excluded(self):
        artifact = json.dumps(build_workout_definition(summary(), detail(), "time"))
        self.assertNotIn("must-not-appear", artifact)
        self.assertNotIn("appUserId", artifact)
        self.assertNotIn("accountId", artifact)

    def test_contract_retains_remote_identity_and_builder_fields(self):
        exported = build_workout_definition(summary(), detail(), "time")
        self.assertEqual(exported["source"]["workout_id"], "101")
        self.assertEqual(exported["source"]["workout_code"], "CODE101")
        action = exported["workout"]["exercises"][0]
        self.assertEqual(action["exercise_id"], 9001)
        self.assertEqual(action["source_fields"]["setsAndReps"], "12,10")
        self.assertEqual(action["source_fields"]["breakTime2"], "90,75")
        self.assertEqual(action["source_fields"]["completionMethod"], "1,1")

    def test_terminal_title_versions_only(self):
        self.assertEqual(parse_title_version("I-BK-v9"), ("I-BK", 9))
        self.assertEqual(parse_title_version("I-BK-v10"), ("I-BK", 10))
        self.assertEqual(parse_title_version("B REC-LB 1/1 v1"), ("B REC-LB 1/1", 1))
        self.assertEqual(parse_title_version("My Workout 2026"), ("My Workout 2026", None))
        self.assertEqual(parse_title_version("Plan-vx"), ("Plan-vx", None))

    def test_hash_is_timestamp_and_key_order_stable_but_array_order_sensitive(self):
        first = build_workout_definition(summary(), detail(), "first")
        second = build_workout_definition(summary(), detail(), "second")
        self.assertEqual(canonical_sha256(first), canonical_sha256(second))
        reordered_keys = {"workout": first["workout"], "source": first["source"], "schema_version": first["schema_version"]}
        self.assertEqual(canonical_sha256(first), canonical_sha256(reordered_keys))
        changed = deepcopy(first)
        changed["workout"]["exercises"].reverse()
        self.assertNotEqual(canonical_sha256(first), canonical_sha256(changed))
        changed = deepcopy(first)
        changed["workout"]["exercises"][0]["sets"].reverse()
        self.assertNotEqual(canonical_sha256(first), canonical_sha256(changed))


class TestDiffs(unittest.TestCase):
    def test_diff_detects_title_order_set_and_capacity_changes(self):
        old = build_workout_definition(summary(), detail(), "old")
        revised = detail(title="I-BK-v10")
        revised["actionLibraryList"][0]["setsAndReps"] = "14,10,8"
        revised["actionLibraryList"][0]["weights"] = "55,52,60"
        revised["actionLibraryList"][0]["sportMode"] = "2,2,1"
        revised["actionLibraryList"][0]["breakTime2"] = "120,75,60"
        revised["actionLibraryList"].reverse()
        new = build_workout_definition(summary(title="I-BK-v10"), revised, "new")
        paths = {item["path"] for item in derive_workout_diff(old, new)["changes"]}
        self.assertIn("title", paths)
        self.assertIn("title_version", paths)
        self.assertIn("exercises.order", paths)
        self.assertTrue(any(path.endswith(".repetitions") for path in paths))
        self.assertTrue(any(path.endswith(".capacity") for path in paths))
        self.assertTrue(any(path.endswith(".mode") for path in paths))
        self.assertTrue(any(path.endswith(".rest_seconds") for path in paths))
        self.assertTrue(any("sets[3]" in path for path in paths))

    def test_diff_detects_exercise_removal_and_identity_change(self):
        old = build_workout_definition(summary(), detail(), "old")
        revised = detail()
        revised["actionLibraryList"] = [revised["actionLibraryList"][0]]
        revised["actionLibraryList"][0]["actionLibraryId"] = 9999
        new = build_workout_definition(summary(), revised, "new")
        paths = {item["path"] for item in derive_workout_diff(old, new)["changes"]}
        self.assertTrue(any("9002" in path for path in paths))
        self.assertTrue(any(path.endswith("exercise_id") for path in paths))

    def test_diff_detects_exercise_addition_and_has_no_false_changes(self):
        old = build_workout_definition(summary(), detail(), "old")
        same = build_workout_definition(summary(), detail(), "new")
        self.assertEqual(derive_workout_diff(old, same)["changes"], [])
        revised = detail()
        revised["actionLibraryList"].append({"groupId": 777, "actionLibraryId": 9003, "title": "New Exercise", "templatePresetId": -1, "setsAndReps": "8", "weights": "20", "sportMode": "1", "breakTime2": "60", "leftRight": "0", "completionMethod": "1", "countType": "1"})
        new = build_workout_definition(summary(), revised, "new")
        self.assertTrue(any(item["path"].endswith("9003]") and item["to"] == "added" for item in derive_workout_diff(old, new)["changes"]))


class TestSnapshots(unittest.TestCase):
    def test_first_identical_changed_and_missing_snapshots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "runtime" / "custom-workout-snapshots"
            client = FakeClient([summary()], {"CODE101": detail()})
            first = exporter(client, 0).export_all(root)
            self.assertTrue(first.manifest["completed"])
            self.assertEqual(first.manifest["workouts"][0]["change_status"], "new")
            validate_manifest(first.path)
            second = exporter(client, 1).export_all(root)
            self.assertEqual(second.manifest["workouts"][0]["change_status"], "unchanged")
            client.details["CODE101"]["actionLibraryList"][0]["weights"] = "55,52"
            third = exporter(client, 2).export_all(root)
            changed = third.manifest["workouts"][0]
            self.assertEqual(changed["change_status"], "changed")
            self.assertTrue((third.path / changed["change_file"]).is_file())
            client.summaries = []
            fourth = exporter(client, 3).export_all(root)
            self.assertEqual(fourth.manifest["missing_from_remote"][0]["change_status"], "missing_from_remote")

    def test_manifest_artifacts_are_valid_and_secret_free(self):
        with tempfile.TemporaryDirectory() as temp:
            result = exporter(FakeClient([summary()], {"CODE101": detail()})).export_all(Path(temp))
            validate_manifest(result.path)
            content = "\n".join(path.read_text(encoding="utf-8") for path in result.path.rglob("*.json"))
            self.assertNotIn("must-not-appear", content)
            self.assertNotIn('"token"', content)

    def test_incomplete_staging_is_not_a_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            staging = Path(temp) / ".staging-broken"
            staging.mkdir()
            (staging / "manifest.json").write_text(json.dumps({"completed": False}), encoding="utf-8")
            with self.assertRaises(ExportError):
                _load_manifest(staging)

    def test_runtime_artifacts_are_gitignored(self):
        # The primary tree ignores all runtime artifacts with a single broad
        # `runtime/` rule (covering custom-workout-snapshots/ and
        # custom-workout-export.json); narrower per-path rules are equivalent.
        ignored = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
        rules = {line.strip() for line in ignored.splitlines()}
        self.assertTrue(
            "runtime/" in rules
            or {"runtime/custom-workout-snapshots/", "runtime/custom-workout-export.json"} <= rules,
            "snapshot/export runtime artifacts must be git-ignored",
        )

    def test_duplicate_ids_title_lookup_and_write_methods_fail_safely(self):
        with self.assertRaises(ExportError):
            exporter(FakeClient([summary(), summary(code="CODE102")], {"CODE101": detail(), "CODE102": detail(code="CODE102")})).list_summaries()
        client = FakeClient([summary(), summary(workout_id="102", code="CODE102")], {"CODE101": detail(), "CODE102": detail("102", "CODE102")})
        with self.assertRaises(ExportError):
            exporter(client).export_single(title="I-BK-v9")
        with self.assertRaises(ExportError):
            exporter(client).export_single(title="missing")
        client = FakeClient([summary(), summary(workout_id="102", code="CODE102", title="I-AR-v1")], {"CODE101": detail(), "CODE102": detail("102", "CODE102", "I-AR-v1")})
        with tempfile.TemporaryDirectory() as temp:
            exporter(client).export_all(Path(temp))
        self.assertEqual(client.detail_calls, ["CODE101", "CODE102"])

    def test_single_export_accepts_exact_id_or_code_only(self):
        client = FakeClient([summary()], {"CODE101": detail()})
        by_id = exporter(client).export_single(remote_id="101")
        by_code = exporter(client).export_single(remote_code="CODE101")
        self.assertEqual(by_id["canonical_sha256"], by_code["canonical_sha256"])


class TestPrimaryTreeCompatibility(unittest.TestCase):
    """Integration-phase patches: strict timezone policy and the repo-wide
    environment-credential convention."""

    def test_unresolvable_timezone_fails_explicitly(self):
        # Approved policy: no silent fallback to system-local time or UTC.
        import custom_workout_export as cwe
        with self.assertRaisesRegex(ExportError, "could not be resolved"):
            cwe._now("Not/AZone")
        broken = FakeClient([summary()], {"CODE101": detail()})
        with self.assertRaisesRegex(ExportError, "could not be resolved"):
            CustomWorkoutExporter(broken, timezone="Not/AZone").export_one(
                {"workout_code": "CODE101"})

    def test_cli_client_honors_env_credential_convention(self):
        import os
        from unittest.mock import patch
        import custom_workout_export as cwe
        fake = FakeClient([], {})
        fake.credentials = {"user_id": "", "token": ""}
        env = {"SPEEDIANCE_USER_ID": "env-user", "SPEEDIANCE_TOKEN": "env-token"}
        with patch.object(cwe, "SpeedianceClient", return_value=fake):
            with patch.dict(os.environ, env):
                client = cwe._make_client()
        self.assertEqual(client.credentials["user_id"], "env-user")
        self.assertEqual(client.credentials["token"], "env-token")


if __name__ == "__main__":
    unittest.main()
