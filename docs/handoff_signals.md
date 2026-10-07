# Disposable handoff signals

The optional E path uses the existing decision engine, PriorityKey, durable
outbox and envelope transport. Production delivery defaults stay unchanged.
`record_delivery_run(..., handoff_signal=True)` requires an explicitly enabled
disposable review store; production mode and channel-only legacy adoption reject it.

Run the actual offline CLI with a saved snapshot and explicitly reviewed facts:

```powershell
python -B run.py review outcomes snapshot.jsonl --events events.jsonl --bindings bindings.jsonl --as-of 2026-10-04T12:00:00+00:00 --output-dir new-review --handoff-signal
```

`--delivery-state prior-review/delivery.sqlite` copies prior marked disposable
state through SQLite's read-only backup API. It never opens production state for
writing and never dispatches a provider. Use `--previous-events` for the separate
validated evidence journal. A prepared preview is not a receipt. Source input
paths, journals, raw evidence, audit rows and database copies remain private.

The selection shares five ready action slots across new discovery and existing
pipeline actions, plus two bounded Verify slots. One suitable new discovery is
reserved when available. Employer/marketplace breadth caps and PriorityKey still
apply. A lower transport cap can reduce this total. There is no quota for weak
leads. Status uses one compact summary of at most three changed opportunities;
the uncapped disposition audit retains overflow, held and already recorded rows.
Coverage has a separate operational denominator and bounded stream.

The V5.5 PriorityKey compares, in order: supported scoped urgency, earlier
supported due time, action readiness, substantive buyer update, supported time
to cash, match, role fit, action cost, economics, language, source, trust,
freshness and stable identity. The three added fields serialize only when
nonzero; ordinary discovery and legacy priorities retain their existing shape.
No score tuning is needed for these evidence dimensions.

For example, a ready allocated task with a current exact-allocation due time
in six hours and a reviewed one-hour estimate precedes a cold application.
Between two such ready tasks, the earlier due time wins. A stale, unknown or
infeasible estimate supplies no urgency. A current actor-confirmed buyer scope
or budget revision precedes an otherwise equally ready Verify item; a seller
reply, thanks, quoted change or refreshed capture does not. The original job
posting date stays unchanged. These are review priorities, not permission to act.

The discovery reservation is displaced only when all ready slots, after breadth
caps, are occupied by supported urgent existing actions. The ledger records the
displaced discovery and exact scope/due-time evidence for each urgent action.
Four urgent tasks in a five-slot selection still leave a discovery slot.
The 24-hour urgency window is a local review policy. A typed counterparty due
time and any stated minimum lead belong to one assessment or allocation;
duration must also be a finite literal number, current and scoped. Boolean or
string coercion cannot establish an assessment time estimate. Missing feasibility creates a Verify
task. An elapsed due time requires an explicit extension and does not close the
public vacancy. Completed steps and financial status do not inherit old urgency.

`jobhound-material-action/v2` retains the selected assessment/work scope, attempt,
stage, action, access, supported native terms, material costs, prerequisites,
real captured deadline and explicit public buyer terms revision. It excludes
ranking, capture time, cosmetic instructions and inactive step/allocation facts.
It retains raw history in the immutable payload. Monetary decimal formatting
does not change the amount; opaque identities/references are never normalized.

Schema migration reconstructs the new projection from an immutable intent with
an actual accepted event for that exact channel and hashed destination. Equivalent
material advances that baseline with append-only adoption evidence, without
creating a fake accepted intent. Missing provenance, including channel-only
receipts, produces `migration_review_required`. A different destination retains
its own receipt state. Reviewed identity aliases preserve the established stream.
Failed, pending, previewed and uncertain sends cannot establish a baseline.

`signal-receipt.json` contains separate selection/action, import/event, coverage,
preparation and receipt-state evidence. `preview.md` is compact; the ordinary
uncapped outcome preview is retained as `outcome-preview-uncapped.md`. Frozen
or uncertain older envelopes can hold a new prepared preview. A copied review
may retire a wholly obsolete draft only when every part and intent has zero
attempts and no provider history. Its body and membership remain frozen and the
cancellation event stays in the audit. Mixed drafts with current members require
explicit reissue review; attempted or uncertain drafts retain their holds. The
renderer rejects attempts to combine
several runs into an envelope exceeding the handoff caps.

Rollback requires restoring the pre-handoff disposable database together with
the previous code. Switching a migrated review database to legacy staging is
rejected to avoid a schema-change resend. No production migration is performed.

Mixed-current recovery uses an explicit `--reissue-plan plan.json` together with
`--handoff-signal --delivery-state prior-review/delivery.sqlite`. The prior state
must be a closed marked copy with no WAL, SHM or journal sidecar. Obtain its SHA256
after closing any inspection connection. Plan input is bounded to 64 KiB, ten
drafts and one hundred unique intent IDs; extra fields, duplicate JSON keys,
boolean/string IDs and nonliteral approval are rejected.

The plan has `schema_version: 1`, `reviewed: true`, an opaque `evidence_ref`,
`source_state_sha256`, `drafts` and optional `pending_intent_ids`. Each draft
contains its exact `envelope`, `body_sha256` and every still-current `intent_ids`.
The `preview_lifecycle` audit lists current and obsolete members separately.
Review these against the new projection before selecting them. Do not include
obsolete IDs. A changed body, source checksum, current scope, recipient, missing
current member or already-reissued member rejects the plan atomically.

Recovered members get linked notification generations with the same revision and
payload; every old body, mapping and event stays in the audit. The entire draft,
including its obsolete members, must have no provider attempt/receipt history.
Reissued and current-run signals share five action/two Verify/three status caps,
the same PriorityKey, discovery reservation and breadth controls. Unshown members
remain pending and appear in `deferred_pending`; another explicit plan can select
their exact unfrozen IDs via `pending_intent_ids`. No plan means no backlog drain.
Run/revisions, generation creation and both destination freezes share one SQL
transaction. Renderer/persistence failures roll it back. Prepared envelopes remain
previews, and an accepted email cannot consume the Telegram baseline.

`reviewed_reissue` keeps generation lineage, draft resolutions, a separate
preparation disposition for every current opportunity, displayed/deferred IDs and
origin labels. `selection` remains the immutable staging ledger. Existing queued
actions and notification generations are not counted as newly discovered vacancies.

Remaining E work: authenticated receipt reconciliation for attempted/uncertain envelopes and channel mapping
for any separately authorized production rollout. Reminders and public application
deadlines do not establish urgency for private allocated work. Financial updates
retain invoice, acceptance and recipient-reported receipt as separate claims.
The synthetic suite is regression evidence, not independent precision or retention.
