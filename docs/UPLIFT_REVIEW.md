# Evidence-integrity uplift, 2026-09-17

Baseline: `58bab4df053f60c8edf09d4e7a3d87a72957d18f`.
The README is preserved byte-for-byte. This is a review branch, not a production
rollout or a claim of measured recommendation precision.

## Changes

- Greenhouse, Lever and Ashby keep successful boards when another fails. Board
  health distinguishes bad schemas, empty success, request failure and budget
  deferral. Access denial/rate limiting stops the affected batch without bypass.
- Native compensation amounts and units survive extraction, assessment and
  rendering. Audio/task/item pay and calendar salary are no longer silently
  converted into working-hour rates by the V4.2/V5 assessment paths. Explicit
  caller-supplied labor conversion remains a labeled estimate in the parser.
- Mixed currencies, reversed ranges, multi-claim ambiguity and malformed values
  abstain rather than falling back into the legacy parser. Invalid structured
  claims remain a conflict/verification obligation, not innocuous unknown pay.
- Native amount/currency/unit/qualifier comparisons prevent equal null hourly
  fields from becoming false corroboration. Pay conflict suppresses economics
  promotion. Low-credibility salary is not labeled credible merely for having a
  calendar unit. “Pay resolved” requires a supported nonconflicting claim.
- Page-type gates reject directories, videos and non-opportunity documents in
  the deployed V4.2 decision path as well as the review policy. Positive controls
  retain individual roles with discussion duties or talent-network footers.
- Replay rejects unsupported policy fields and malformed appended observations;
  it does not silently drop evidence that could contain a blocker.
- A read-only local workbench, contribution guide and staged adoption backlog
  make the existing review outputs inspectable without a new hosted service.

The legacy V3 rollback engine was not redesigned. The review-only policy was not
promoted to production. Source expansion, global-context isolation, identity
migration, a durable outbox, private outcome synchronization and live validation
remain future work, not implied features.

## Verification

Fresh baseline CI passed on Linux (Python 3.11/3.13) and Windows (Python 3.12).
The Linux baseline was 608 passed, 2 skipped. The Linux artifact supplied the
complete source and dependency wheels for an offline local environment.

Local integrated suite: **717 passed, 2 skipped**. The new assertions reproduce
failures before fixes and exercise real modules, not algorithm stand-ins. The
existing 401k regression now checks native annual amounts (56,500-73,400), while
still proving that the benefit number is not consumed; its previous implicit
2,080-hour conversion assertion was intentionally replaced.

The six-record same-input replay ran under both policies before and after the
change. Repeated replay fingerprints matched, and decision/presentation ledgers
passed. All fixtures are synthetic; there is no independent real-market label
set or measured recall in this check. See `replay-comparison.json` for the native
unit and action projections, including retained genuine-role controls.

Chromium workbench checks: **10 passed**, no remote requests or script errors.
The checks covered file import, real drag-and-drop events, malformed input,
untrusted text/URLs, search, filtering, clearing and narrow-screen overflow.
HTML was loaded through `set_content` because file navigation was restricted in
the environment. This is not cross-browser or accessibility certification.

Before merge, verify the current CI checks on the exact PR head. Local counts
and earlier CI runs must not be substituted for a later failing head.

## Reproduction and limits

Follow [GETTING_STARTED](GETTING_STARTED.md). Keep real captures and outcomes
private. A valid replay reproduces saved evidence, not present-day vacancy
availability. Precision/retention targets require a separately labelled holdout
including rejected and hidden candidates. No production database, scheduler,
mailbox, application, paid bid or provider credential was changed.
