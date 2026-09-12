# Public source verification

Prepared 2026-09-12. This is a development source release, not a certification of
all security properties or of live job quality.

## Privacy boundary

- Explicit file selection; no private Git objects or identity metadata included.
- No operator emails, home paths, credentials, local configuration, signup state,
  application results or trust-feedback narratives included.
- Root settings are blank/neutral examples. Runtime source and delivery switches
  are off. Detection rule catalogs are retained, without operator declarations.
- Scraped descriptions and database-history metadata were replaced with compact
  synthetic regression inputs. Private cache content is not required by the suite.
- Roadmaps, handoffs, archives, images, notebooks, captures, logs and databases
  remain outside the public tree. The release is approximately 1.2 MB of text.

## Verification

- Fresh Python 3.12 virtual environment installed from requirements.txt.
- Unit/regression suite: **609 passed, 1 skipped**. The skip is the optional
  historical live-cache test. External network connections are blocked in tests;
  loopback is allowed for Windows asyncio's internal wakeup sockets.
- Acceptance adapter: **100 cases, 0 adapter errors**; evaluator: **204 assertions
  passed**. These tests are not a live-market or production-rollout certification.
- Publication scan: configured private values and common encoded forms checked;
  provider-token, URL-credential, header, email and home-path checks applied.
  No unresolved findings in the release. The 26 exact reviewed exceptions are
  synthetic regression strings or literal API-documentation placeholders.
- Regression tests cover private-file exclusions, local/example precedence,
  example-policy hashing, pre-import test isolation, safe staged-byte scanning,
  rejection of subtree-only index scans, and fresh Windows log-directory creation.

## Limits and next actions

The private repository's old history is **not cleared for publication**. Create
new history from this directory. Review the exact staged bytes with the included
checker before pushing. Never force-add ignored local files. No repository was
uploaded and no credential was rotated as part of this preparation.

The package does not include an open-source license; choose one before granting
reuse rights. V5.5 remains review-only. A clean scan cannot detect every unknown
secret, and normal future use can create private files that must remain excluded.
