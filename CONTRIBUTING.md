# Contributing to JobHound

Start with a small, reproducible change that improves a supported next action.
A larger scraped feed, a higher score, or a longer digest is not evidence of a
better recommendation.

## Development

Use Python 3.12 or newer in a virtual environment. Install `requirements.txt`,
then run `python -m pytest -q`. Tests use synthetic configuration and block
external network connections. CI runs the suite on Linux and Windows.

Do not use `run --dry-run` as an isolation mechanism: it suppresses delivery,
not all database, snapshot or maintenance writes. Prefer the offline replay
commands in [the guide](docs/GETTING_STARTED.md). Keep private profiles,
credentials, mailbox content, real outcome ledgers and captured job descriptions
out of commits.

## A useful pull request

Include the observed failure, a minimal public or synthetic fixture, the expected
behavior, a positive-retention control, and the test command/result. Write the
test first and show that it fails without your change. Distinguish unit tests,
synthetic replay, supervised real-data review, and live-provider coverage.
Explain any deliberately changed contract or migration requirement.

For source adapters, use `httpx.MockTransport` fixtures. Cover healthy empty
responses, malformed schemas, partial failure, rate limits, access denial, and
budget exhaustion. Approved V5.5 runs fetch through one bounded transport per
run (see the README); adapters must accept the transport they are given rather
than creating their own client. A 403 is not permission to evade access
controls. Respect the provider's access, retention and redistribution rules.

For interpretation changes, preserve source text and native units. Missing
requirements are unknown, not satisfied. A marketplace budget is not earnings;
a pool registration is not task allocation; application and payout states are
separate, scoped events. No automated applications, assessments or bids belong
in a parser patch.

For delivery changes, test on disposable SQLite stores only. Never assume a
provider's acceptance means inbox arrival, and never resend an uncertain send
automatically.

Before publishing, stage an explicit file list and run:

```sh
python tools/check_publication.py --index
```

Inspect the diff yourself as well. The scanner is a heuristic, not a guarantee.
Do not add broad exceptions for private data.

## Licensing and ownership

No open-source license has been selected yet, so no reuse rights are granted.
Resolve licensing before soliciting general external contributions or
distributing packages.

## Where help is valuable

See [the roadmap](docs/ROADMAP.md). Good first contributions include small
parser boundary fixtures, adapter contract tests, accessible workbench controls,
and documentation that can be followed in a clean environment. Avoid broad
rewrites and unverified provider lists.
