# Job-search accuracy and native evidence

This update improves how JobHound turns a discovered link into a supported next
action. It covers three stages: identifying a real opportunity, reading the exact
original posting, and explaining what still prevents an application.

## What changes

| Failure shape | Current behavior |
| --- | --- |
| A category page advertises many jobs and a pay range | Counted job directories fail the vacancy gate; their advertised range cannot make the directory a recommended job |
| A foreign onsite role also matches automation or AI keywords | Explicit attendance and applicant geography take precedence, including German `vor Ort` and `Deutschland`; negated, optional and remote alternatives remain distinct |
| An AI task title names a physician or attorney occupation | The role needs the matching target capability; general annotation and administrative support roles do not become practitioner occupations |
| A management title abbreviates manager as `Mgr` | The existing seniority gate handles that abbreviation |
| A personal social post mentions AI work | It remains discussion or seller material; explicit buyer demand and structured recruitment posts are retained for verification |
| A social recruiter advertises a talent pool | It retains pool/watch status; recruitment language does not establish current allocation |
| A Greenhouse posting uses an EU hosted URL | An exact posting ID routes to the documented public Job Board API, with employer, title and identity checks |
| Native evidence is captured after a run starts | Accepted evidence is evaluated at retrieval completion, with actual capture times preserved in snapshots and replay |
| A request is deferred before inspecting a vacancy | Budget, cadence and cooldown deferrals do not consume a failed verification cycle |
| A publisher extends a deadline | A later complete observation of the same original can supersede its earlier expired deadline; copied pages cannot extend the original and explicit closure notices remain authoritative |
| Every candidate requires verification | The saved-run quality report identifies the missing facts and collection gaps across the full cohort |

These gates retain separate meanings for discovery, eligibility, an open posting,
an available application route, a talent pool, and an allocated task.

## Native provider semantics

### Ashby

Ashby's `isRemote` can describe a remote or hybrid posting. Its explicit
`workplaceType` determines that distinction when supplied. A broad remote flag
without a clear arrangement or role-specific remote statement stays uncertain;
the verifier does not promote it to fully remote work. The raw provider fields
remain available for inspection.

`isListed=false` describes a published posting that is reachable by its direct
link but omitted from the organization's public list. It does not establish
closure. Captured deadlines and scoped closure evidence are assessed separately.
See the [Ashby Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api).

### Lever

Collection and hydration use the same description assembly. It retains the main
description, named requirement lists, additional text, and salary descriptions,
while preserving the existing application-form boundaries. This prevents a
requirement at the end of a posting from disappearing during normalization.

Primary and alternative posting locations are retained. Salary ranges use the
explicit provider currency; only recognized intervals become pay units, and the
originals remain in the source payload. Missing values do not acquire a guessed
currency or annual unit. Invalid, reversed and non-finite ranges provide no
numeric pay claim. See the [Lever Postings API](https://github.com/lever/postings-api).

### Greenhouse and generic JobPosting

Exact Greenhouse leaves use the public posting endpoint, including supported EU
hosted URLs. A board slug inferred during search discovery can be replaced by
the same exact native posting's employer name; an independently contradictory
employer or different posting ID still fails identity checks.

Greenhouse `first_published` supplies the publication time when available.
`updated_at` is retained in the source payload, but is not substituted for a new
publication date. See the [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html).

For generic JobPosting data, a list-valued `jobLocationType` is supported. Remote
work and applicant residence remain separate: an explicit eligible country can
establish geography; the word `Remote` alone cannot. Date-only deadlines remain
valid through the stated UTC day. Naive timestamps use UTC; explicit time zones
are respected. Missing or malformed deadlines do not establish closure. See
[Google's JobPosting documentation](https://developers.google.com/search/docs/appearance/structured-data/job-posting).

## Retrieval time and verification budgets

A run can start before a successful posting read finishes. The final evaluation
clock advances to independently measured retrieval completion when an accepted
hydration observation exists. This admits a real same-run opening check and also
enforces a deadline crossed while the remaining reads were finishing. A future
cache entry cannot advance the clock or become fresh evidence.

Snapshots save the original discovery capture and opening-check times. Offline
replay restores those times rather than relabeling earlier observations with the
later evaluation clock.

Within the existing bounded shortlist, exact supported native leaves have
priority over unresolved copies at the same action-readiness level. Source
diversity remains; source-name sorting cannot decide the final slot. A request
that never reaches a vacancy because of a local deferral does not count as a
failed inspection. Actual HTTP failures retain their failure handling. These
changes do not raise source budgets or bypass provider cooldowns.

## Diagnose the next run

Use the isolated review workflow with the intended profile and source settings:

```sh
python run.py review capture --output-dir review/accuracy-check --limit 40
python -m jobhound.review_audit quality review/accuracy-check/audit.compact.json
```

Review capture writes a source snapshot, digest, full audit, compact audit and
quality report. A compact export over the size limit is explicitly omitted;
the full audit, snapshot and bounded quality report are retained. Load the
compact audit into `workbench/index.html` for individual evidence review.

The quality report separates application-ready actions, other supported next
steps, active verification, watch/status records and rejections. It includes
hidden candidates, retained-source yield, missing facts, collection-health
coverage and saved code/configuration fingerprints. See
[run-quality diagnostics](run_quality.md) for exact counting rules.

New durable digest summaries call these records **matched candidates**. A match
does not assert that applicant eligibility passed. Their audit footer contains
the saved code and configuration SHA-256 prefixes, allowing an operator to
compare a digest with its actual run. A code content hash is not a Git commit ID.
Older staged reports without those fields do not acquire an invented fingerprint;
already frozen delivery bodies retain their original bytes.

## Verification and operational boundary

The regression suite covers both the problematic inputs and nearby valid
opportunities: real single-role titles with numbers, explicit recruiting posts,
support occupations, supported country alternatives, direct-link-only postings,
future captures and conflicting identity. Acceptance contracts and the local
demo exercise the full decision workflow. Exact results are recorded in
[release checks](../RELEASE_CHECKS.md).

These checks establish the tested behavior. They do not measure market-wide
precision or recall, prove current provider coverage, or establish a hiring or
payment outcome. The [independent quality gate](ROADMAP.md) remains open until
there are independently reviewed positives and negatives.

An existing scheduled installation must run the updated code to receive these
changes. Review mode provides a concrete comparison before updating its daily
copy; its private configuration, notification history and scheduler remain
operational inputs owned by that installation.
