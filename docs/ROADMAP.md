# Roadmap

This is an honest status list, not a feature promise. The product goal is:
**show a supported next action and the evidence that changed**, rather than
maximize the number of collected listings.

## Where things stand

| Iteration | Status |
|---|---|
| 1. Evidence integrity: native pay units, page-type gates, partial ATS retention, strict replay | Done |
| 2. Run context and identity: immutable per-run context, profile/policy isolation, identity crosswalks | Done for runs; profile onboarding still manual |
| 3. Outcomes and delivery: scoped outcome events, durable per-destination outbox | Outbox done and used in approved runs; outcome events are offline preview only |
| 4. Local onboarding: profile wizard, data directory, `doctor`, backup/restore | Not started |
| 5. Supervised validation: frozen cohorts, independent labels, precision/retention with denominators | Not started |
| 6. Public release: owner-selected license, versioned release | Not started (no license yet) |

## Known issues

- Some listing pages and freelancer profiles still reach Verify instead of
  being rejected.
- "X is necessary" is not yet read as a requirement; only required / must /
  mandatory / minimum are.
- Verify cards still print a developer "Ordering:" line.

## Next

1. Community buyer-thread adapters for public Discourse hiring categories (Make
   community Hire Help, n8n community Jobs): the original poster is the buyer,
   replies are mostly sellers, closure markers end the lead, reads are paced
   and a 429 is recorded as missing coverage.
2. Exact-role adapters for contractor platforms with explicit identity and
   completeness checks; a JavaScript shell never counts as complete evidence.
3. A compact-audit CLI export for the workbench.
4. Onboarding: profile wizard, `doctor`, backup/restore.

## Branchable ideas

| Idea | Value | Small experiment and stop condition |
|---|---|---|
| `source-conformance-kit` | Reusable adapter contracts | Extract transport-independent fixtures and health semantics once a second consumer exists |
| `pay-claims` | Inspectable compensation extraction | Curated currency/unit/qualifier corpus with spans and an abstention API; buyer budget, seller quote and accepted payment stay separate |
| `source-yield-pilot` | New sources only when they add useful opportunities | One permitted source at a time, fixed request budget, overlap measured against existing routes |
| `review-benchmark` | A defensible evaluation | Synthetic adversarial cases plus an independently labelled holdout, split by opportunity so copies never leak across sets |
| `portable-review-ui` | Non-programmers inspect saved evidence | Keyboard navigation, screen-reader labels, import errors and large-file limits, tested across browsers; read-only |

## Measuring usefulness

Track supported next actions per review session, repeated-blocked-item rate,
critical false recommendations, loss of known positives, verification time and
source coverage. Keep discovery, application, assessment, selection, allocation,
accepted work and received payment as different events. The 95% Primary
precision and 90% actionable retention figures are targets, not measurements:
report denominators and uncertainty, and never evaluate only displayed items.
