# Reviewed outcome aliases

`review outcomes` remains an offline file review. It opens no mailbox, writes no
production store and sends no notification. Existing bindings retain their exact
provider, portal, role, account and attempt scopes; the event journal and every
attempt projection are preserved independently of action selection.

An original-page hydration can change a canonical ID while retaining the older
discovery observation. Supply `--aliases aliases.jsonl` only for explicitly
reviewed migrations. Each JSONL record has these fields:

```json
{
  "schema_version": 1,
  "alias_id": "reviewed:original-page-migration",
  "from_canonical_id": "older-canonical-id",
  "to_canonical_id": "current-canonical-id",
  "observation_id": "retained-observation-id",
  "role_url": "https://jobs.example.org/role-one",
  "observation_sha256": "REPLACE_WITH_64_LOWERCASE_HEX_CHARACTERS",
  "reviewed": true
}
```

The example is a schema illustration, not an importable migration. The source
canonical must be absent from the current run; the destination must exist. The
binding and alias must share their exact retained observation and public role
URL. That observation must belong to only the destination canonical. Hash it
with `jobhound.v41.outcome_aliases.observation_binding_hash`, using the current
evaluated observation exported in the full audit. The function hashes its complete
sanitized model, including native content and evidence projections. Hashing an
unprojected input object may differ. Changed content requires a new review.

Full audits now expose `observation_ids`, `field_sources`, `alternate_urls`,
`legacy_canonical_id` and the canonicalization reason on each decision. Those
references let a reviewer inspect why an old discovery and its original page
belong to the current canonical. The alias records reviewed historical ownership;
the reader validates the exact current anchor. This does not authenticate a mail
message or prove that the old canonical ownership assertion is true.

```powershell
python run.py review outcomes saved_snapshot.jsonl --events events.jsonl --bindings bindings.jsonl --aliases aliases.jsonl --previous-events previous/events.jsonl --as-of 2026-10-03T15:00:00+00:00 --output-dir new-review
```

All input/output paths are explicit. The output must be a new nonproduction
directory. Alias/event inputs have the existing 8 MB, 5,000-record and 16 KB
per-record bounds. Only one-hop mappings are admitted; there is no company/title
matching, recursive alias chain or automatic choice of application attempt.

An invalid or ambiguous alias is listed in `alias_quarantine.json`. Its binding
is held in `binding_resolutions.json` while the facts remain in `events.jsonl`
and `projections.json`. An unmapped assertion cannot close or reopen a job.
If several valid active bindings resolve to one exact role, all those selections
are held and that role receives a concrete `active_outcome_attempt` verification
task. Other independently resolved roles continue through the normal action
policy. Mark historical attempts `active: false`; preserve them in the bindings
file and journal rather than deleting their history.

The output includes accepted `aliases.jsonl`, original `bindings.jsonl`,
`active_attempts.json`, binding resolutions, alias quarantine, the full audit,
action/status preview and a manifest binding the exact alias file, snapshot,
event inputs and runtime versions. `active_attempts.json` records the requested
selection; consult binding resolutions before interpreting it as a resolved
current action. Projection fingerprints include both the retained history and
resolved mapping, independent of input ordering.

Library callers that reuse a previous preview revalidate its selected projection
events against the supplied complete bindings. Missing historical scopes or
contradictory prior event IDs fail validation; an old preview's selected action
is cleared before applying the new selection. Use the complete exported journal
to retain inactive attempts that are not attached to the prior selected projection.

Without `--aliases`, the existing strict exact-binding behavior remains: unknown
canonicals and multiple active selections fail the review. An alias review with
held facts establishes neither selection, work allocation nor collected payment.
