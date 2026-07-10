"""
Unit tests for workout_ops.py — no real API calls.

Calendar fixtures mirror the monthNew shape consumed by templates/index.html
(day dicts with `date` + `trainingPlanList` entries carrying code/templateCode,
title, isReservation). Reservation mutations mirror the observed
templateReservation contract (status 1|0 keyed by thatDay + templateCode).
Multiple workouts on one date are valid; the schedule-entry identity key is
(thatDay, templateCode).
"""
import calendar as pycal
import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfoNotFoundError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))
import workout_ops as wo
import test_workout_update as tu
from workout_update import WorkoutUpdateError

V9 = {"id": 501, "name": "I-BK-v9", "code": "code-v9"}
V7 = {"id": 502, "name": "I-BK-v7", "code": "code-v7"}
LEG = {"id": 503, "name": "Leg Day", "code": "code-leg"}

SCHED_DATE = "2099-07-11"
SCHED_MONTH = "2099-07"


def make_workouts():
    return [copy.deepcopy(V9), copy.deepcopy(V7), copy.deepcopy(LEG)]


def make_v7_detail():
    d = tu.make_detail()
    d["id"] = V7["id"]
    d["name"] = V7["name"]
    d["code"] = V7["code"]
    for a in d["actionLibraryList"]:
        a["templateId"] = V7["id"]
    return d


def make_month_days(month_key, entries_by_date=None):
    y, m = map(int, month_key.split("-"))
    days = []
    for dd in range(1, pycal.monthrange(y, m)[1] + 1):
        ds = f"{y:04d}-{m:02d}-{dd:02d}"
        days.append({
            "date": ds,
            "trainingPlanList": [dict(e) for e in (entries_by_date or {}).get(ds, [])],
        })
    return days


def entry(code, title, entry_id=6001, is_reservation=True):
    return {"id": entry_id, "code": code, "title": title,
            "isReservation": bool(is_reservation)}


class FakeOpsClient:
    def __init__(self, workouts=None, calendar=None, details=None):
        self.credentials = {"user_id": "fake-user-id-123", "token": "fake-token-value-abc"}
        self.device_type = 1
        self._workouts = workouts if workouts is not None else make_workouts()
        self._calendar = calendar if calendar is not None else {}
        self._details = details if details is not None else {}
        self.reservation_calls = []
        self.delete_calls = []
        self.save_calls = []
        self.calendar_requests = []
        self.mutate_on_reservation = None
        self.fail_delete_silently = False

    def get_user_workouts(self):
        return copy.deepcopy(self._workouts)

    def get_calendar_month(self, month):
        self.calendar_requests.append(month)
        return copy.deepcopy(self._calendar.get(month, []))

    def get_workout_detail(self, code):
        return copy.deepcopy(self._details.get(code))

    def schedule_workout(self, date_str, code, status):
        self.reservation_calls.append((date_str, code, status))
        month = date_str[:7]
        days = self._calendar.setdefault(month, make_month_days(month))
        for day in days:
            if day["date"] == date_str:
                lst = day.setdefault("trainingPlanList", [])
                if status == 1:
                    title = next((w["name"] for w in self._workouts
                                  if w["code"] == code), "?")
                    lst.append(entry(code, title, 7000 + len(self.reservation_calls)))
                else:
                    lst[:] = [p for p in lst
                              if not (str(p.get("code") or p.get("templateCode") or "") == code
                                      and p.get("isReservation") is not False)]
        if self.mutate_on_reservation:
            self.mutate_on_reservation(self._calendar)
        return True

    def delete_workout_checked(self, wid):
        self.delete_calls.append(wid)
        if not self.fail_delete_silently:
            self._workouts = [w for w in self._workouts if w["id"] != wid]
        return {"code": 0}

    def save_workout_payload(self, payload):
        self.save_calls.append(copy.deepcopy(payload))
        new_id = 900000 + len(self.save_calls)
        new_code = f"new-code-{new_id}"
        self._workouts.append({"id": new_id, "name": payload["name"], "code": new_code})
        self._details[new_code] = {
            "id": new_id, "name": payload["name"], "code": new_code,
            "totalCapacity": payload.get("totalCapacity"), "bgColor": 0,
            "actionLibraryList": [
                dict(a, sort=i + 1) for i, a in enumerate(payload["actionLibraryList"])
            ],
        }
        return {"code": 0, "message": "Success"}


class OpsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def sched_client(self, entries_by_date=None):
        return FakeOpsClient(
            calendar={SCHED_MONTH: make_month_days(SCHED_MONTH, entries_by_date)}
        )

    def with_write_env(self, fn, destructive=False):
        env = {wo.WRITE_ENABLE_ENV: "true"}
        if destructive:
            env[wo.DESTRUCTIVE_ENV] = "true"
        with patch.dict(os.environ, env):
            return fn()


class TestScheduling(OpsTestCase):
    def test_exact_title_match(self):
        result = wo.run_schedule(self.sched_client(), "I-BK-v9", SCHED_DATE,
                                 runtime_dir=self.tmpdir)
        self.assertEqual(result["plan"]["workout_id"], 501)
        self.assertEqual(result["plan"]["workout_code"], "code-v9")
        self.assertEqual(result["outcome"], "DRY-RUN ONLY - no write performed")

    def test_zero_title_match_rejected(self):
        with self.assertRaisesRegex(WorkoutUpdateError, "No saved workout"):
            wo.run_schedule(self.sched_client(), "i-bk-v9", SCHED_DATE,
                            runtime_dir=self.tmpdir)

    def test_multiple_title_match_rejected(self):
        client = self.sched_client()
        client._workouts.append({"id": 599, "name": "I-BK-v9", "code": "dupe"})
        with self.assertRaisesRegex(WorkoutUpdateError, "2 saved workouts"):
            wo.run_schedule(client, "I-BK-v9", SCHED_DATE, runtime_dir=self.tmpdir)

    def test_valid_date_parsing(self):
        self.assertEqual(wo.validate_date("2099-07-11"), date(2099, 7, 11))

    def test_invalid_dates_rejected(self):
        for bad in ("2099-7-11", "07/11/2099", "2099-13-40", "20990711", "", "tomorrow"):
            with self.assertRaises(WorkoutUpdateError, msg=bad):
                wo.validate_date(bad)

    def test_past_date_rejected(self):
        with self.assertRaisesRegex(WorkoutUpdateError, "past"):
            wo.run_schedule(self.sched_client(), "I-BK-v9", "2000-01-01",
                            runtime_dir=self.tmpdir)

    def test_exact_identifier_and_date_only_payload(self):
        client = self.sched_client()
        self.with_write_env(lambda: wo.run_schedule(
            client, "I-BK-v9", SCHED_DATE, apply=True, runtime_dir=self.tmpdir))
        # The outgoing payload uses the exact template code and a date-only
        # YYYY-MM-DD string (no time component).
        self.assertEqual(client.reservation_calls, [(SCHED_DATE, "code-v9", 1)])

    def test_identical_schedule_is_idempotent_noop(self):
        client = self.sched_client({SCHED_DATE: [entry("code-v9", "I-BK-v9")]})
        result = self.with_write_env(lambda: wo.run_schedule(
            client, "I-BK-v9", SCHED_DATE, apply=True, runtime_dir=self.tmpdir))
        self.assertIn("ALREADY_SCHEDULED", result["outcome"])
        self.assertEqual(client.reservation_calls, [])

    def test_different_workout_on_same_date_is_allowed(self):
        client = self.sched_client({SCHED_DATE: [entry("code-leg", "Leg Day")]})
        result = self.with_write_env(lambda: wo.run_schedule(
            client, "I-BK-v9", SCHED_DATE, apply=True, runtime_dir=self.tmpdir))
        self.assertEqual(result["outcome"], "SCHEDULED AND VERIFIED")
        self.assertEqual(client.reservation_calls, [(SCHED_DATE, "code-v9", 1)])
        self.assertEqual(result["plan"]["existing_other_entries"][0]["code"], "code-leg")

    def test_duplicated_same_code_entries_rejected_as_ambiguous(self):
        client = self.sched_client({SCHED_DATE: [
            entry("code-v9", "I-BK-v9", 6001),
            entry("code-v9", "I-BK-v9", 6002),
        ]})
        with self.assertRaisesRegex(WorkoutUpdateError, "ambiguous"):
            self.with_write_env(lambda: wo.run_schedule(
                client, "I-BK-v9", SCHED_DATE, apply=True, runtime_dir=self.tmpdir))
        self.assertEqual(client.reservation_calls, [])

    def test_scheduling_preserves_unrelated_same_date_entries(self):
        client = self.sched_client({SCHED_DATE: [
            entry("code-leg", "Leg Day", 6001),
            entry("code-v7", "I-BK-v7", 6002),
        ]})
        result = self.with_write_env(lambda: wo.run_schedule(
            client, "I-BK-v9", SCHED_DATE, apply=True, runtime_dir=self.tmpdir))
        self.assertEqual(result["outcome"], "SCHEDULED AND VERIFIED")
        after = client.get_calendar_month(SCHED_MONTH)
        codes = sorted(wo._entry_code(p) for p in wo.day_entries(after, SCHED_DATE))
        self.assertEqual(codes, ["code-leg", "code-v7", "code-v9"])

    def test_dry_run_makes_no_write(self):
        client = self.sched_client()
        wo.run_schedule(client, "I-BK-v9", SCHED_DATE, runtime_dir=self.tmpdir)
        self.assertEqual(client.reservation_calls, [])

    def test_apply_requires_both_interlocks(self):
        client = self.sched_client()
        env = {k: v for k, v in os.environ.items() if k != wo.WRITE_ENABLE_ENV}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(WorkoutUpdateError, wo.WRITE_ENABLE_ENV):
                wo.run_schedule(client, "I-BK-v9", SCHED_DATE, apply=True,
                                runtime_dir=self.tmpdir)
        self.assertEqual(client.reservation_calls, [])

    def test_fetch_back_confirms_expected_schedule(self):
        client = self.sched_client()
        result = self.with_write_env(lambda: wo.run_schedule(
            client, "I-BK-v9", SCHED_DATE, apply=True, runtime_dir=self.tmpdir))
        self.assertEqual(result["outcome"], "SCHEDULED AND VERIFIED")
        self.assertEqual(result["entry"]["code"], "code-v9")

    def test_unrelated_entries_must_be_unchanged(self):
        client = self.sched_client()

        def corrupt(cal_state):
            cal_state[SCHED_MONTH][0]["trainingPlanList"] = [
                entry("code-leg", "Leg Day", 6099)
            ]

        client.mutate_on_reservation = corrupt
        result = self.with_write_env(lambda: wo.run_schedule(
            client, "I-BK-v9", SCHED_DATE, apply=True, runtime_dir=self.tmpdir))
        self.assertIn("SCHEDULE STATE UNCERTAIN", result["outcome"])

    def test_ambiguous_empty_month_blocks_apply(self):
        client = FakeOpsClient(calendar={})
        with self.assertRaisesRegex(WorkoutUpdateError, "ambiguous"):
            self.with_write_env(lambda: wo.run_schedule(
                client, "I-BK-v9", SCHED_DATE, apply=True, runtime_dir=self.tmpdir))
        self.assertEqual(client.reservation_calls, [])


class TestUnscheduling(OpsTestCase):
    def occupied_client(self):
        return self.sched_client({SCHED_DATE: [
            entry("code-v9", "I-BK-v9", 6001),
            entry("code-leg", "Leg Day", 6002),
        ]})

    def test_exact_entry_resolved_by_code_and_date(self):
        result = wo.run_unschedule(self.occupied_client(), "I-BK-v9", SCHED_DATE,
                                   runtime_dir=self.tmpdir)
        self.assertEqual(result["plan"]["matching_entries"], 1)
        self.assertEqual(result["plan"]["schedule_entry_id"], 6001)
        self.assertEqual(result["outcome"], "DRY-RUN ONLY - no write performed")

    def test_zero_match_returns_not_scheduled(self):
        client = self.sched_client()  # month exists, nothing on the date
        dry = wo.run_unschedule(client, "I-BK-v9", SCHED_DATE, runtime_dir=self.tmpdir)
        self.assertIn("NOT_SCHEDULED", dry["outcome"])
        live = self.with_write_env(lambda: wo.run_unschedule(
            client, "I-BK-v9", SCHED_DATE, apply=True, runtime_dir=self.tmpdir))
        self.assertIn("NOT_SCHEDULED", live["outcome"])
        self.assertEqual(client.reservation_calls, [])  # no blind delete

    def test_multiple_matching_entries_rejected(self):
        client = self.sched_client({SCHED_DATE: [
            entry("code-v9", "I-BK-v9", 6001),
            entry("code-v9", "I-BK-v9", 6003),
        ]})
        with self.assertRaisesRegex(WorkoutUpdateError, "ambiguous"):
            self.with_write_env(lambda: wo.run_unschedule(
                client, "I-BK-v9", SCHED_DATE, apply=True, runtime_dir=self.tmpdir))
        self.assertEqual(client.reservation_calls, [])

    def test_delete_key_is_date_plus_template_code(self):
        # The proven API contract has no per-entry-id delete; the display id is
        # recorded as evidence only.
        client = self.occupied_client()
        result = self.with_write_env(lambda: wo.run_unschedule(
            client, "I-BK-v9", SCHED_DATE, apply=True, runtime_dir=self.tmpdir))
        self.assertEqual(client.reservation_calls, [(SCHED_DATE, "code-v9", 0)])
        self.assertEqual(result["plan"]["delete_key"],
                         {"thatDay": SCHED_DATE, "templateCode": "code-v9"})

    def test_dry_run_makes_no_write(self):
        client = self.occupied_client()
        wo.run_unschedule(client, "I-BK-v9", SCHED_DATE, runtime_dir=self.tmpdir)
        self.assertEqual(client.reservation_calls, [])

    def test_fetch_back_confirms_removal(self):
        client = self.occupied_client()
        result = self.with_write_env(lambda: wo.run_unschedule(
            client, "I-BK-v9", SCHED_DATE, apply=True, runtime_dir=self.tmpdir))
        self.assertEqual(result["outcome"], "UNSCHEDULED AND VERIFIED")
        after = client.get_calendar_month(SCHED_MONTH)
        self.assertEqual(wo.reservations_for(after, SCHED_DATE, "code-v9"), [])

    def test_other_workouts_on_same_date_preserved(self):
        client = self.sched_client({SCHED_DATE: [
            entry("code-v9", "I-BK-v9", 6001),
            entry("code-leg", "Leg Day", 6002),
            entry("code-v7", "I-BK-v7", 6003),
        ]})
        result = self.with_write_env(lambda: wo.run_unschedule(
            client, "I-BK-v9", SCHED_DATE, apply=True, runtime_dir=self.tmpdir))
        self.assertEqual(result["outcome"], "UNSCHEDULED AND VERIFIED")
        after = client.get_calendar_month(SCHED_MONTH)
        codes = sorted(wo._entry_code(p) for p in wo.day_entries(after, SCHED_DATE))
        self.assertEqual(codes, ["code-leg", "code-v7"])

    def test_unrelated_mutation_flagged(self):
        client = self.occupied_client()

        def corrupt(cal_state):
            for day in cal_state[SCHED_MONTH]:
                if day["date"] == SCHED_DATE:
                    day["trainingPlanList"] = [
                        p for p in day["trainingPlanList"]
                        if p.get("code") != "code-leg"
                    ]

        client.mutate_on_reservation = corrupt
        result = self.with_write_env(lambda: wo.run_unschedule(
            client, "I-BK-v9", SCHED_DATE, apply=True, runtime_dir=self.tmpdir))
        self.assertIn("UNSCHEDULE STATE UNCERTAIN", result["outcome"])


class TestRemovalHorizon(OpsTestCase):
    def remove_client(self, ref_days=None):
        cal_state = {}
        if ref_days is not None:
            ref_date = date.today() + timedelta(days=ref_days)
            month = f"{ref_date.year:04d}-{ref_date.month:02d}"
            cal_state[month] = make_month_days(
                month, {ref_date.strftime("%Y-%m-%d"): [entry("code-v7", "I-BK-v7", 6050)]}
            )
        return FakeOpsClient(calendar=cal_state,
                             details={"code-v7": make_v7_detail()})

    def test_default_removal_scan_is_30_days(self):
        # Operator policy: 30-day default buffer over the ~7-day scheduling habit.
        self.assertEqual(wo.DEFAULT_REMOVAL_SCAN_DAYS, 30)
        result = wo.run_remove(self.remove_client(), "I-BK-v7", runtime_dir=self.tmpdir)
        self.assertEqual(result["plan"]["schedule_scan"]["scan_days"], 30)

    def test_custom_365_day_horizon_accepted(self):
        client = self.remove_client()
        result = wo.run_remove(client, "I-BK-v7",
                               runtime_dir=self.tmpdir, scan_days=365)
        scan = result["plan"]["schedule_scan"]
        self.assertEqual(scan["scan_days"], 365)
        start = datetime.strptime(scan["scan_start_date"], "%Y-%m-%d").date()
        end = datetime.strptime(scan["scan_end_date"], "%Y-%m-%d").date()
        self.assertEqual((end - start).days, 365)
        # A larger horizon fans out to every intersecting month (12-13 requests).
        self.assertEqual(scan["months_requested"],
                         len(wo.months_in_range(start, end)))
        self.assertEqual(sorted(set(client.calendar_requests)), scan["months"])

    def test_invalid_horizon_rejected(self):
        for bad in (0, -5, 100000, "abc", None):
            with self.assertRaises(WorkoutUpdateError, msg=repr(bad)):
                wo.run_remove(self.remove_client(), "I-BK-v7",
                              runtime_dir=self.tmpdir, scan_days=bad)

    def test_every_intersecting_month_queried(self):
        client = self.remove_client()
        result = wo.run_remove(client, "I-BK-v7", runtime_dir=self.tmpdir)
        scan = result["plan"]["schedule_scan"]
        start = datetime.strptime(scan["scan_start_date"], "%Y-%m-%d").date()
        end = datetime.strptime(scan["scan_end_date"], "%Y-%m-%d").date()
        expected_months = wo.months_in_range(start, end)
        self.assertEqual(scan["months"], expected_months)
        self.assertEqual(scan["months_requested"], len(expected_months))
        self.assertEqual(sorted(set(client.calendar_requests)), sorted(expected_months))

    def test_reference_on_day_30_blocks_removal(self):
        # The horizon end date is inclusive: a reference exactly on day 30
        # blocks removal under the default scan.
        client = self.remove_client(ref_days=30)
        dry = wo.run_remove(client, "I-BK-v7", runtime_dir=self.tmpdir)
        self.assertIn("BLOCKED", dry["outcome"])
        self.assertEqual(dry["plan"]["scheduled_reference_count"], 1)

    def test_reference_after_day_30_is_outside_default_scan(self):
        # A reference at +40 days is invisible to the default 30-day scan; the
        # plan must report only bounded evidence for the recorded horizon,
        # never a global absence claim — and a wider scan still finds it.
        client = self.remove_client(ref_days=40)
        result = wo.run_remove(client, "I-BK-v7", runtime_dir=self.tmpdir)
        self.assertEqual(result["outcome"], "DRY-RUN ONLY - no delete performed")
        self.assertEqual(
            result["plan"]["schedule_scan"]["result"],
            "no schedule reference was found within the recorded scan horizon",
        )
        wider = wo.run_remove(self.remove_client(ref_days=40), "I-BK-v7",
                              runtime_dir=self.tmpdir, scan_days=60)
        self.assertIn("BLOCKED", wider["outcome"])

    def test_scan_window_dates_recorded_in_plan(self):
        result = wo.run_remove(self.remove_client(), "I-BK-v7", runtime_dir=self.tmpdir)
        scan = result["plan"]["schedule_scan"]
        self.assertRegex(scan["scan_start_date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(scan["scan_end_date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertLess(scan["scan_start_date"], scan["scan_end_date"])


class TestRemoveWorkout(OpsTestCase):
    def remove_client(self, schedule_refs=False):
        # 14 days ahead: inside the default 30-day scan horizon.
        return TestRemovalHorizon.remove_client(
            self, ref_days=14 if schedule_refs else None)

    def apply_remove(self, client, **overrides):
        kwargs = dict(expected_id="502", confirm_title="I-BK-v7", apply=True,
                      runtime_dir=self.tmpdir)
        kwargs.update(overrides)
        return wo.run_remove(client, "I-BK-v7", **kwargs)

    def test_exact_title_match(self):
        result = wo.run_remove(self.remove_client(), "I-BK-v7", runtime_dir=self.tmpdir)
        self.assertEqual(result["plan"]["workout_id"], 502)
        self.assertEqual(result["plan"]["workout_code"], "code-v7")
        self.assertEqual(result["outcome"], "DRY-RUN ONLY - no delete performed")

    def test_zero_and_multiple_matches_rejected(self):
        client = self.remove_client()
        with self.assertRaisesRegex(WorkoutUpdateError, "No saved workout"):
            wo.run_remove(client, "I-BK-V7", runtime_dir=self.tmpdir)
        client._workouts.append({"id": 598, "name": "I-BK-v7", "code": "dupe7"})
        with self.assertRaisesRegex(WorkoutUpdateError, "2 saved workouts"):
            wo.run_remove(client, "I-BK-v7", runtime_dir=self.tmpdir)

    def test_expected_id_mismatch_rejected(self):
        client = self.remove_client()
        with self.assertRaisesRegex(WorkoutUpdateError, "expected-id"):
            self.with_write_env(lambda: self.apply_remove(client, expected_id="999"),
                                destructive=True)
        self.assertEqual(client.delete_calls, [])

    def test_confirm_title_mismatch_rejected(self):
        client = self.remove_client()
        with self.assertRaisesRegex(WorkoutUpdateError, "confirm-title"):
            self.with_write_env(lambda: self.apply_remove(client, confirm_title="I-BK-v8"),
                                destructive=True)
        self.assertEqual(client.delete_calls, [])

    def test_standard_interlock_required(self):
        client = self.remove_client()
        env = {k: v for k, v in os.environ.items() if k != wo.WRITE_ENABLE_ENV}
        env[wo.DESTRUCTIVE_ENV] = "true"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(WorkoutUpdateError, wo.WRITE_ENABLE_ENV):
                self.apply_remove(client)
        self.assertEqual(client.delete_calls, [])

    def test_destructive_interlock_required(self):
        client = self.remove_client()
        with self.assertRaisesRegex(WorkoutUpdateError, wo.DESTRUCTIVE_ENV):
            self.with_write_env(lambda: self.apply_remove(client), destructive=False)
        self.assertEqual(client.delete_calls, [])

    def test_scheduled_workout_cannot_be_removed(self):
        client = self.remove_client(schedule_refs=True)
        dry = wo.run_remove(client, "I-BK-v7", runtime_dir=self.tmpdir)
        self.assertIn("BLOCKED", dry["outcome"])
        self.assertEqual(dry["plan"]["scheduled_reference_count"], 1)
        with self.assertRaisesRegex(WorkoutUpdateError, "schedule reference"):
            self.with_write_env(lambda: self.apply_remove(client), destructive=True)
        self.assertEqual(client.delete_calls, [])

    def test_backup_created_before_delete(self):
        result = wo.run_remove(self.remove_client(), "I-BK-v7", runtime_dir=self.tmpdir)
        with open(result["backup_path"], encoding="utf-8") as f:
            backup = json.load(f)
        self.assertEqual(backup["schema_version"], wo.REMOVAL_BACKUP_SCHEMA)
        self.assertEqual(backup["workout_title"], "I-BK-v7")
        self.assertFalse(backup["remote_identity_preserved"])
        payload = backup["recreation_payload"]
        self.assertNotIn("id", payload)  # re-create, never write to a dead id
        target = [a for a in payload["actionLibraryList"] if a["groupId"] == 321][0]
        self.assertEqual(target["weights"], "49,49,49")

    def test_dry_run_makes_no_delete(self):
        client = self.remove_client()
        self.with_write_env(
            lambda: wo.run_remove(client, "I-BK-v7", runtime_dir=self.tmpdir),
            destructive=True)
        self.assertEqual(client.delete_calls, [])

    def test_fetch_back_verifies_absence_and_others_unchanged(self):
        client = self.remove_client()
        result = self.with_write_env(lambda: self.apply_remove(client), destructive=True)
        self.assertIn("REMOVED AND VERIFIED", result["outcome"])
        self.assertIn("identity-destructive", result["outcome"])
        self.assertEqual(client.delete_calls, [502])
        remaining = client.get_user_workouts()
        self.assertEqual({w["id"] for w in remaining}, {501, 503})
        self.assertEqual({w["name"] for w in remaining}, {"I-BK-v9", "Leg Day"})

    def test_artifacts_contain_no_credentials(self):
        client = self.remove_client()
        result = wo.run_remove(client, "I-BK-v7", runtime_dir=self.tmpdir)
        for path in (result["plan_path"], result["backup_path"]):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn(client.credentials["token"], text)
            self.assertNotIn(client.credentials["user_id"], text)
            lowered = text.lower()
            for banned in ('"token"', '"authorization"', '"cookie"', '"password"'):
                self.assertNotIn(banned, lowered)

    def test_delete_verification_failure_surfaced(self):
        client = self.remove_client()
        client.fail_delete_silently = True
        with self.assertRaisesRegex(WorkoutUpdateError, "did not take effect"):
            self.with_write_env(lambda: self.apply_remove(client), destructive=True)
        self.assertIn(502, [w["id"] for w in client.get_user_workouts()])


class TestRecreationSemantics(OpsTestCase):
    remove_client = TestRemoveWorkout.remove_client
    apply_remove = TestRemoveWorkout.apply_remove

    def _remove_then_recreate(self):
        client = self.remove_client()
        removed = self.with_write_env(lambda: self.apply_remove(client), destructive=True)
        recreated = self.with_write_env(lambda: wo.run_recreate_from_backup(
            client, removed["backup_path"], apply=True))
        return client, removed, recreated

    def test_removal_plan_states_identity_not_preserved(self):
        result = wo.run_remove(self.remove_client(), "I-BK-v7", runtime_dir=self.tmpdir)
        self.assertIs(result["plan"]["remote_identity_preserved"], False)
        self.assertIs(result["plan"]["recreation_expected_new_remote_identity"], True)

    def test_recreation_records_old_and_new_identities_and_hashes(self):
        _, _, recreated = self._remove_then_recreate()
        self.assertEqual(recreated["deleted_remote_id"], 502)
        self.assertEqual(recreated["deleted_remote_code"], "code-v7")
        self.assertEqual(recreated["new_remote_id"], 900001)
        self.assertEqual(recreated["new_remote_code"], "new-code-900001")
        self.assertTrue(recreated["old_canonical_hash"])
        self.assertTrue(recreated["recreated_canonical_hash"])

    def test_content_equivalent_recreation_with_new_id_reported_correctly(self):
        client, _, recreated = self._remove_then_recreate()
        self.assertIs(recreated["content_equivalent"], True)
        self.assertIs(recreated["identity_preserved"], False)
        self.assertIn("NEW", recreated["outcome"])
        self.assertIn("I-BK-v7", [w["name"] for w in client.get_user_workouts()])
        self.assertNotIn("id", client.save_calls[0])

    def test_recreation_never_claims_identity_restoration(self):
        _, _, recreated = self._remove_then_recreate()
        self.assertIn("original remote identity was not restored", recreated["outcome"])
        for banned in ("ROLLBACK", "RESTORED AND VERIFIED", "identity restored"):
            self.assertNotIn(banned, recreated["outcome"])

    def test_recreate_dry_run_makes_no_write(self):
        client = self.remove_client()
        removed = self.with_write_env(lambda: self.apply_remove(client), destructive=True)
        result = wo.run_recreate_from_backup(client, removed["backup_path"], apply=False)
        self.assertEqual(result["outcome"], "DRY-RUN ONLY - no write performed")
        self.assertEqual(client.save_calls, [])

    def test_compat_alias_and_v1_backup_field_accepted(self):
        self.assertIs(wo.run_restore, wo.run_recreate_from_backup)
        client = self.remove_client()
        removed = self.with_write_env(lambda: self.apply_remove(client), destructive=True)
        with open(removed["backup_path"], encoding="utf-8") as f:
            backup = json.load(f)
        backup["schema_version"] = "speediance-workout-removal-backup/v1"
        backup["restore_create_payload"] = backup.pop("recreation_payload")
        v1_path = os.path.join(self.tmpdir, "v1-backup.json")
        with open(v1_path, "w", encoding="utf-8") as f:
            json.dump(backup, f)
        result = self.with_write_env(lambda: wo.run_restore(client, v1_path, apply=True))
        self.assertIs(result["content_equivalent"], True)

    def test_documentation_uses_corrected_terminology(self):
        doc_path = os.path.join(os.path.dirname(__file__), "..", "WORKOUT_OPERATIONS.md")
        with open(doc_path, encoding="utf-8") as f:
            doc = f.read()
        self.assertIn("identity-destructive", doc)
        self.assertIn("recreate", doc.lower())
        for banned in ("fully reversible", "reversible delete", "restore original workout",
                       "restores the original identity"):
            self.assertNotIn(banned, doc.lower())


class TestTimezone(unittest.TestCase):
    def test_edmonton_resolves_via_zoneinfo(self):
        zone = wo.get_zone("America/Edmonton")
        self.assertEqual(str(zone), "America/Edmonton")

    def test_winter_offset_is_minus_420(self):
        self.assertEqual(wo.tz_offset_minutes("America/Edmonton", date(2026, 1, 15)), -420)

    def test_summer_offset_is_minus_360(self):
        self.assertEqual(wo.tz_offset_minutes("America/Edmonton", date(2026, 7, 15)), -360)

    def test_utc_timestamp_converts_to_edmonton_date(self):
        # 2026-01-15 12:00 UTC = 05:00 MST same day.
        self.assertEqual(wo.local_training_date("2026-01-15T12:00:00Z"),
                         date(2026, 1, 15))

    def test_late_evening_workout_stays_on_edmonton_date(self):
        # 22:30 MDT on Jul 9 is 04:30 UTC on Jul 10; truncating the UTC value
        # would wrongly shift the training date to the next day.
        for value in ("2026-07-10T04:30:00Z",
                      "2026-07-10 04:30:00",           # naive server time (UTC)
                      datetime(2026, 7, 10, 4, 30, tzinfo=dt_timezone.utc),
                      1783657800):                      # same instant, epoch s
            self.assertEqual(wo.local_training_date(value), date(2026, 7, 9), value)

    def test_original_utc_instant_preserved(self):
        local = wo.utc_to_local("2026-07-10T04:30:00Z")
        self.assertEqual(local.astimezone(dt_timezone.utc),
                         datetime(2026, 7, 10, 4, 30, tzinfo=dt_timezone.utc))

    def test_missing_tzdata_fails_explicitly(self):
        broken = MagicMock(side_effect=ZoneInfoNotFoundError("no tzdata"))
        with patch.object(wo, "ZoneInfo", broken):
            with self.assertRaisesRegex(WorkoutUpdateError, "tzdata"):
                wo.get_zone("America/Edmonton")

    def test_unknown_timezone_fails_explicitly(self):
        with self.assertRaisesRegex(WorkoutUpdateError, "could not be resolved"):
            wo.get_zone("Not/AZone")

    def test_routine_windows(self):
        h_start, h_end = wo.routine_history_window()
        self.assertEqual((h_end - h_start).days, 30)
        c_start, c_end = wo.routine_calendar_window()
        self.assertEqual((h_end - c_start).days, 7)
        self.assertEqual((c_end - h_end).days, 30)


class TestPlanArtifacts(OpsTestCase):
    def sched_client(self, entries_by_date=None):
        return FakeOpsClient(
            calendar={SCHED_MONTH: make_month_days(SCHED_MONTH, entries_by_date)}
        )

    def test_schedule_plan_records_timezone_evidence(self):
        result = wo.run_schedule(self.sched_client(), "I-BK-v9", SCHED_DATE,
                                 runtime_dir=self.tmpdir)
        with open(result["plan_path"], encoding="utf-8") as f:
            plan = json.load(f)
        self.assertEqual(plan["schema_version"], wo.SCHEDULE_PLAN_SCHEMA)
        self.assertEqual(plan["timezone_name"], "America/Edmonton")
        self.assertEqual(plan["timezone_source"], "zoneinfo")
        self.assertRegex(plan["local_date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn(plan["effective_utc_offset_minutes"], (-420, -360))
        self.assertEqual(plan["scheduled_date"], SCHED_DATE)
        self.assertFalse(plan["apply_requested"])

    def test_remove_plan_backup_path_points_to_existing_file(self):
        client = FakeOpsClient(details={"code-v7": make_v7_detail()})
        result = wo.run_remove(client, "I-BK-v7", runtime_dir=self.tmpdir)
        with open(result["plan_path"], encoding="utf-8") as f:
            plan = json.load(f)
        self.assertEqual(plan["backup_path"], result["backup_path"])
        self.assertTrue(os.path.isfile(plan["backup_path"]),
                        f"backup_path in plan does not exist: {plan['backup_path']}")
        with open(plan["backup_path"], encoding="utf-8") as f:
            backup = json.load(f)
        self.assertEqual(backup["workout_id"], plan["workout_id"])


if __name__ == "__main__":
    unittest.main()
