"""
Unit tests for workout_update.py — no real API calls.

Fixture data mirrors the schema captured in evidence/speediance/sanitized/
(05_custom_template_app_page.json, 07_custom_template_detail_by_code_nonzero.json):
exercise ID 321 = groupId of "Barbell Bent Over Row", per-set values live in
CSV strings (weights/setsAndReps/breakTime2/sportMode/...).
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import workout_update as wu
from workout_update import WorkoutUpdateError


WORKOUT_ID = 1345252
WORKOUT_CODE = "aabbccddeeff0011"


def make_detail():
    """An I-BK-v8-shaped template detail: stretch + Barbell Bent Over Row (321)
    + another weighted exercise that also has 49s which must NOT change."""
    return {
        "id": WORKOUT_ID,
        "name": "I-BK-v8",
        "code": WORKOUT_CODE,
        "totalCapacity": 2744.0,
        "deviceType": 1,
        "bgColor": 0,
        "actionLibraryList": [
            {
                "id": 91001, "templateId": WORKOUT_ID, "sort": 1,
                "groupId": 522, "actionLibraryId": 386, "templatePresetId": -1,
                "title": "Standing Back Neck Stretch",
                "setsAndReps": "30", "breakTime2": "15", "weights": "0",
                "level": "0", "sportMode": "1", "leftRight": "0",
                "selectCompletionMethod": "1", "counterweight2": "",
                "capacity": 0.0, "completionMethod": 0,
            },
            {
                "id": 91002, "templateId": WORKOUT_ID, "sort": 2,
                "groupId": 321, "actionLibraryId": 424929027751937,
                "templatePresetId": -1,
                "title": "Barbell Bent Over Row",
                "setsAndReps": "12,12,12", "breakTime2": "90,90,90",
                "weights": "49,49,49", "level": "0,0,0", "sportMode": "1,1,1",
                "leftRight": "0,0,0", "selectCompletionMethod": "1,1,1",
                "counterweight2": "", "capacity": 1764.0, "completionMethod": 1,
            },
            {
                "id": 91003, "templateId": WORKOUT_ID, "sort": 3,
                "groupId": 999, "actionLibraryId": 555000111,
                "templatePresetId": -1,
                "title": "Some Other Row",
                "setsAndReps": "10,10", "breakTime2": "60,60",
                "weights": "49,49", "level": "0,0", "sportMode": "1,1",
                "leftRight": "0,0", "selectCompletionMethod": "1,1",
                "counterweight2": "", "capacity": 980.0, "completionMethod": 1,
            },
        ],
    }


def make_workouts():
    return [
        {"id": WORKOUT_ID, "name": "I-BK-v8", "code": WORKOUT_CODE},
        {"id": 222, "name": "Leg Day", "code": "ff00ff00ff00"},
    ]


class FakeClient:
    """Simulates the SpeedianceClient surface used by workout_update.
    save_workout_payload applies the payload to internal state like the server
    would (name + per-set CSVs), so fetch-back verification can be exercised."""

    def __init__(self, workouts=None, detail=None):
        self.credentials = {"user_id": "fake-user-id-123", "token": "fake-token-value-abc"}
        self.device_type = 1
        self._workouts = workouts if workouts is not None else make_workouts()
        self._detail = detail if detail is not None else make_detail()
        self.write_calls = []
        self.mutate_on_save = None  # optional hook: simulates a misbehaving server

    def get_user_workouts(self):
        return copy.deepcopy(self._workouts)

    def get_workout_detail(self, code):
        if code == self._detail.get("code"):
            return copy.deepcopy(self._detail)
        return None

    def save_workout_payload(self, payload):
        self.write_calls.append(copy.deepcopy(payload))
        self._detail["name"] = payload["name"]
        by_group = {a["groupId"]: a for a in payload["actionLibraryList"]}
        for action in self._detail["actionLibraryList"]:
            sent = by_group.get(action["groupId"])
            if sent:
                action["weights"] = sent["weights"]
                action["setsAndReps"] = sent["setsAndReps"]
                action["breakTime2"] = sent["breakTime2"]
                action["sportMode"] = sent["sportMode"]
                action["capacity"] = sent["capacity"]
        for w in self._workouts:
            if w["id"] == payload["id"]:
                w["name"] = payload["name"]
        if self.mutate_on_save:
            self.mutate_on_save(self._detail)
        return {"code": 0, "message": "Success"}


def run(client, tmpdir, apply=False, **overrides):
    kwargs = dict(
        title="I-BK-v8", new_title="I-BK-v9", exercise_id=321,
        expected_capacity=49, new_capacity=51,
        apply=apply, runtime_dir=tmpdir,
    )
    kwargs.update(overrides)
    return wu.run_update(client, **kwargs)


class WorkoutUpdateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)


class TestLookupValidation(WorkoutUpdateTestCase):
    def test_exact_title_match(self):
        result = run(FakeClient(), self.tmpdir)
        self.assertEqual(result["plan"]["workout_id"], WORKOUT_ID)
        self.assertEqual(result["plan"]["source_title"], "I-BK-v8")

    def test_zero_title_match_rejected(self):
        client = FakeClient(workouts=[{"id": 1, "name": "I-BK-V8", "code": "x"}])
        with self.assertRaisesRegex(WorkoutUpdateError, "No saved workout"):
            run(client, self.tmpdir)

    def test_multiple_title_match_rejected(self):
        workouts = make_workouts() + [{"id": 333, "name": "I-BK-v8", "code": "dupe"}]
        with self.assertRaisesRegex(WorkoutUpdateError, "2 saved workouts"):
            run(FakeClient(workouts=workouts), self.tmpdir)

    def test_existing_target_title_rejected(self):
        workouts = make_workouts() + [{"id": 444, "name": "I-BK-v9", "code": "taken"}]
        with self.assertRaisesRegex(WorkoutUpdateError, "already uses the title"):
            run(FakeClient(workouts=workouts), self.tmpdir)


class TestExerciseValidation(WorkoutUpdateTestCase):
    def test_exact_exercise_id_match(self):
        result = run(FakeClient(), self.tmpdir)
        self.assertEqual(result["plan"]["exercise_title"], "Barbell Bent Over Row")
        self.assertEqual(result["plan"]["action_index"], 1)

    def test_missing_exercise_rejected(self):
        with self.assertRaisesRegex(WorkoutUpdateError, "not found"):
            run(FakeClient(), self.tmpdir, exercise_id=32149454)

    def test_duplicate_exercise_rejected(self):
        detail = make_detail()
        dupe = copy.deepcopy(detail["actionLibraryList"][1])
        dupe["sort"] = 4
        detail["actionLibraryList"].append(dupe)
        with self.assertRaisesRegex(WorkoutUpdateError, "appears 2 times"):
            run(FakeClient(detail=detail), self.tmpdir)

    def test_expected_capacity_validation(self):
        with self.assertRaisesRegex(WorkoutUpdateError, "exactly 47"):
            run(FakeClient(), self.tmpdir, expected_capacity=47)


class TestPlanSemantics(WorkoutUpdateTestCase):
    def test_only_matching_sets_change(self):
        detail = make_detail()
        detail["actionLibraryList"][1]["weights"] = "49,52,49"
        detail["actionLibraryList"][1]["capacity"] = 1800.0
        result = run(FakeClient(detail=detail), self.tmpdir)
        self.assertEqual(result["plan"]["new_weights_csv"], "51,52,51")
        self.assertEqual(result["plan"]["sets_changed"], [1, 3])

    def test_repetitions_unchanged(self):
        client = FakeClient()
        with patch.dict(os.environ, {wu.WRITE_ENABLE_ENV: "true"}):
            run(client, self.tmpdir, apply=True)
        sent = client.write_calls[0]
        target = [a for a in sent["actionLibraryList"] if a["groupId"] == 321][0]
        self.assertEqual(target["setsAndReps"], "12,12,12")
        self.assertEqual(target["weights"], "51,51,51")

    def test_mode_and_rest_unchanged(self):
        client = FakeClient()
        with patch.dict(os.environ, {wu.WRITE_ENABLE_ENV: "true"}):
            run(client, self.tmpdir, apply=True)
        sent = client.write_calls[0]
        target = [a for a in sent["actionLibraryList"] if a["groupId"] == 321][0]
        self.assertEqual(target["sportMode"], "1,1,1")
        self.assertEqual(target["breakTime2"], "90,90,90")
        self.assertEqual(target["breakTime"], "90,90,90")

    def test_exercise_order_unchanged(self):
        client = FakeClient()
        with patch.dict(os.environ, {wu.WRITE_ENABLE_ENV: "true"}):
            run(client, self.tmpdir, apply=True)
        sent = client.write_calls[0]
        self.assertEqual(
            [a["groupId"] for a in sent["actionLibraryList"]], [522, 321, 999]
        )

    def test_other_exercises_unchanged(self):
        client = FakeClient()
        with patch.dict(os.environ, {wu.WRITE_ENABLE_ENV: "true"}):
            run(client, self.tmpdir, apply=True)
        sent = client.write_calls[0]
        other = [a for a in sent["actionLibraryList"] if a["groupId"] == 999][0]
        # groupId 999 also has 49s — they must stay 49.
        self.assertEqual(other["weights"], "49,49")
        self.assertEqual(other["setsAndReps"], "10,10")
        self.assertEqual(other["breakTime2"], "60,60")
        stretch = [a for a in sent["actionLibraryList"] if a["groupId"] == 522][0]
        self.assertEqual(stretch["weights"], "0")

    def test_title_updated_v8_to_v9(self):
        client = FakeClient()
        with patch.dict(os.environ, {wu.WRITE_ENABLE_ENV: "true"}):
            result = run(client, self.tmpdir, apply=True)
        self.assertEqual(client.write_calls[0]["name"], "I-BK-v9")
        self.assertEqual(result["outcome"], "LIVE UPDATE VERIFIED")

    def test_capacity_totals_shift_by_delta_only(self):
        result = run(FakeClient(), self.tmpdir)
        # 3 sets x 12 reps x (51-49) = 72
        self.assertEqual(result["plan"]["new_action_capacity"], 1764.0 + 72)
        self.assertEqual(result["plan"]["new_total_capacity"], 2744.0 + 72)


class TestInterlocks(WorkoutUpdateTestCase):
    def test_dry_run_performs_no_network_write(self):
        client = FakeClient()
        with patch.dict(os.environ, {wu.WRITE_ENABLE_ENV: "true"}):
            result = run(client, self.tmpdir, apply=False)
        self.assertEqual(client.write_calls, [])
        self.assertEqual(result["mode"], "dry-run")

    def test_apply_without_env_interlock_rejected(self):
        client = FakeClient()
        env = {k: v for k, v in os.environ.items() if k != wu.WRITE_ENABLE_ENV}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(WorkoutUpdateError, wu.WRITE_ENABLE_ENV):
                run(client, self.tmpdir, apply=True)
        self.assertEqual(client.write_calls, [])

    def test_env_interlock_without_apply_stays_dry_run(self):
        client = FakeClient()
        with patch.dict(os.environ, {wu.WRITE_ENABLE_ENV: "true"}):
            result = run(client, self.tmpdir, apply=False)
        self.assertEqual(client.write_calls, [])
        self.assertEqual(result["outcome"], "DRY-RUN ONLY - no write performed")

    def test_missing_auth_rejected_before_lookup(self):
        client = FakeClient()
        client.credentials = {"user_id": "", "token": ""}
        with self.assertRaisesRegex(WorkoutUpdateError, "Authentication"):
            run(client, self.tmpdir)


class TestArtifacts(WorkoutUpdateTestCase):
    def test_plan_contains_no_credentials(self):
        client = FakeClient()
        result = run(client, self.tmpdir)
        for path in (result["plan_path"], result["backup_path"]):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn(client.credentials["token"], text)
            self.assertNotIn(client.credentials["user_id"], text)
            lowered = text.lower()
            for banned in ('"token"', '"authorization"', '"cookie"', '"password"'):
                self.assertNotIn(banned, lowered)

    def test_plan_artifact_schema(self):
        result = run(FakeClient(), self.tmpdir)
        with open(result["plan_path"], encoding="utf-8") as f:
            artifact = json.load(f)
        self.assertEqual(artifact["schema_version"], wu.PLAN_SCHEMA_VERSION)
        self.assertEqual(artifact["operation"], "update_saved_workout")
        self.assertEqual(artifact["source_workout"]["title"], "I-BK-v8")
        self.assertEqual(artifact["target_title"], "I-BK-v9")
        self.assertFalse(artifact["apply_requested"])
        change = artifact["changes"][0]
        self.assertEqual(change["exercise_id"], "321")
        self.assertEqual(change["field"], "weights")
        self.assertEqual(change["from"], 49)
        self.assertEqual(change["to"], 51)
        self.assertEqual(change["sets_changed"], [1, 2, 3])
        self.assertIn("repetitions", artifact["preserved"])

    def test_backup_sufficient_for_rollback(self):
        result = run(FakeClient(), self.tmpdir)
        with open(result["backup_path"], encoding="utf-8") as f:
            backup = json.load(f)
        restore = backup["restore_payload"]
        self.assertEqual(restore["name"], "I-BK-v8")
        self.assertEqual(restore["id"], WORKOUT_ID)
        target = [a for a in restore["actionLibraryList"] if a["groupId"] == 321][0]
        self.assertEqual(target["weights"], "49,49,49")


class TestFetchBackVerification(WorkoutUpdateTestCase):
    def test_fetch_back_detects_unrelated_mutation(self):
        before = make_detail()
        plan = wu.build_update_plan(before, 321, 49, 51, "I-BK-v9")
        after = copy.deepcopy(before)
        after["name"] = "I-BK-v9"
        after["actionLibraryList"][1]["weights"] = "51,51,51"
        # Unrelated mutation: rest time of another exercise changed.
        after["actionLibraryList"][2]["breakTime2"] = "45,60"
        ok, mismatches = wu.verify_fetch_back(before, after, plan)
        self.assertFalse(ok)
        self.assertTrue(any("breakTime2" in m for m in mismatches))

    def test_fetch_back_accepts_exact_planned_change(self):
        before = make_detail()
        plan = wu.build_update_plan(before, 321, 49, 51, "I-BK-v9")
        after = copy.deepcopy(before)
        after["name"] = "I-BK-v9"
        after["actionLibraryList"][1]["weights"] = "51,51,51"
        ok, mismatches = wu.verify_fetch_back(before, after, plan)
        self.assertTrue(ok, mismatches)

    def test_rollback_on_server_side_mutation(self):
        client = FakeClient()

        def corrupt(detail):
            # Server "helpfully" rewrites another exercise's rest — once.
            detail["actionLibraryList"][2]["breakTime2"] = "45,60"
            client.mutate_on_save = None

        client.mutate_on_save = corrupt
        with patch.dict(os.environ, {wu.WRITE_ENABLE_ENV: "true"}):
            result = run(client, self.tmpdir, apply=True)
        self.assertEqual(result["outcome"], "LIVE UPDATE ROLLED BACK")
        self.assertEqual(len(client.write_calls), 2)
        self.assertEqual(client._detail["name"], "I-BK-v8")


if __name__ == "__main__":
    unittest.main()
