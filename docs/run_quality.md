# Diagnose the quality of a saved run

The quality report answers a practical question: **where did this run stop
producing usable next actions?** It examines every saved decision, including
hidden cards, rejections and status updates. It does not contact employers,
rerank a run, load operator settings, open a database, or deliver messages.

## Run it

```sh
python -m jobhound.review_audit quality review/first-look/audit.compact.json
python -m jobhound.review_audit quality review/first-look/audit.json --top 20
```

The command prints JSON to standard output and does not modify its input.
Both full and compact audits are accepted up to the existing 32 MiB reader
limit. Duplicate JSON keys, non-finite numbers, invalid decision shapes,
inconsistent canonical counts and malformed diagnostic fields are rejected.
`--top` accepts 1–100 and bounds missing-fact and source groups; it never limits
the decisions examined. Omitted group/candidate counts are explicit.

For Python callers:

```python
from jobhound.review_audit import load_audit
from jobhound.quality import quality_report

report = quality_report(load_audit("review/first-look/audit.compact.json"))
print(report["cohort"])
print(report["active_verification"]["missing_facts"])
```

## Outputs from review capture and replay

`run.py review capture` and `run.py review replay` automatically add two files
to their output directory:

| File | Purpose |
|---|---|
| `audit.json` | Existing full audit, unchanged for current consumers |
| `audit.compact.json` | Every decision with small observation-status projections; full source text remains in the snapshot |
| `quality.json` | Whole-cohort funnel, evidence coverage, missing facts, source yield and warnings |

The compact audit is the preferred workbench input. It is bounded to the same
32 MiB limit as the audit reader. If a compact export itself exceeds that limit,
review completes with its full audit and snapshot, and still writes the small
quality report where it fits. The manifest records `omitted_size_limit`, the
projected byte size and the limit; it does not claim a compact file was saved
or silently discard candidates. A stale compact file in a reused review
directory is removed. The explicit `write_compact_audit` API retains its
existing strict size/error and no-overwrite behavior.

Capture's `manifest.json` and replay's `replay_manifest.json` include
`compact_audit`, `quality_report`, their byte-level SHA-256 values, and the
SHA-256 of the exact snapshot used. These paths are additive; the existing
full-audit filename and capture/replay return values are preserved. The compact
audit's snapshot reference is inert data. The quality command does not open
that referenced path or claim to have verified its current contents.

## Read the funnel

The `cohort` categories are mutually exclusive and add up to `total`:

| Category | What it means |
|---|---|
| `rejected` | Saved final action band is reject |
| `watch_or_status` | A non-rejected record has a recorded, non-active lifecycle |
| `active_verify` | Active record whose final action band is verify |
| `application_ready` | Active PRIMARY/apply with passed eligibility, recorded empty blockers and verification tasks, saved application readiness, and complete references supporting matching role content, a role application route and a verified-open timestamp at or before the run clock |
| `other_ready_action` | Active PRIMARY with passed eligibility and no recorded blockers/tasks, for allocated work or a captured invitation, assessment or scope-clarification step |
| `active_primary_unconfirmed` | A PRIMARY label without consistent recorded readiness/evidence |
| `lifecycle_unknown` | A non-rejected record lacks a usable saved lifecycle |

Application readiness is assessed at the saved run's `metadata.as_of`, using
the saved policy's readiness conclusion. The diagnostic checks the supporting
saved fields; it does not reapply the current policy's freshness TTL or assert
that a listing remains open today. A captured next step is counted separately
from a new application. Neither category establishes selection, allocation
or eventual payment.

`partition_ok` only checks this report's row accounting.
`upstream_accounting_ok` preserves the original engine's accounting result;
one cannot hide a failure in the other.

## Missing facts and evidence support

`active_verification.missing_facts` counts **candidates**, not repeated task
rows: one candidate with two identical current-opening checks contributes one
to that missing fact. Status/rejected records do not inflate this list.
Unrecorded task arrays and tasks lacking a fact code have separate counts.

`saved_statuses` reports saved eligibility, readiness and source-authority
states. Missing fields appear as `not_recorded`; they are not converted to
failed eligibility, absent compensation or a closed vacancy.

`evidence_support` summarizes matching role content, application routes and
open-state evidence from observation references. `unverified` means the saved
observations do not support a positive conclusion. `not_recorded` means the
audit lacks the necessary evidence projection. An exact matched closure
takes precedence over an open observation, except when a later complete original
for the same posting explicitly renews a deadline that caused the older closure.
The engine and this report share that status-resolution rule. A scoped closure
notice remains closed, a copy cannot extend an original's deadline, and future
captures cannot change status at the saved run clock. `deadline_elapsed` records
an elapsed effective deadline even if an observation still carries an older
verified-open flag. Compact audits retain the small source/capture/deadline
projection needed for these checks; historical observations remain intact.
Missing references prevent a PRIMARY label from being counted as application-ready.

The warning `all_active_candidates_require_verification` identifies a run
that has generated a verification queue but no stronger active decisions.
`no_verified_actions` flags the absence of supported ready actions in the
saved fields. Neither warning proves that no suitable work exists.

## Source yield and collection coverage

`source_yield` groups by the retained canonical `job.source`. Its denominator
includes every canonical decision assigned to that source, including rejected
and status records. `application_ready_share` is this cohort share. It is not
an estimate of hiring probability, source precision, or the causal contribution
of a collector. A role discovered by a search provider and resolved to an ATS
can be attributed to the retained ATS source.

`coverage` summarizes recorded source-health rows. Missing coverage remains
unknown; partial collection, request deferral and failure do not imply vacancy
closure. A source named by a retained job but missing from source-health rows
is flagged for inspection. This may also reflect a discovery/resolution naming
difference. The report cannot infer the expected configured sources from an
audit that does not record them.

## Identify the code that ran

`provenance` carries the saved run ID, run clock, engine version, code and
dependency content hashes, profile/config/ruleset/trust hashes, and snapshot
hash when recorded. Null values remain null. The code content hash is not a
Git commit ID: it identifies the runtime files recorded by the engine. It can
help diagnose deployment lag without guessing a deployed commit from an email
header or a repository's latest merge.

`audit_fingerprint` hashes canonical JSON of the entire supplied audit. It
binds the report to the saved input; manifest file hashes additionally identify
the exact exported bytes. Identical input and `--top` produce identical report
content, with no dependency on the current date or network state.

## Accuracy needs separate evidence

`external_accuracy.state` is always `not_measured` and precision/recall are
null. A run with many PRIMARY labels is not proven accurate, and an all-VERIFY
run is not proven to have found no useful jobs. Measuring accuracy requires
independent review of original vacancies and applicant constraints, including
both surfaced and rejected candidates. Synthetic regression cases establish
specific behavior, not a population precision estimate.
