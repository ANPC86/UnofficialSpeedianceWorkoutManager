# Custom Workout Export and Snapshot Layer

Part of the saved-workout tooling suite:

| Task | Command / document |
| --- | --- |
| Update a saved workout (capacity/weight, title) | [WORKOUT_UPDATE.md](WORKOUT_UPDATE.md) |
| Schedule / unschedule / remove / recreate | [WORKOUT_OPERATIONS.md](WORKOUT_OPERATIONS.md) |
| Export / version / snapshot workouts | this document |

## Scope and privacy

`custom_workout_export.py` is a read-only export service for custom
Speediance workouts. It reuses the same client reads as the Flask dashboard
and editor:

- `SpeedianceClient.get_user_workouts()`
- `SpeedianceClient.get_workout_detail(code)`

It never calls create, update, schedule, unschedule, or delete methods.
Exports are local, git-ignored runtime artifacts. They exclude credentials,
tokens, cookies, request headers, account/profile fields, and unfiltered API
responses.

## Existing dashboard and export flow

```text
GET /
  -> app.py::index
  -> templates/index.html workout cards and data-code
  -> client.get_user_workouts()
  -> GET /api/app/v4/customTrainingTemplate/appPage?pageNo=1&pageSize=-1&deviceTypes=<deviceType>
  -> immutable summary id + code
  -> /edit/<code> or /api/workout/<code>
  -> client.get_workout_detail(code)
  -> GET /api/app/v3/customTrainingTemplate/detailByCode?code=<code>
```

The dashboard Select/Export action in `templates/index.html` fetches
`/api/workout/<code>` for selected cards and creates a browser download. It is
useful for builder import/export, but omits the remote identity and several
builder fields. The CLI/service layer is the authoritative snapshot path.

## Commands

All commands are read-only. Credentials come from the existing local
`config.json` or the repo-wide `SPEEDIANCE_USER_ID`/`SPEEDIANCE_TOKEN`
environment override (same convention as `workout_ops.py` and
`test_e2e_workouts.py`); credentials are never copied or printed. Timezone
handling is strict `zoneinfo` + `tzdata` (canonical `America/Edmonton`); an
unresolvable timezone fails explicitly rather than silently falling back to
system-local time or UTC.

```powershell
python -m custom_workout_export export-all-custom-workouts `
  --output-root runtime/custom-workout-snapshots `
  --timezone America/Edmonton

python -m custom_workout_export export-custom-workout `
  --title "I-BK-v9" `
  --output runtime/custom-workout-export.json
```

`export-custom-workout` also accepts exactly one immutable `--id` or `--code`.
Exact-title lookup rejects zero or multiple matches.

## Contracts

### Workout definition

Schema: `speediance-custom-workout-export/v1`.

Each definition contains the immutable remote ID/code, title and description,
ordered exercises, ordered normalized sets, relevant builder source fields,
and a canonical SHA-256. The set model preserves target value, repetitions or
duration, capacity/weight, mode, rest, side, completion/count method, and
level. Arrays are never sorted.

### Snapshot manifest

Schema: `speediance-custom-workout-snapshot-manifest/v1`.

The manifest records source/export/failure counts, one entry per current
workout, parsed title family/version, canonical hash, artifact path, prior
hash, and `new`, `unchanged`, or `changed` status. A previous workout absent
from the current list is recorded in `missing_from_remote` with a factual
change artifact.

### Diff contract

Schema: `speediance-custom-workout-diff/v1`.

Diffs record title/version, exercise add/remove/reorder/identity changes, and
set add/remove/repetitions/duration/capacity/mode/rest/side/completion changes.
They do not infer health, progression, or gate-clear conclusions.

## Snapshot layout and completion

```text
runtime/custom-workout-snapshots/
  2026-07-10T001500-0600/
    manifest.json
    workouts/<immutable-id>__<slugged-title>.json
    changes/<immutable-id>__<slugged-title>.json
```

The exporter writes to a hidden staging directory, validates manifest
references, then atomically renames it to the final snapshot directory. A
partial export is explicitly marked `completed: false`; incomplete staging
snapshots are not accepted as previous-snapshot inputs.

Canonical JSON is UTF-8 with sorted object keys, compact separators, finite
numeric values, and preserved array order. Capture timestamps are excluded from
the workout-content hash.

## Versions, count limit, and future database mapping

Only a terminal `-v<digits>` or ` v<digits>` suffix is parsed. For example,
`I-BK-v9` becomes family `I-BK`, version `9`; `B REC-LB 1/1 v1` becomes family
`B REC-LB 1/1`, version `1`. Remote ID/code remains authoritative.

The dashboard displays a 50-workout indicator and the builder has a
50-exercise client-side limit. Neither proves an upstream custom-workout count
limit, so no limit is encoded in validation.

| Proposed future table | Key / relationship |
| --- | --- |
| `custom_workout` | Stable local lineage surrogate; survives remote removal via soft deletion (never physically deleted locally). |
| `custom_workout_remote_identity` | One row per historical remote ID/code attached to a lineage; remote removal + recreation appends a new identity row (remote identity is not preserved by recreation — see WORKOUT_OPERATIONS.md). |
| `custom_workout_version` | Immutable snapshot per canonical SHA-256, with parsed title family/version as metadata. |
| `custom_workout_exercise` | Version row plus exercise order (order preserved exactly). |
| `custom_workout_set` | Exercise row plus set number (order preserved exactly). |
| `exercise_reference` | Deduplicated group/action IDs after contract review. |
| `snapshot_import` | Snapshot ID and manifest provenance. |

This layout supports later derivation of capacity/weight, repetition, rest,
mode, and exercise changes per lineage, and later gate-clear /
strength-progression analytics — those interpretations belong in the analytics
layer, never in snapshots or diffs. Local soft deletion and lineage tracking
are future Precision Health/database work, not current implementation.

Unresolved: authoritative completion-method semantics, action-library ID
stability, description-field coverage, and future raw-payload retention policy.
An AI coach should consume only the normalized artifacts and diffs.

## API fragility

The service relies only on the current dashboard/edit endpoints. Relevant API
fields such as `setsAndReps`, `counterweight2`, `breakTime2`, `sportMode`,
`leftRight`, `completionMethod`, and `countType` are preserved as constrained
source fields while normalized sets provide a stable ingestion contract.
Revalidate the mapping whenever the upstream app/API changes.
