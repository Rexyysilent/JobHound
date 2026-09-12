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

from jobhound.cli import main

if __name__ == "__main__":
    main()
