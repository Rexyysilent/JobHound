# Try, inspect, and reproduce

## No-account preview

Open `workbench/index.html` in a browser and select **Load synthetic demo**.
The four demonstration records are fictional. The workbench reads a local
JobHound `audit.json`, keeps its contents in memory, and shows saved decisions.
It does not rerank jobs, connect accounts, run collectors, send notifications,
submit applications, or store a persistent profile. Clear the view when done.

The workbench accepts either the file picker or drag-and-drop. Search and filter
by saved action band; open the evidence details before interpreting a score.
Untrusted title text is escaped and non-HTTP(S) links are not made clickable.
This is a review surface, not a production-hosted multi-user application.

## Reproduce the synthetic replay

After creating a Python virtual environment and installing `requirements.txt`:

```sh
python -m pytest -q
python -m pytest tests/test_pay_integrity.py tests/test_replay_integrity.py -q
```

To export the six-record synthetic replay and inspect it in the workbench:

```sh
# POSIX shell
JOBHOUND_EVIDENCE_DIR=review/synthetic python -m pytest tests/test_replay_integrity.py::test_actual_pipeline_round_trip_is_deterministic -q
```

```powershell
# PowerShell
$env:JOBHOUND_EVIDENCE_DIR = 'review/synthetic'
python -m pytest tests/test_replay_integrity.py::test_actual_pipeline_round_trip_is_deterministic -q
Remove-Item Env:JOBHOUND_EVIDENCE_DIR
```

Import `review/synthetic/review/audit.json` or the `v42` equivalent. The test
exports snapshots, digests and fingerprints for both policies at a fixed clock.
The source fixture is in `tests/test_replay_integrity.py`. Passing this corpus
is not a market-precision measurement and the synthetic profiles must not be
mistaken for a user's actual eligibility.

## Replay an existing capture

Use a private, isolated output directory, not the production `data` or `state`
directory. The existing command is:

```sh
python run.py review replay PATH_TO_SNAPSHOT --output-dir review/local-check
```

This reads saved inputs. It does not establish that a vacancy is still open
now. Review-only policy remains review-only; the production default was not
switched. Unsupported policy fields or malformed appended observations now
cause replay to stop instead of silently removing evidence.

**Important:** `python run.py run --dry-run` is not a no-write sandbox. It can
write stores/snapshots and run maintenance. Do not use it against production
configuration for experiments.

## What is not shipped yet

There is no one-click profile installer, profile-isolated hosted service, or
working outcome synchronization bridge. Some source/location defaults still
assume India. Those are explicit milestones in [the roadmap](ROADMAP.md), not
features to infer from the workbench.
