# Posting identity and notification compatibility

Updated October 23, 2026.

## Why separate postings must survive collection

An employer can publish several openings with the same title. They may have
different countries, application links, language requirements or compensation.
Merging those records before assessment can discard a suitable opening or
attach another opening's requirements to it.

The V3 pipeline previously inferred duplicates from fuzzy company/title matches.
Its ID also discarded every query parameter and lowercased URL paths. The
default V4.2 engine used a separate clustering implementation: remote locations
could match despite different countries, and canonical IDs omitted the URL.
That allowed two separately retained openings to collide again during storage.

## Current identity rules

| Engine | Evidence needed to collapse ordinary observations | Stable identity |
| --- | --- | --- |
| V3 | Matching normalized title, company, posting URL, location, remote flag and region-tag set | Each record's own posting and applicant scope |
| Default V4.2 | Matching counterparty, exact normalized title, exact normalized location and public posting URL | Normalized identity plus posting URL |
| V5.5/V6 | The same exact posting key; accepted original-page lineage can also join a discovery row to its verified original | Existing URL-bound formula, with corrected name normalization |

V4.2 also accepts explicit, successful hydration lineage. Similar titles or a
shared remote label do not establish that relationship. Unknown location does
not act as a wildcard for a known country. Recognized company aliases still
identify one counterparty, but that identity alone does not merge its vacancies.

For example, these stay separate:

- The same employer's remote India and remote United States openings.
- Two requisition links with identical employer/title/location values.
- Case-sensitive posting paths or different requisition query values.
- Different employers or titles occupying one shared board URL.
- Bengali names and specialisms such as C++ and C#.

Actual duplicates still retain source corroboration and prefer stronger source
authority. The modern engine preserves every observation and its field
provenance. Lower-authority compensation and requirement claims continue to be
assessed independently when there is evidence that they describe the same job.

The existing modern public-URL sanitation policy is unchanged. V3 now strips
only recognized `utm_*` campaign parameters and the click identifiers `gclid`,
`fbclid`, `mc_cid` and `mc_eid`. It preserves unknown query components, repeated
parameters, their order, path case and client-side fragments. Generic `ref` and
`source` values are not assumed to be tracking in V3.

## Determinism and unresolved records

An ID never depends on whether another vacancy happened to appear in the same
batch. Canonical output is sorted by identity. Equally ranked observations are
selected by their content before their positional observation IDs; changing the
capture clock alone cannot switch the selected description.

Observation IDs remain intact because evidence and outcome links use them.
Malformed or absent public URLs do not establish an ordinary posting identity.
The modern engine uses a content-bound unresolved identity in that situation,
preserving different unresolved records without assigning them the same database
key. Changed unresolved content may therefore produce a different unresolved ID.

This is a posting-identity policy, not a claim that every conflicting statement
identifies another job. Contradictory geography in descriptions of the exact same
posting remains available to assessment and can cause a location rejection.
The PR does not change those interpretation rules.

## Existing stores and notifications

### Default V4.2

V4.2 now uses URL-bound IDs consistently, including when one vacancy appears by
itself. Every canonical also exposes its actual historical `legacy_canonical_id`.
The historical normalizer is retained only to locate old records; it must not
be reused to merge current postings.

Before classifying a previously delivered posting as new, the V4.2 transition
code can read its old successful receipt. All of the following must hold:

1. The receipt joins to its original `run_jobs` row by both run and canonical ID.
2. That immutable run row contains the same public posting URL, preserving path
   and query case.
3. Its linked normalized observations unambiguously match the current title,
   counterparty and location under the corrected name rules.
4. Every linked observation is present and readable; the stored action and pay
   baseline are valid.

The mutable `canonical_jobs.current_job_json` snapshot is not evidence of what
an earlier notification contained. A later scrape of another opening cannot
borrow the earlier opening's receipt. A failed send is not a successful receipt.
An ambiguous or incomplete old record does not silently suppress a current job.

This lookup is read-only: old rows and receipts are preserved, and no global
aliases are inserted. A failed migrated promotion or pay update retains the
successful old receipt as its comparison baseline until a new-ID send succeeds.
After that point the existing V4.2 transition behavior applies. This is not a
replacement for the V5.5 durable outbox or a general delivery-retry redesign.

### V3 and V5.5/V6 boundaries

V3 preserves historical IDs for compatible unscoped HTTPS/ASCII records.
Previously discarded geography, meaningful URL content, Unicode or specialism
symbols can require a new ID and a one-time resurfacing. V3's seen store has no
equivalent immutable receipt proof, so this patch does not guess history
transfers from employer/title similarity.

Ordinary single-posting ASCII V5.5 identities retain the existing URL-bound
formula. Records affected by previously erased characters, split same-URL
counterparties or accepted-lineage selection ties can change identity. Existing
outcome aliases and durable-outbox migration remain separate reviewed workflows;
the new V4.2 receipt proof is not applied to those paths. In particular, it does
not replace the older outbox's public-URL legacy-hold crosswalk. Review affected
stored outcomes and legacy holds before changing a production installation.

## Verification

All new fixtures are synthetic. The regression panels exercise raw provider
normalization, V3 final verdicts, V4.2 assessment and accounting, independent
requisitions, Unicode names, repeated captures, snapshot replay and disposable
SQLite history. Positive controls retain confirmed duplicates, source authority,
compensation conflicts and accepted hydration lineage.

```sh
python -m pytest -q \
  tests/test_legacy_opportunity_identity.py \
  tests/test_opportunity_canonicalization.py \
  tests/test_opportunity_retention.py \
  tests/test_opportunity_receipt_compatibility.py
python -m pytest -q
python tools/check_publication.py --index
```

These checks do not measure live-provider coverage, recommendation quality or
delivery to a real inbox. No source configuration, credentials, live job capture,
operator database or notification destination is included in the change.
