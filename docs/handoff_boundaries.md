# Compensation conditions, equipment and exact roles

These checks use the existing V5.5 evidence and action paths. They do not enable
new sources, import personal records or authorize applications and work.

## Vendor estimates and payment conditions

An amount such as USD 24 per 60 audio minutes is an **output audio-hour** rate.
It is not USD 24 per hour worked. A publisher's estimate of five audio minutes
processed per working hour remains a separately labeled estimate; the engine
does not turn it into guaranteed earnings or a ranking wage.

The selected compensation observation retains literal vendor work estimates,
approval conditions, approval-processing durations and payment schedules with
source spans and hashes. A payment delay after approval does not establish the
approval date or time to first payment. Conflicting estimates remain separate
evidence. Another observation's terms cannot supply the selected role's timing.

Digest pay lines show at most six distinct condition excerpts of 220 characters
each; shortened or omitted conditions have an explicit full-audit notice. The
complete evidence stays in the assessment. Closed projects stay closed even
when their old recruiting text still advertises pay and vendor work estimates.
Recognition covers bounded explicit English clauses, not arbitrary contracts.

## Optional equipment declarations

The optional profile `equipment_evidence` list records explicit dated inventory.
For example, this fictional operator has a phone and reports no webcam:

```yaml
equipment_evidence:
  - device: webcam
    available: false
    evidence_id: demo-webcam-report
    source: fictional_operator_declaration
    event_date: '2026-09-01'
  - device: phone
    available: true
    evidence_id: demo-phone-report
    source: fictional_operator_declaration
    event_date: '2026-09-01'
```

Supported devices are `webcam` and `phone`; `available` is a literal boolean or
null. Dates are ISO calendar dates or null. A profile without active declarations
retains its existing behavior. No hardware facts are inferred from a listing.

For a complete, identity-matched original role requiring a webcam, explicit
absence produces a compatibility verification step, not an account rejection.
A phone does not establish webcam compatibility. New dated webcam evidence can
resolve that dimension while other eligibility and action checks remain active.
Future declarations are ignored at the frozen run date; undated or conflicting
declarations remain uncertain.

Preferred, optional and negated webcam demands do not become mandatory. Explicit
device alternatives remain uncertain unless the webcam branch is supported;
the parser does not approve a phone-as-webcam workaround. General inventories do
not establish device specifications or provider compatibility automatically.

An assessment-only webcam demand does not block discovery. It is checked before
the invited assessment; generic equipment approval cannot erase webcam-specific
absence. Completed assessment/work bookkeeping remains available. These are
bounded webcam clauses, not a universal hardware-requirements parser.

## Multiple job records on one page

When a generic page contains multiple JSON-LD JobPosting objects, hydration
requires exactly one explicit role URL matching the requested leaf. Query
parameters that identify roles are preserved. Similar titles, campaign prose
and sibling postings cannot supply the selected role's degree, location or pay.

Duplicate matching identities, foreign-only postings and unscoped multi-posting
pages stay incomplete. The existing single-posting fallback without an explicit
URL still goes through the original identity guards. An India-filtered index
does not establish eligibility for every linked role, and a survey does not
establish assignment or paid work.


## Native boundaries and delivery material

Standalone application-form headings with a reliable submit/end marker or a
closed HTML/ARIA form are excluded from role interpretation. Missing or nested
boundaries remain uncertain; native hydration reports partial evidence. Genuine
requirements and closure notices after a form remain effective. This is bounded
template handling, not a universal plain-text form classifier.

Explicit mandatory onsite role statements override broad remote metadata.
Clause-local remote alternatives and optional attendance remain distinct from
mandatory attendance. Broad data/programming profile domains do not establish
data-science or competitive-programming expert qualifications; explicit matching
domain claims still use the normal assessment path.

Delivery material v3 excludes support-only buyer post lists from the action
fingerprint while retaining semantic revision identity and selected pay. A
buyer acknowledgment cannot reissue an unchanged opportunity. Existing v2
baselines are adopted only after their exact stored material is proven from
an accepted payload for that destination. Missing or mismatched proof requires
review. Adoption creates no send or acceptance receipt; genuine scope/pay changes
remain material. Saved native community-thread v2 evidence is unchanged, and
legacy delivery rollback still requires a pre-handoff database backup.


## Reviewed setup and assigned-work restrictions

Schema-5 setup facts identify an exact pre-allocation checklist and revision,
with an explicit reviewed selection. A dated user completion can supersede an
older reminder while retaining both facts. Its compact status is labeled
user-reported; payable approval and allocation remain unknown. A payment-details
change notice does not establish completion or reissue the completed status.
Ambiguous selection, conflict or undated completion requires verification.
Selected assessments, assigned work and account/terminal states keep priority.
This is explicit reviewed-file input, not automatic mail extraction.

The schema-5 predicates are `setup_selection`, `setup_checklist` and
`setup_details_changed`. Their value identifies `checklist_id` and `revision`;
`setup_checklist` additionally requires `state: pending` or `state: completed`.
Use the normal exact reviewed binding and event provenance. `user_report`
requires actor `user`; `mail` requires actor `provider`. An explicit same-scope
`supersedes` edge reconciles a user completion with the older provider reminder.
The details-change predicate only records the notice. Old schema-1 through
schema-4 events retain their existing serialized shape.

Reviewed allocation terms optionally record `ai_assistance` as `prohibited` or
`allowed`. A prohibition produces manual-work guidance and remains visible in
completed status. Omission means unknown and preserves legacy serialized terms;
no execution permission is inferred. Terms for another allocation do not apply.

Provider-selected plain descriptions now use explicit standalone form boundaries
as well as actual HTML control boundaries. Raw provider comments cannot supply
internal parser closure markers. Undelimited prose and unfamiliar templates
remain bounded extraction limits, with no universal parser guarantee.
