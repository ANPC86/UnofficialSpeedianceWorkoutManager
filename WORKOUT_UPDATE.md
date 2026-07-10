# Saved-Workout Updater (`workout_update.py`)

Safe, surgical updates to one saved Speediance custom workout template:
rename the workout and change the capacity/weight of one exercise's sets,
preserving everything else.

Related tooling: schedule/unschedule/remove/recreate →
[WORKOUT_OPERATIONS.md](WORKOUT_OPERATIONS.md); read-only export, versioning,
and snapshots → [CUSTOM_WORKOUT_EXPORT.md](CUSTOM_WORKOUT_EXPORT.md).

> **Warning — capacity/weight is NOT repetitions.**
> `--capacity` changes the per-set resistance stored in the template's
> `weights` CSV. Repetitions (`setsAndReps`), mode (`sportMode`), rest
> (`breakTime2`) and exercise ordering are never modified by this tool.

## Dry-run (default)

```powershell
python workout_update.py `
  --title "I-BK-v8" `
  --new-title "I-BK-v9" `
  --exercise-id 321 `
  --expected-capacity 49 `
  --capacity 51
```

Exercise ID `321` is the `groupId` of *Barbell Bent Over Row*. The dry-run
fetches the workout, validates everything, writes the artifacts below, prints
the sanitized diff, and performs **no write**.

## Apply (live write)

A live write requires **both** interlocks; either one alone does nothing:

1. the `--apply` flag, **and**
2. the environment variable `SPEEDIANCE_WRITE_ENABLED=true`.

```powershell
$env:SPEEDIANCE_WRITE_ENABLED = "true"
python workout_update.py --title "I-BK-v8" --new-title "I-BK-v9" `
  --exercise-id 321 --expected-capacity 49 --capacity 51 --apply
Remove-Item Env:SPEEDIANCE_WRITE_ENABLED   # do not leave write mode enabled
```

Authentication comes from `config.json` (as for the app) or the
`SPEEDIANCE_USER_ID` / `SPEEDIANCE_TOKEN` environment variables (same
convention as `test_e2e_workouts.py`). Never commit either.

## Artifacts

Written to `runtime/speediance-write-plans/` (git-ignored) before any write:

- `plan-<workout-id>-<timestamp>.json` — sanitized write plan
  (`speediance-workout-write-plan/v1`): source workout id/title/hash, target
  title, per-exercise `from`/`to` weights and the 1-based set numbers changed.
  Contains no tokens, headers, cookies, or credentials.
- `backup-<workout-id>-<timestamp>.json` — full template detail (account-ID
  fields redacted) **plus a ready-to-POST `restore_payload`** that reproduces
  the original workout for rollback.

Keep both files after a live run; they are the change evidence.

## Exact-match behavior

- The source workout is matched by **exact** title equality; zero or multiple
  matches abort. No fuzzy matching, ever.
- The exercise is matched by exact `groupId`; it must appear exactly once.
- Only sets whose current weight equals `--expected-capacity` **exactly** are
  changed; mixed-load sets with other values are left untouched (and at least
  one set must match, or the run aborts).

## Backup / rollback

After an apply, the workout is fetched back and canonically compared against
the original plus exactly the planned change (title + target weights). Any
other difference triggers an automatic rollback by POSTing the backup's
`restore_payload` through the same endpoint, followed by a restore
verification. If rollback cannot be confirmed the tool reports
`LIVE UPDATE STATE UNCERTAIN` and stops — no speculative corrective writes.

## Safety stop conditions (no write happens)

- Zero or multiple workouts with the exact source title
- Target title already used by a different workout
- Exercise ID absent or duplicated in the workout
- No set at the expected current capacity
- Weights/reps CSV length mismatch (schema drift)
- Missing authentication
- Missing either write interlock
- Artifact serialization would contain a configured credential value

## Scope guarantees

Only `POST /api/app/v2/customTrainingTemplate` (the same endpoint the app's
builder uses for saves/updates) is ever written to. Workout history, training
records, and the exercise library are never modified.

## Tests

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

`tests/test_workout_update.py` covers lookup/validation, interlocks,
preservation of reps/mode/rest/order/other exercises, artifact sanitization,
fetch-back verification, and rollback.
