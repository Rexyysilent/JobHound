# Source budgets and reviewed cadence evidence

The optional `SourcePlan` is an explicit overlay for controlled expansion work.
It does not replace configured discovery, load the watch register into a loop,
or establish parser admission from a provider name. Each route records its
source/family/category, bounded task reference, public hosts, parser state and
reviewed evidence references. A core/pilot route requires a matching logged
policy decision. A reduced-cadence decision requires a later `next_check_at`;
watch, paused or not-yet-due routes cannot start scoped work. At most two pilot
families may be present. The initial n8n-only pilot choice remains a planning
decision; this extension activates no additional family.

`RunBudget(context, limits, source_plan=plan)` owns the plan and the existing
shared request counter. The plan cannot add requests to that owner's ceiling,
and cannot exceed 60 requests / 12 logical searches / 12 distinct inspections.
Per-source and category caps share the same SQLite transaction as the HTTP
reservation, so redirects, retries and paginated requests cannot double-spend
discovery/hydration budgets. Exploration is bounded by floor(20% of the planned
search/inspection ceiling), normally two; unused capacity is not a quota. The
soft-reject hydration reserve remains separately configured.

Controlled adapters use the owner's scope around one distinct document:

```python
budget.sources.search('n8n-jobs', 'query-1')
with budget.sources.inspection('n8n-jobs', 'topic-123'):
    response = await client.get(public_url)
```

The client must borrow `BoundedTransport(..., budget)` from that same owner.
Public host/scheme and credential checks apply before dispatch. A scope from
another run/workspace cannot lend authority. Distinct document identities and
declared logical search identities are counted separately from HTTP attempts.
Retries/pages of the same document retain one inspection and count each HTTP
reservation. A cache reader calls `cached_inspection()` after reading the saved
document: one inspection, zero HTTP requests. Only unused unsent inspection
reservations are released. Interrupted/possibly sent requests and 429 responses
retain their cost; provider/account cooldowns persist across restart.

Receipts separate charged inspection reservations, attempts/cache inspections
and unknown unsent reservations. Actual exploration shares may differ from the
planned fraction; no filler core searches are required. Existing unclassified
adapter calls remain collected under the shared request ceiling and are counted
as `unattributed_existing_requests`. Their per-source logical effort is not
claimed as measured. New dedicated adapters must use explicit scopes before
collection is enabled. `capture_review(..., source_plan=plan)` can install the
owner overlay; a default capture retains its existing behavior.

The separate offline check contract records source/query/profile cohort,
intended window, allowed/fetched page set, omissions, retrieval state, novelty
review, original/action identities, duplicates, suitability occurrences,
critical exclusions, measured review effort/cost in native currency and later
transition references. Unknown measurements stay null. An adequately covered
check requires the entire planned set, no omissions, successful retrieval and
explicit novelty evaluation. Throttling, unsupported content and partial/failed
retrieval are inadequate coverage, never zero demand. Five adequately covered
zero-yield checks in nonoverlapping windows flag cadence review for a core route
or pilot. Repeated coverage of the same window cannot multiply that evidence.
A known new worthwhile action resets the streak even when other pages remain
unavailable; that partial check still cannot claim adequate coverage. No automatic blacklist or policy rewrite
occurs; subsequent changes need a logged decision and a new reviewed plan.

Run the offline evidence report with:

```powershell
python -B tools/source_policy_review.py --plan PLAN.json --checks CHECKS.jsonl `
  --as-of 2026-10-04T12:00:00+00:00 --output-dir NEW_REVIEW_DIRECTORY
```

Use `--decisions DECISIONS.jsonl` for additional reviewed policy history.
Reports keep profile/query cohorts separate, deduplicate check identities,
reject collisions/future evidence, retain unmeasured denominators and preserve
native external-cost currencies. Action unions describe incidence, not finder
attribution, conversion rates or payment. Input evidence hashes are supplied
reviewed references, not authenticated remote-source verification. No network,
production store, scheduler or policy mutation occurs in this CLI.

Turing contractor and micro1 now have separate [native role contracts](exact_roles.md)
and synthetic parser fixtures. Daily collection still needs logged admission and
adequately covered route-scoped checks before claiming expanded source coverage.
The budget/evidence controls and exact-page readers do not establish source
effectiveness or activate new collectors.
