"""jobhound entrypoint — thin wrapper around jobhound.cli (see its docstring
for commands). Cron/scheduler calls `python run.py` (== `run`).

  python run.py --dry-run                        fetch, rank, write digest; NO telegram
  python run.py feedback --platform outlier --event deactivated_no_reason
  python run.py trust show [platform]
  python run.py show-rejected
"""
from __future__ import annotations

import sys

# Windows console: allow emoji in digest/summary without crashing (cf. telegram-ai-bot).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    # Establish local isolation before importing modules that load operator settings.
    if args[:1]==['local']:
        from jobhound.local_cli import main as local_main
        return local_main(args[1:])
    from jobhound.cli import main as legacy_main
    return legacy_main(args)

if __name__ == "__main__":
    result=main()
    if result is not None:raise SystemExit(result)
