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
