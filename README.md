# JobHound

A personal project for finding remote work without spending the whole day
sorting through job boards. JobHound collects listings, checks them against a
profile, and sends a short daily digest that answers one question:

> **What is worth doing next, why, and what has changed since I last checked?**

It's a working prototype that I run every day, not a finished job-search
service. Expect rough edges and rules that keep changing as I learn where it
gets things wrong.

## What a digest card looks like

This example is made up, but it's the real card layout:

```text
APPLY | AI Training Contributor - Bengali (India) - Remote | Example Co
  Fit: language_ai (title:bengali); ai_evaluation (title:ai training)
  Pay: unknown
  Requirements: bengali + english · experience not stated · completeness complete
  Readiness: application ready; India (Remote) · hours unknown
  Next: Submit one application through the verified individual role page;
        selection and task allocation remain pending.
  Evidence: original_ats via jobs.example.org; applicant eligibility passed
  Checked: matching role open at 2026-09-27T14:30Z; selection is not guaranteed.
```

Cards say what was actually verified and what is still unknown. Unknown pay
stays "unknown"; an advertised rate is never shown as earnings.

The ordering line now labels task fit and role priority separately. They are
different ranking fields, not a fraction or a score out of a maximum.

## How it works

```text
 collect ──► normalize ──► classify page ──► re-check the original ──► judge ──► next action ──► deliver
 (sources)   (clean text,  (vacancy? index?   (hydration: fetch the     (eligibility,  (apply / verify    (digest file +
              keep spans)   seller? thread?)   real job page, re-judge)  pay, freshness) / watch / reject)  email outbox)
```

Every fact keeps a pointer to the observation it came from, so a claim can
always be traced back to the source text. Decisions are reproducible: replaying
a saved snapshot gives the same decisions and the same fingerprint.

### Two engines

| | V4.2 (starter default) | V5.5 (what I run daily) |
|---|---|---|
| Judging | Keyword relevance, eligibility gates, pay floor | Evidence-based: who must satisfy a requirement, AND/OR language rules, specialist vs generalist work, pay units, freshness, page type |
| Fetching | Legacy clients, link resolution | v6 bounded transport + hydration (below) |
| Delivery | Simple send loop | Durable outbox (below) |
| Turned on by | Default | Four explicit keys in `config.yaml` |

V5.5 only runs when both `v55` keys are `true`. The durable outbox also needs
both `delivery` keys. That makes an accidental switch hard.

### v6 fetching: keep what you fetched, spend a fixed budget

Old collectors could throw away good results when one request failed. v6
changes how a run talks to the internet:

- **One budget per run.** Every request in an approved V5.5 run (discovery,
  pagination, redirects and page re-checks) goes through a single bounded
  transport. It has a request cap, time limits and per-response size limits
  (`v55.production_fetch`). The example policy sets a 200-request ceiling;
  that is a budget, not a promise of complete coverage.
- **Partial results survive.** If one Greenhouse/Lever/Ashby board times out,
  the healthy boards' jobs are kept. If a JSearch or Serper query fails
  halfway, earlier queries' results are kept. Records without IDs and changed
  pay quotes are kept as separate observations instead of being collapsed.
- **Honest source health.** Discovery, resolution and hydration are tracked
  separately: an empty feed, a malformed response, a rate limit and a deferred
  request all look different. A 429 means "coverage degraded", never "no jobs".
- **Hydration, the re-check.** For the most promising candidates, the run
  fetches the matching original job page (ATS posting or first-party page),
  adds it as a new child observation, and re-judges the whole run. A redirect
  or a 200 response alone never counts as "complete".
- **Verification memory.** If a candidate's automatic check fails twice, it
  moves to Watch for 7 days instead of reappearing every day. History lives in
  `runtime/v6/` (ignored by Git).
- **Polite by construction.** Credentialed requests don't follow redirects,
  `Retry-After` and cooldowns are respected, and there is no CAPTCHA, proxy or
  login bypass. Upwork is never scraped directly.

Native Make/n8n thread readers and exact public Turing/micro1 role readers are
included. Threads separate the buyer from sellers, quotes and unrelated
replies; exact-role readers distinguish a real role from a JavaScript shell.
These are bounded evidence readers, not a claim of complete platform coverage.
Native role readers, V5.5 ATS HTML feeds and generic JobPosting hydration keep
application dropdowns, form controls and navigation out of role evidence.
Form text cannot close a generic role; genuine requirements and closure notices
remain enforced. Captured provider fields remain separate from cleaned text.
The optional [source policy](docs/source_policy.md) records admission, cadence,
inspection cost and adequately covered checks before expanding a source.

### Quality gates: a useful title is not enough

- Resource pages and job roundups fail the specific-vacancy gate, even when
  their text mentions hiring or pay. The same floor applies to full cards and
  the compact **Also eligible** list.
- Unresolved hosted-board copies under `is-great.org` and `my-board.org` cannot
  headline as original jobs. An identity-resolved employer or ATS URL remains
  usable even when a copy appeared elsewhere.
- Buyer requests with native thread evidence become historical leads after
  60 days without substantive activity by that buyer. Seller replies, cosmetic
  edits and simple acknowledgments do not refresh demand. Historical leads
  remain in Watch/audit records and leave the daily card and compact pools.
- Company revenue, funding, customer contract values and allowances do not
  become worker wages. Cash benefits retain their conditions separately from
  base pay; unsupported units stay unknown.

### v5.7 outcomes: remember what actually happened

A job listing is only the first step. v5.7 adds scoped outcome facts:
applications, assessments, rejections, buyer replies and access changes.

- **Scoped, not global.** A rejection closes that application, not the whole
  company. An access block applies to that project, not every project on the
  platform.
- **Newer facts win per predicate.** A new fact replaces an older one only for
  the same attribute and scope. History is kept, and reopening a closed item
  needs an explicit link.
- **Actions, not scores.** Outcomes feed the same action policy: "clarify
  changed scope", "complete the existing assessment", "don't reapply". There is
  no separate outcome score.

Right now outcome import is an **offline preview** (`review outcomes`) using
reviewed files. It never reads a mailbox or writes to production stores.

Review now keeps historical application attempts, assessment revisions and
work allocations separately. One active attempt supplies the next action;
approvals for another assessment revision or allocation do not carry over.
Agreed terms, delivered work, acceptance, invoice, settlement and received
payment remain different facts. Public suitability blockers still veto an
application or work action.

Optional [handoff signals](docs/handoff_signals.md) prepare a compact preview
from a marked disposable outbox. They share the existing ranking and delivery
machinery, preserve destination-specific receipts, and require explicit review
to replace a mixed unsent draft. A preview is not a provider receipt.

### Durable delivery: no silent duplicates, no silent losses

- Each decision revision and its delivery intent are committed together before
  anything is sent.
- Each destination (for example, one email address) keeps its own "last
  delivered" record, so a failing channel can't block or mark another.
- A provider-confirmed "not sent" is retried on a later run. An **uncertain**
  send (the connection dropped mid-send) is never auto-resent; it waits for
  reconciliation.
- You only hear about real changes: new, promoted, pay resolved, conflict
  appeared or cleared. A job seen again with nothing changed stays quiet.
- Rolling back to V4.2 is safe: it sees what V5.5 already delivered.

Public-page connections pin a validated public IP while retaining the original
hostname for HTTP and TLS verification. Unsupported proxies fail closed, SMTP
and IMAP verify certificates, and unrelated JobPosting blocks cannot make a
parent listing appear verified. These fixes have offline regression tests;
they are not a certification of every security property.

## Try it

Python 3.12+. The quickest look needs no install: open `workbench/index.html`
and click **Load synthetic demo**. To run the code:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pytest -q
```

Tests run without credentials, using synthetic profiles and listings. A pinned
Python 3.12 dependency set is also supplied in `requirements-local.lock`. For live
use, follow [Getting started](docs/GETTING_STARTED.md): copy the example
configs, pick sources, try `review capture`, then schedule daily runs. Sources,
LLM calls and delivery are all off in the starter config.

### Local offline workflow

`python run.py local --help` provides isolated data directories, named profiles,
diagnostics, reviewed-file replay/outcomes, a fictional workflow demo and
closed-state backup/restore. Local commands do not read the repository's
operator settings or provider credentials and do not enable collection or
delivery.

Initialization uses the qualification-free public starter. For a positive
fictional demo, explicitly select the separate policies in `examples/local/`;
they are test personas, not declarations about you. Follow
[the local setup guide](docs/local_alpha.md) for the exact commands.

## Good to know

- **Scores are triage aids, not endorsements.** Advertised pay isn't verified
  earnings, a remote label doesn't guarantee you're eligible, and "Apply" is
  never a promise of selection.
- **`run --dry-run` is not a sandbox.** It skips sending but can still write
  state. Use `review capture` / `review replay` to experiment.
- **Region defaults.** Some adapters still assume India/INR; set yours
  explicitly.
- **Respect providers.** Follow each source's access and usage rules and use
  your own API keys.

## Project status

This repo contains the code, synthetic tests and demo inputs, not my private
configuration, job history, outcomes or credentials. See
[the current change summary](docs/CHANGES.md),
[release checks](RELEASE_CHECKS.md) for what has been verified and
[publication notes](PUBLICATION.md) for the
sharing boundary. Plans and known gaps are in the [roadmap](docs/ROADMAP.md);
contribution guidelines are in [CONTRIBUTING](CONTRIBUTING.md).

No open-source license has been selected yet.
