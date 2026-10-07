# Reviewed existing work and payment claims

Version 3 extends the offline `review outcomes` importer with a separate work
subject. It retains the provider, portal, public role, account and application
attempt binding, then adds opaque `project_id`, `allocation_id` and `revision`
identifiers. Company name similarity or a public role URL cannot establish an
allocation. A user-reviewed `work_selection` event puts that identity in `value`;
work facts put it in `work`. Required provenance fields remain unchanged. The
support hash binds supplied evidence; it does not authenticate accounts, mail or
bank records. Version 1 and 2 event shapes and actions remain compatible.

| Predicate | Claim and authority |
| --- | --- |
| `allocation_state` | Counterparty says allocated, revoked or finished. |
| `work_access` | Accessible or blocked for this exact allocation. |
| `payment_setup` | Verified, pending or blocked setup; it is not payable approval. |
| `payable_approval` | Counterparty says approved, pending or denied. |
| `work_terms` | Counterparty's positive decimal amount, explicit currency, opaque reference and native labor-hour/task/deliverable unit. |
| `work_checks` | User's explicit privacy, schedule, equipment, cost, scope, terms and economics compatibility. |
| `work_route` | Safe public HTTPS reference, never an authenticated dashboard link. |
| `work_cost` | Optional strict finite effort/cash estimate, with explicit cash currency and basis. |
| `work_state` | Submitted claim or counterparty acceptance; user reports cannot establish counterparty acceptance. |
| `invoice` | Counterparty invoice claim with amount, currency and opaque reference. |
| `collected_payment` | Recipient's user-reported receipt claim with amount, currency and opaque reference. Provider “sent” is not a recipient receipt. |

A selection excerpt is:

```json
{"schema_version":3,"predicate":"work_selection","actor":"user",
 "source_kind":"user_report",
 "value":{"project_id":"p-1","allocation_id":"a-1","revision":"terms-2"}}
```

Associated facts carry that same identity in `work`. This excerpt omits required
provenance and is not a complete import record. Complete synthetic controls are
in `tests/test_work_outcomes.py`. No personal facts are collected automatically.

The authoritative action policy recommends `start_allocated_task` only when
selection, allocation, access, setup, payable approval, safe route and all seven
user checks are current and supported for that exact subject. Contract terms
require known, unexpired non-seed evidence. Labor-hour terms must pass the
existing configured floor using supported FX. Task/deliverable amounts remain
native and require explicit economics review; they are not converted to hourly
wages. Unrelated assessments, attempts, projects, allocations and revisions
supply no prerequisites. Missing cost stays null and stale costs remain in the
journal. Legacy cost and timing are not borrowed; no time-to-cash is generated.

Public closure and an elapsed application deadline stop new applications. They
do not erase a supported existing allocation. Public blockers and closed source
observations remain in its audit. Location, qualifications, pay, scam and other
suitability vetoes still prevent starting work, including later risk refinement.

Submitted work recommends waiting for acceptance. Acceptance, an invoice or a
receipt claim recommends read-only `reconcile_payment_status` in the existing
records. This follow-up does not require a new public opening, assessment,
restored work access or eligibility for a new application. It remains Watch /
Verify, keeps qualification and closure evidence and contacts nobody. Unknown
dates, expired evidence, seeds and conflicting claims produce explicit checks.
Missing messages do not establish nonpayment. Invoices, acceptance, setup and
received cash never imply one another. A receipt claim proves neither bank
authentication nor full settlement.

Supersession is local to the predicate and exact work subject. Submitted /
accepted work, receipts and invoices require explicit reviewed correction edges.
Reminders cannot reopen revoked/finished work automatically. Incomparable claims
remain conflicts. Multiple receipt references stay separate journal entries;
this slice neither sums them nor decides partial/full settlement. Historical
conflicts on another allocation do not veto the selected allocation. Ambiguous
selection holds the action.

The engine computes the work plan once. The offline action artifact includes
that plan: target, attempt scope, current event IDs, blocking prerequisites,
terms and optional next-action cost. Current IDs mean projection membership,
not blanket freshness. Original claims remain in the journal/projection. There
is no payment integration, live mailbox import, production private-state write,
task execution or additional permission.
