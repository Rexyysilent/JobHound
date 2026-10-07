# Reviewed assessment steps and revisions

An application attempt can contain several assessments. A completion, route or
privacy approval for one step does not describe another step, or changed terms
for the same step. Outcome history remains append-only and scoped to the existing
provider, portal, role, account and attempt binding.

Version 2 outcome events support an `AssessmentIdentity` with opaque `step_id`
and `revision` strings. The provider's `assessment_step` event selects the current
identity in its `value`. Assessment state, route, user action checks and optional
next-action cost carry that identity in their `assessment` field. Each still
requires the usual reviewed evidence reference, support hash, source, actor and
dated observation. A valid hash does not authenticate the source.

For example, a selection event can contain:

```json
{
  "schema_version": 2,
  "predicate": "assessment_step",
  "value": {"step_id": "english-screen", "revision": "terms-2"},
  "actor": "provider"
}
```

This is an excerpt, not a complete import record. The associated invitation,
public route, four explicit user compatibility checks and optional effort/cash
estimate use `assessment: {"step_id": "english-screen", "revision": "terms-2"}`.
Authenticated or credential-bearing routes remain outside imports.

The projection groups facts by predicate and exact assessment identity. Older
step histories and their conflicts remain visible; they do not decide the action
for a different provider-selected step. Supersession cannot cross assessment
identities. A newer reminder cannot automatically replace completed/passed state
for the same revision; a reviewed explicit correction must identify the prior
fact. Cross-actor disagreements remain conflicts without such a correction.

Recommending the selected assessment requires current selection, invitation,
public route and explicit privacy, schedule, equipment and cost compatibility for
that exact identity. Unknown event times, expired facts, dated seeds or checks
for a different revision do not meet those requirements. Missing selection or
state creates a specific verification task. Completion stops a repeat of that
step; it establishes neither selection nor paid allocation. Public suitability
vetoes, exact application rejection and access blocks keep their existing force.

The offline action artifact includes the selected identity, attempt scope,
current event IDs and its stated next-action cost, when current and applicable to
the recommended assessment. Waiting or verification does not inherit the cost
of a completed assessment. A stale, unknown-time, expired or seed-only estimate
stays in the journal rather than becoming the current next-action cost. Current event
IDs describe projection membership; the recommendation separately checks
freshness and prerequisites. Missing cost stays null. Other step costs and total
project effort are not substituted. These artifacts recommend actions; they do
not execute an assessment or import facts into production.

Version 1 event serialization and the existing legacy action path remain
compatible. A journal containing step-scoped facts requires an explicit selected
identity before those facts can recommend an assessment. Setup, allocation,
work acceptance and payment claims use the separate version 3 work contract in
`work_outcomes.md`; assessment facts do not establish those outcomes or forecast
earnings.
