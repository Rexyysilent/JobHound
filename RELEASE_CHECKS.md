# Public source verification

Prepared 2026-09-27 from the current private implementation. This is a
development source release, not a certification of all security properties or
of live job quality.

## What this export contains

- The full current implementation: V4.2 and V5.5 engines, v6 bounded fetching
  and batch retention (ATS boards, JSearch/Serper queries), production
  hydration with persistent verification history, v5.7 scoped outcome events
  (offline preview), review/audit tooling, the durable delivery outbox and the
  offline workbench.
- 129 package and test files are byte-identical to the private implementation.
  Eight carry only sanitization edits: neutral delivery defaults and one
  docstring in the package; example addresses, synthetic fixture paths and
  synthetic-provenance wording in six tests. One production-derived fixture is
  excluded and replaced by the existing synthetic ranking fixture.
- Public-only tooling is retained: synthetic test settings, acceptance
  contracts and the publication checker. CI (Linux and Windows) and contributor
  docs were carried forward from the earlier uplift review where they still
  apply; that review's code was not merged, because the current implementation
  supersedes it.

## Privacy boundary

- Explicit file selection; no private Git objects, identity metadata, personal
  configuration, profile, account or outcome states, delivery ledgers, captures,
  databases, logs or runtime request ledgers.
- Root settings are neutral examples with every source, LLM call, V5.5 key and
  delivery key off.
- Publication check (`tools/check_publication.py`, folder mode with an exact
  comparison against the maintainer's private `.env`, including encoded forms):
  **passed**, 167 files. The 31 reviewed exceptions are individually hash-bound
  synthetic regression strings or API-documentation placeholders.
- Additional manual sweeps for personal names, home paths, private project
  names, private fixtures and real email domains found nothing to remove beyond
  the edits listed above. New URLs relative to the previous release are
  synthetic examples plus one public ATS job-board endpoint used in a mocked test.

## Verification

- Unit/regression suite with synthetic settings and external network blocked:
  **1,181 passed, 1 skipped**, in three consecutive full runs. The skip is the
  optional historical live-cache test.
- `python -m compileall` and `git diff --check`: clean.
- Acceptance adapter: **100 cases, 0 adapter errors**. Evaluator: **204 of 204
  assertions pass**. The first export draft found two contracts that the v6
  changes had regressed (`DOC-03`, a discussion page classified as a vacancy;
  `OPS-01`, a rate-limited resolution request missing from the HTTP error
  count). Both were fixed test-first in the private implementation and
  re-exported here, together with a related case where real "Discussion
  Moderator" roles were rejected as discussions. Replays of two production
  snapshots kept identical decision fingerprints.

These tests are not a live-market, precision/recall or production-rollout
certification.

## Limits

The private repository's history is **not cleared for publication**; only this
explicit file selection is. Review the staged bytes with
`python tools/check_publication.py --index` before any push. No open-source
license has been selected. A clean scan cannot detect every unknown secret, and
normal use creates private files that must stay ignored.
