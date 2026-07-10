# Saved-Workout Operations (`workout_ops.py`)

Deterministic schedule / unschedule / remove (and recreate-from-backup)
operations for saved Speediance custom workouts. Companion to the field-level
updater in [WORKOUT_UPDATE.md](WORKOUT_UPDATE.md) and the read-only
export/version/snapshot layer in
[CUSTOM_WORKOUT_EXPORT.md](CUSTOM_WORKOUT_EXPORT.md). These are the building
blocks for an eventual AI fitness coach; natural-language phrasing like the
following maps 1:1 onto the commands below:

> *"Schedule workout 'I-BK-v9' for July 11, 2026."*
> *"Unschedule workout 'I-BK-v9' from July 11, 2026."*
> *"Remove custom workout 'I-BK-v7'."*

All operations are **dry-run by default**, use **exact title matching only**
(zero or multiple matches abort), and resolve titles to immutable remote
identifiers (numeric template `id` + template `code`) before any mutation.

## API contracts (evidence-backed)

| Operation | Call | Identifier |
|---|---|---|
| List schedule | `GET /api/app/v5/trainingCalendar/monthNew?date=YYYY-MM` | — |
| Schedule | `POST /api/app/templateReservation` `{status:1, deviceType, thatDay:"YYYY-MM-DD", templateCode}` | template `code` |
| Unschedule | same endpoint with `status:0` | (`thatDay`, `templateCode`) |
| Delete workout | `DELETE /api/app/customTrainingTemplate?ids=<id>` | numeric template `id` |
| Recreate (re-create content) | `POST /api/app/v2/customTrainingTemplate` (payload without `id`) | — |

Schedule payloads are **date-only** (`thatDay: YYYY-MM-DD`, no time). The
reservation API exposes **no per-entry identifier for deletes**: an entry is
addressed by its (date, template code) pair. When the calendar response
carries an entry `id`, it is recorded in plans as evidence only — never used
as the delete key.

## Routine read windows

Rolling defaults for ordinary display, lookup, coaching, and synchronization
(`routine_history_window()` / `routine_calendar_window()`):

- **Training history:** local today − 30 days → today.
- **Calendar/schedule view:** local today − 7 days → today + 30 days.

These routine windows are **never** valid proof that a workout has no future
schedule references — destructive removal uses its own distinct scan horizon
(below).

## Timezone handling

- Canonical application timezone: **`America/Edmonton`**.
- Timezone rules come from Python's `zoneinfo` backed by the maintained
  `tzdata` package (in `requirements.txt`). There is **no custom timezone
  database**, and no silent fallback: if the configured timezone cannot be
  resolved the operation fails explicitly rather than quietly using system
  local time or treating UTC as Edmonton time.
- The timezone determines the intended **local calendar date** and local
  "today" (e.g. the past-date check and scan-horizon start). The remote API
  remains date-only; no timezone is sent in schedule payloads.
- Every generated plan records the evidence block:
  `timezone_name`, `local_date`, `effective_utc_offset_minutes` (−420 in
  winter/MST, −360 in summer/MDT), `timezone_source: "zoneinfo"`.
- Speediance UTC timestamps are parsed timezone-aware (`parse_utc_timestamp`),
  converted to Edmonton before assigning a local training date
  (`local_training_date`), and the original UTC instant is preserved
  (`utc_to_local`). The local date is **never** derived by truncating the UTC
  timestamp — a 22:30 Edmonton workout whose UTC time crossed midnight stays
  on the Edmonton date.
- Future database note: SQLite should store **timezone names and timestamps**,
  not timezone transition rules; rules always come from `zoneinfo`/`tzdata`.

## Scheduling

```powershell
# Dry-run (default)
python workout_ops.py schedule --title "I-BK-v9" --date "2026-07-11" --timezone "America/Edmonton"

# Apply
$env:SPEEDIANCE_WRITE_ENABLED = "true"
python workout_ops.py schedule --title "I-BK-v9" --date "2026-07-11" --apply
Remove-Item Env:SPEEDIANCE_WRITE_ENABLED
```

**Multiple workouts on one date are valid.** The idempotency/identity key is
`thatDay + templateCode`:

| Existing calendar state | Result |
|---|---|
| Different workout on the same date | **Allowed** (recorded as context in the plan) |
| Same template code on the date, exactly once | `ALREADY_SCHEDULED` idempotent no-op |
| Same template code on the date, multiple times | Ambiguous remote state — blocked |
| No matching entry | Schedule entry created |

Nothing is ever unscheduled or replaced to make room; no morning/evening
ordering is inferred (the observed contract is date-only). After an apply, the
month is fetched back: exactly one matching entry must exist and all unrelated
entries — including other workouts on the same date — must be unchanged, else
the result is `SCHEDULE STATE UNCERTAIN` (exit 3).

## Unscheduling

```powershell
python workout_ops.py unschedule --title "I-BK-v9" --date "2026-07-11"           # dry-run
python workout_ops.py unschedule --title "I-BK-v9" --date "2026-07-11" --apply   # + interlock
```

- The target is resolved by `thatDay + templateCode`. Other workouts on the
  same date remain untouched.
- Zero exact matches → `NOT_SCHEDULED` no-op in **both** modes; no blind
  deletes are ever issued.
- One exact match may be removed; multiple exact matches are ambiguous and
  blocked.
- Entries with `isReservation: false` (official programs, completed history)
  are never matched or touched.
- Post-write verification compares the full unrelated calendar state and
  confirms only the intended matching entry was removed.

## Removing a custom workout (destructive, identity-destructive)

**Remote removal is identity-destructive.** Deleting a remote workout destroys
its remote `id`/`code`. The backup written before deletion permits **content
recreation, generally under a new remote identity** — it does not preserve or
restore the original remote id/code. Plans record
`"remote_identity_preserved": false` and
`"recreation_expected_new_remote_identity": true`.

```powershell
# Dry-run: resolves identifiers, scans for schedule references, writes plan+backup
python workout_ops.py remove --title "I-BK-v7"

# Live deletion — ALL of the interlocks below are required
$env:SPEEDIANCE_WRITE_ENABLED = "true"
$env:SPEEDIANCE_DESTRUCTIVE_WRITE_ENABLED = "true"
python workout_ops.py remove --title "I-BK-v7" `
  --expected-id "<id from the dry-run plan>" `
  --confirm-title "I-BK-v7" `
  --apply
Remove-Item Env:SPEEDIANCE_DESTRUCTIVE_WRITE_ENABLED
Remove-Item Env:SPEEDIANCE_WRITE_ENABLED
```

**Standard vs destructive interlocks:** schedule/unschedule/recreate need
`--apply` + `SPEEDIANCE_WRITE_ENABLED=true`. Removal additionally requires
`SPEEDIANCE_DESTRUCTIVE_WRITE_ENABLED=true`, `--confirm-title` exactly equal
to the remote title, and `--expected-id` exactly equal to the resolved remote
id. Never leave the env vars set after a run.

**Future-reference scan (distinct from routine windows):** remote workout
removal scans **30 days forward by default** from the current
`America/Edmonton` local date. The horizon is configurable with
`--schedule-scan-days` (validated to 1..3650) — operators who have scheduled
further ahead than usual should select a larger horizon (e.g.
`--schedule-scan-days 365`). Every calendar month intersecting the selected
horizon is queried, and the plan records the exact `scan_start_date`,
`scan_end_date`, `scan_days`, and `months_requested`. Any reference within
the horizon blocks removal (unschedule explicitly first; removal never
unschedules automatically). A clean scan means exactly this:

> **No schedule reference was found within the recorded scan horizon.**

It does **not** mean that no future schedule reference exists beyond that
horizon.

**Backup before delete:** a `remove-backup-*.json` is always written first,
containing the full template detail (account-ID fields redacted), a canonical
SHA-256, and a ready-to-POST `recreation_payload` for the proven save
endpoint.

## Recreating from a backup

```powershell
python workout_ops.py recreate-from-backup --backup runtime/speediance-write-plans/remove-backup-<ts>.json --apply
```

Recreation POSTs the backup's payload **without an id**, producing a new
workout with a **new remote id/code** and equivalent content. The result
records the deleted id/code, the new id/code, both canonical hashes, and two
separate verdicts: `content_equivalent` (canonical field comparison) and
`identity_preserved` (always expected `false`). A content-equivalent
recreation is **content restoration under a new remote identity — never a
rollback of the deletion**. (`restore` remains as a deprecated CLI alias.)

## Future local database behavior (design note — NOT current implementation)

Planned Precision Health / local-database semantics, documented here so the
remote behavior above is designed to feed it:

- Locally cached workout records will use **soft deletion** — no local row is
  ever physically deleted when a remote workout is removed.
- Each workout has a stable **local lineage**; historical remote IDs/codes
  remain recorded against it.
- A remote recreation attaches the **new remote id/code to the same local
  lineage**, preserving continuity across the identity-destructive remote
  delete.
- Old workout snapshots and versions are retained in the local ledger.

## Artifacts

All plans/backups go to git-ignored `runtime/speediance-write-plans/`:
`schedule-plan-<ts>.json`, `unschedule-plan-<ts>.json`,
`remove-plan-<ts>.json`, `remove-backup-<ts>.json`. Removal plans reference
the actual on-disk `backup_path` (verified by test). Artifacts contain no
tokens, headers, cookies, or credential values (writes are refused if a
configured credential value would appear). Keep them after live runs — they
are the change evidence.

## Custom-workout limit — status

The UI shows "(n / 50)" on the My Workouts page, but **no repository or
captured API evidence confirms a server-enforced 50-workout limit**. Treat 50
as an unverified UI assumption. (The verified 50 limit is *exercises per
workout*, enforced client-side in the builder.)

## Tests

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

`tests/test_workout_ops.py` covers exact matching, strict dates, timezone
resolution and UTC→Edmonton conversion, multiple-workouts-per-date semantics,
idempotent no-ops, ambiguity blocking, both interlocks, destructive
confirmations, the bounded removal scan horizon, backup completeness and
path correctness, fetch-back verification, recreation identity/content
separation, and delete-failure surfacing — all against mocked clients; no
live credentials required.
