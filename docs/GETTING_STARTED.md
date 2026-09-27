# Try, inspect and run JobHound

Commands below use PowerShell on Windows; on macOS/Linux use `python` from your
virtual environment and `/` in paths.

## 1. No-account preview (nothing to install)

Open `workbench/index.html` in a browser and select **Load synthetic demo**.
The demo records are fictional. The workbench reads a local audit file, keeps it
in memory and shows saved decisions with their evidence. It does not rerank
jobs, connect accounts, run collectors, send notifications or submit
applications. Untrusted text is escaped and non-HTTP(S) links are not clickable.

It accepts files up to 32 MiB and pages through large cohorts 200 cards at a
time. A full `audit.json` from a real run can be over 100 MB, so load a compact
audit (`jobhound.compact_audit.write_compact_audit`) rather than the full one. A
CLI flag for compact export is on the roadmap.

## 2. Install and run the tests

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pytest -q
```

Tests use synthetic settings from `tests/fixtures`, blank every credential and
block external network connections. You do not need any API key to run them.

## 3. Configure your own copy

```powershell
Copy-Item .env.example .env
Copy-Item config.example.yaml config.yaml
Copy-Item profile.example.yaml profile.yaml
Copy-Item platform_registry.example.yaml platform_registry.yaml
Copy-Item platforms_to_join.example.yaml platforms_to_join.yaml
```

All five copies are ignored by Git. In `config.yaml`, enable only the sources
you want. Keyless sources (Remotive, RemoteOK, Arbeitnow, and the Greenhouse,
Lever and Ashby job boards) work without credentials; Adzuna, JSearch
(RapidAPI) and Serper need your own keys in `.env`. Edit `profile.yaml` to
describe your languages, location and target work honestly: it drives
eligibility, and nothing in it is verified for you.

## 4. Look before you schedule: review mode

Review commands write only to the directory you name. They never open the
production databases or send anything.

```powershell
# Live, read-only capture of public sources into an isolated folder
.venv\Scripts\python.exe run.py review capture --output-dir review\first-look --limit 40

# Re-judge a saved snapshot offline (no network, no model calls)
.venv\Scripts\python.exe run.py review replay review\first-look\<snapshot>.jsonl --output-dir review\replay-1
```

Each run writes a digest, a full audit and a snapshot. A replay of the same
snapshot is deterministic: the printed decision fingerprint should not change.

`review outcomes` previews reviewed application/buyer events against a snapshot
(offline; it never imports into a production store). Run it with `--help` for
the required files; the synthetic cases in `tests/test_v57_outcomes.py` show
the event and binding format.

## 5. Daily runs

```powershell
.venv\Scripts\python.exe run.py run --skip-llm
```

With the starter config this uses the legacy **V4.2** engine. To use **V5.5**
with v6 fetching and durable delivery, set all four keys in `config.yaml`:

```yaml
v55:
  enabled: true
  production_approved: true
delivery:
  enabled: true
  production_approved: true
```

and list your channels (for example `digest.channels: [email]`) with matching
credentials in `.env`. Approved V5.5 runs keep their request ledger and
verification history in `runtime/v6/` (ignored by Git). To go back, set the
keys to `false`: V4.2 sees what V5.5 already delivered and will not resend it.
Back up `data/` before switching engines.

`jobhound_daily.cmd` is a Windows Task Scheduler wrapper that runs the daily
job from the repository folder and appends output to `data\cron.log`.

**Important:** `run --dry-run` is not a no-write sandbox. It skips delivery but
can still write stores, snapshots and maintenance state. Use review mode for
experiments.

## What is not shipped yet

There is no profile wizard, `doctor` command, hosted service or automatic
mailbox/outcome sync. Some adapter defaults still assume India/INR. Community
buyer-thread adapters (Make, n8n) and exact-role adapters (Turing, micro1) are
planned but not built. See [the roadmap](ROADMAP.md).
