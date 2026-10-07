# Local offline alpha

Use Python 3.12 or newer. This personal prototype supports synthetic demos and
reviewed-file workflows. It makes no platform coverage, income or independent
recommendation-quality claim. Existing scheduled operation remains separately
configured; these commands do not register a task or enable providers.

Create a virtual environment and install the pinned local dependency set:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-local.lock
.\.venv\Scripts\python.exe run.py local doctor
.\.venv\Scripts\python.exe run.py local init --data-dir "$env:LOCALAPPDATA\JobHound\alpha" --profile example
.\.venv\Scripts\python.exe run.py local doctor --data-dir "$env:LOCALAPPDATA\JobHound\alpha"
```

On Linux, the Python installation must include `venv` and `ensurepip` (some
distributions provide these in a separate Python venv package):

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-local.lock
.venv/bin/python run.py local doctor
.venv/bin/python run.py local init --data-dir "$HOME/.local/share/jobhound-alpha" --profile example
.venv/bin/python run.py local doctor --data-dir "$HOME/.local/share/jobhound-alpha"
```

`local --help` and runtime-only `local doctor` load no operator configuration or
dotenv. Local settings contain blank provider credentials, including when the
parent process has provider variables. Initialization copies only the three
tracked neutral root examples. They assert no operator qualifications; edit
your profile using your own supported facts before reviewing real opportunities.
All collection, ingestion, LLM calls and delivery approval stay disabled.

To exercise positive next actions with a **fictional** English evaluator persona,
explicitly copy the separate demo policies into the newly initialized example
profile. Use this profile only for the demo; it is not a biography or evidence of
your qualifications:

```powershell
$demoProfile = "$env:LOCALAPPDATA\JobHound\alpha\profiles\example"
Copy-Item examples/local/config.example.yaml "$demoProfile\config.yaml"
Copy-Item examples/local/profile.example.yaml "$demoProfile\profile.yaml"
.\.venv\Scripts\python.exe run.py local demo --data-dir "$env:LOCALAPPDATA\JobHound\alpha"
```

```sh
demo_profile="$HOME/.local/share/jobhound-alpha/profiles/example"
cp examples/local/config.example.yaml "$demo_profile/config.yaml"
cp examples/local/profile.example.yaml "$demo_profile/profile.yaml"
.venv/bin/python run.py local demo --data-dir "$HOME/.local/share/jobhound-alpha"
```

The demo policy enables the V5.5 interpreter only for offline review; production
approval, discovery, LLM calls, ingestion and delivery remain disabled. Without
these explicit demo claims, the qualification-free starter may reject the
fictional roles, which is expected.

The workspace contains `.jobhound-local.json` and `profiles/<name>/`. Each
profile has its own `config.yaml`, `profile.yaml`, `platform_registry.yaml`,
`state/` and `reviews/`. Add a separate neutral profile with:

```text
python run.py local profile add --data-dir <workspace> --profile second
python run.py local demo --data-dir <workspace> --profile second --name demo-two
```

Names use lower-case letters, digits, underscores and hyphens, beginning with a
letter. Existing directories are never replaced by initialization or restore.
Demo output names must be new. Review the `updated/preview.md` path printed by
the demo; complete evidence stays beside it in audits and `demo-receipt.json`.
The demo exercises native parsing, exact outcome scopes, closure/access changes,
bounded unsent previews and an explicitly fictional mixed-draft recovery. It
never imports an existing user review or contacts a provider.

Use the same selected local profile with the existing reviewed-file workflows:

```text
python run.py local review replay <snapshot> --data-dir <workspace> --profile <name> --name <new-review-name>
python run.py local review outcomes <snapshot> --data-dir <workspace> --profile <name> --name <new-review-name> --events <reviewed-events.jsonl> --bindings <reviewed-bindings.jsonl> --as-of <aware-ISO-time> --handoff-signal
```

Outcome review accepts the existing explicit previous-event, alias and reissue
options. A prior `--delivery-state` must belong to the selected profile's reviews;
another profile's notification state is rejected. Input files are read only when
explicitly named, and every output goes to a new directory under that profile.
These commands do not enable live collection or production outcome import.

Doctor checks runtime dependencies, selected policy and closed database integrity
without opening a store for migration. A database with a WAL, SHM or journal is
reported unverified. A successful runtime-only doctor has not checked a workspace;
`workspace_checked` records this difference. Enabled side effects, missing policy
files, corrupt databases and unknown workspace files fail workspace diagnostics.

Stop local commands and close local database readers before backup. Backup copies
all initialized profiles, policy, state and review history, including empty review
directories. It verifies every byte checksum, SQLite integrity/foreign keys and
the source again before publishing its manifest. Restore validates those proofs
before writing a new destination, then verifies the restored bytes and structure:

```text
python run.py local backup --data-dir <workspace> --output <new-backup-outside-workspace>
python run.py local restore --backup <backup> --data-dir <new-restored-workspace>
python run.py local doctor --data-dir <restored-workspace>
```

This is a closed-state local backup, not an online multi-database transaction or
an authenticated signed archive. Keep the backup private: it includes the selected
profiles' reviewed inputs and outcome/delivery history. No `.env`, links, junctions,
foreign root files or arbitrary plugins are included. Ambiguous/active state is
rejected rather than silently omitted. Bounds are 5,000 files, 5,000 directories,
4 GiB per file and 16 GiB total. A corrupted or incomplete backup does not become
a successful restore. A publication I/O failure can leave a new uninitialized
destination; existing destinations and source state are preserved.

Local review contexts permit SQLite writes only to the exact marked
`delivery.sqlite` within the selected profile and current review run, with no
attached foreign database. Production-store access and provider dispatch remain
forbidden. Historical absolute paths inside immutable audits remain historical
when restored; the backup does not rewrite their provenance or delivery baselines.

Public verification is recorded in [release checks](../RELEASE_CHECKS.md);
remaining capability and quality boundaries are in [the roadmap](ROADMAP.md).
