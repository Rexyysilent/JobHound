# JobHound

A personal project for finding remote work without spending the whole day
sorting through job boards. JobHound collects listings, filters and ranks them
against a profile, and puts the results into a digest with reasons you can inspect.

It's a working prototype, not a finished job-search service. The idea is to keep
trying it on real listings, learn where it gets things wrong, and improve it over
time. Expect rough edges and changing rules.

## What it does

- Collects jobs from multiple sources and groups duplicate listings.
- Checks relevance, eligibility, location and stated pay, keeping the source evidence.
- Produces an explainable digest, with optional email or Telegram delivery.
- Uses offline regression cases to check that fixes don't lose useful leads.

The default engine is V4.2; the newer V5.5 evidence-based policy is **review-only**.
Scores are triage aids, not endorsements. Advertised pay isn't verified earnings,
and a remote label doesn't guarantee you're eligible.

## Try it locally

Python 3.11+; these commands use PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item config.example.yaml config.yaml
Copy-Item profile.example.yaml profile.yaml
Copy-Item platform_registry.example.yaml platform_registry.yaml
Copy-Item platforms_to_join.example.yaml platforms_to_join.yaml
.venv\Scripts\python.exe run.py --help
.venv\Scripts\python.exe -m pytest -q
```

Tests run without credentials and use synthetic profiles and listings. For live
discovery, configure your own profile and provider credentials, then enable the
sources you want. Sources, LLM calls and delivery are off in the starter config.
Some adapters still assume India/INR; check those settings for your region.
Follow provider access rules and inspect a local digest before scheduling delivery.

## A few notes

This repo contains the code and demo/test inputs, not my private configuration,
job history or credentials. Local settings and generated data stay ignored by Git.
See [release checks](RELEASE_CHECKS.md) for what's been tested and
[publication notes](PUBLICATION.md) for the sharing boundary.

No open-source license has been selected yet.
