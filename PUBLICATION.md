# Public-release boundary

Included: current Python implementation, portable launcher, requirements,
blank/neutral configuration examples, synthetic regression tests and executable
acceptance contracts. All source files are inspectable text.

Excluded: Git history and identity metadata; real credentials; personal config
and profile; signup and application states; trust-feedback events; private
handoffs; roadmap archives; live captures; cached HTML; email digests; database
rows; screenshots; notebooks; build output and virtual environments.

The source export deliberately has no Git repository. Initialize new history
here only after reviewing the release and your author identity. Never push the
original private history on the assumption that sanitized current files erase it.

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
# After initializing a NEW repository and staging reviewed files:
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
