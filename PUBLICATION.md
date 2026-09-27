# Public-release boundary

Included: current Python implementation, portable launcher, requirements,
blank/neutral configuration examples, synthetic regression tests and executable
acceptance contracts. All source files are inspectable text.

Excluded: the private repository's Git history and identity metadata; real
credentials; personal config and profile; signup, application and outcome
states; delivery ledgers; trust-feedback events; private handoffs; roadmap
archives; live captures and production-derived fixtures; cached HTML; email
digests; database rows; runtime request ledgers; screenshots; notebooks; build
output and virtual environments.

This public repository has its own history. Each export is a new commit built
from an explicit file selection of the current private implementation; the
private history is never pushed or merged. Never assume that sanitized current
files erase anything already present in older history.

## Verification expectations

- Scan every release file for configured secret values and encoded equivalents,
  credential formats, credential-bearing URLs, emails and user-home paths.
- Manually review test-only dummy secrets; do not blanket-exempt tests.
- Scan historical objects separately to decide whether old history is publishable.
- Run tests using only synthetic settings and no private credentials or captures.
- Check a clean copy without caches, private files or access to the old checkout.
- Check the exact Git staging set again immediately before any future push.

Run the included checker from this directory:

```powershell
python tools/check_publication.py
# After staging the reviewed files:
python tools/check_publication.py --index
```

Optionally add `--env-file <path-to-private-env>` for an exact-value comparison,
including common encoded forms. The report never prints those values. Folder
mode intentionally fails if you have added local private settings; index mode
checks the actual staged bytes. The exception manifest waives only individually
reviewed synthetic test lines, bound to both their path and full-line hash.

No scanner proves the absence of every possible secret. Exclusion by provenance
and an explicit file selection are the primary privacy protections. Rotating any
credential previously shared in logs/screenshots remains a separate account action.

Local `.env` and personal YAML files may be changed by normal use but must stay
ignored. Review generated outputs before sharing: a URL or email body can carry
identifiers even when routine HTTP logging is redacted.
