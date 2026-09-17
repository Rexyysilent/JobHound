# Iterations toward a useful maintained project

This is a proposal and acceptance backlog, not a list of completed features.
The product promise is: **show a supported next action and the material evidence
that changed**, rather than maximize the number of collected listings.

## Preserve the useful architecture

Keep one modular local-first application. Its existing observation, provenance,
assessment, decision and replay layers are worth retaining. Separate immutable
run context from source acquisition, pure interpretation, scoped user outcomes,
and delivery. Do not begin with microservices or a hosted multi-tenant wrapper.
The current mutable configuration and legacy identity state need isolation first.

## Near-term acceptance sequence

| Iteration | Deliverable | Acceptance before the next stage |
|---|---|---|
| 1. Evidence integrity | Native pay, page-type gates, partial ATS retention and strict replay | Full existing suite plus failure and positive controls; same-input replay; README unchanged |
| 2. Context and identity | Immutable RunContext; profile/policy ownership; explicit identity aliases | Two interleaved profiles cannot affect each other; distinct requisitions stay distinct; notification history migrates without a blanket reset |
| 3. Outcomes and delivery | Append-only scoped outcome events and durable per-channel outbox | Failed delivery retries; email success does not mark another channel successful; application rejection is scoped to that application; restart tests preserve state |
| 4. Local onboarding | Profile wizard, explicit data directory, doctor, demo and backup/restore | A new tester completes first useful review without editing Python; no credentials needed for the demo; rollback exercised |
| 5. Supervised validation | Frozen candidate cohorts, labels, missing-source accounting and prospective feedback | Report denominators, critical-error cases, useful positives lost, and uncertainty; no promotion based only on a quiet digest |
| 6. Public release | Owner-selected license, versioned release, contribution policy and reproducible issues | Clean install on supported systems; maintenance capacity and known limitations stated |

The currently deployed V4.2 policy can merge same-title/company/location records
more broadly than the review policy. Tightening that identity without a crosswalk
can reset notifications. Treat this as a migration, not a string-hash patch.

## Branchable ideas worth testing

| Branch proposal | Concrete user value | Small experiment and stop condition |
|---|---|---|
| `profile-context` | Multiple people can use the same code without policy leakage | Two deliberately different synthetic profiles, concurrent/interleaved replays, separate state directories. Stop the hosted-service work until isolation passes. |
| `outcome-ledger` | Stops repeat applications, repeated qualifications, and “paid work” inferred from invitations | Structured manual import first: opportunity, project/account scope, event, observation time, evidence reference. No mailbox-wide inference. Reject ambiguous scope. |
| `source-conformance-kit` | Reusable adapter contracts for other collectors | Extract only the transport-independent fixtures and health semantics after one external consumer can use them. Do not fork a new framework without that consumer. |
| `pay-claims` | Native compensation extraction other projects can inspect | Curated currency/unit/qualifier corpus with source spans and an abstention API. Keep buyer budget, seller quote, expenses and accepted payment separate. Publish a library only after licensing and two consumer integrations. |
| `material-change-inbox` | Fewer repeated alerts, with explanations that survive restarts | Compare last-delivered evidence revision, not last-scraped score; per-channel outbox and ambiguous-send state. Stop any “exactly once email” claim without receiver support. |
| `source-yield-pilot` | New sources only when they contribute useful opportunities | One permitted source at a time, fixed request/review budget, overlap measured against existing routes. Disable unchanged low-yield or inaccessible routes. |
| `review-benchmark` | A defensible public evaluation story | Synthetic adversarial cases plus licensed/redacted independent holdout, opportunity-level split isolation, full rejected/hidden sample. No train/test split across copies of the same vacancy. |
| `portable-review-ui` | Non-programmers inspect saved evidence | Improve keyboard navigation, screen-reader labels, import errors and large-file limits; cross-browser tests before claiming accessibility support. Keep the initial UI read-only. |

## Source shortlist: validated contracts before more providers

1. Maintain existing Greenhouse, Lever and Ashby original-board adapters before
   adding another aggregator. Record which boards actually yielded complete
   requirements, not only HTTP success. An empty healthy board differs from a
   failed fetch. Ashby's public API documents `isListed`, native posting fields
   and optional compensation; unlisted jobs are not for public board display.
   See [Ashby's public API](https://developers.ashbyhq.com/docs/public-job-posting-api)
   and [Greenhouse's job-board API](https://docs.greenhouse.io/job-board.html).
2. Treat an index page as discovery input, never as one paid vacancy. Keep the
   individual role's source date and applicant geography separate from a generic
   remote label. See [Google's JobPosting guidance](https://developers.google.com/search/docs/appearance/structured-data/job-posting).
3. For communities and marketplaces, require explicit buyer/employer intent,
   individual scope, current substantive status and a permitted response route.
   A job-seeker discussion, seller catalogue or company home page is not a buyer.
   Login/access denial is a visible limitation, not permission to evade it.
4. Private email is optional evidence, not a required distribution architecture.
   Start with reviewed, scoped imports. Do not promise a sync bridge that has not
   been implemented or account access that has not been observed.

These are architecture/source priorities, not claims that any particular listing
is currently open or that a third-party platform is free to redistribute.

## Measure usefulness without gaming the denominator

Track supported next actions per review session, repeated-blocked-item rate,
critical false recommendations, loss of known positives, time spent verifying,
source coverage, and repeat use. Keep discovery, application, assessment,
selection, allocation, accepted work and received payment as different events.
Do not multiply advertised wages by an arbitrary trust probability.

The proposed 95% Primary precision and 90% actionable retention are release
**targets**, not achieved measurements. Retention is relative to a labeled
candidate pool, not internet-wide recall. Require explicit denominators and
confidence intervals. Zero errors in a tiny synthetic set proves neither.
Report unlabelled and hidden cases; do not evaluate only the displayed positives.

## Adoption plan

Start with 3-5 onboarding testers, then 5-10 supervised design partners across
more than one geography/language profile. These are suggested cohort sizes, not
predictions. Observe where installation, evidence interpretation and outcome
recording fail. Publish a small number of honest fixes and reproducible case
studies, rather than automated promotional posts.

A good launch story is concrete: an index misclassified as a paid role, a monthly
salary misread as annual, or a healthy ATS board erased by another board's error.
Show the retained positive control, code and test. Ask for reproduction feedback,
not manufactured stars. Maintain a small issue backlog with evidence, explicit
scope and supported environments; review external contributions before expanding
supported providers. Do not adopt dependency code until its license is compatible.

## Maintainer-program readiness

OpenAI's current [Codex for Open Source](https://openai.com/form/codex-for-oss/)
page describes active maintenance, meaningful usage/adoption or ecosystem
importance. It is not a guaranteed cash grant or a published universal star
threshold. Selected maintainers receive six months of ChatGPT Pro including
Codex, with API credits and conditional Codex Security consideration. Recheck
that page at application time.

The credible application evidence is working releases, actual users, reviewed
issues/PRs, reproducible quality checks and a specific maintenance use for the
tools. Choose an OSS license before claiming the project is distributable OSS.
No application was submitted and no license was selected by this change.
