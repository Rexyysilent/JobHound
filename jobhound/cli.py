"""jobhound CLI (HANDOFF v2 §3/§8.5): run · feedback · trust show · show-rejected.

  python run.py run --dry-run                    fetch, rank, write digest; no telegram
  python run.py feedback --platform outlier --event deactivated_no_reason
  python run.py feedback --platform mercor  --event paid_on_time --amount 120
  python run.py feedback --job <id> --event ghosted_after_apply
  python run.py trust show [platform]            scores + evidence audit log
  python run.py trust sweep [--dry-run]          incident news sweep, on demand
  python run.py trust review outlier 2.2 --source trustpilot   class-3 rating evidence
  python run.py join show                        Tier B signup checklist (trust × lang-fit)
  python run.py join set mercor joined           update signup status
  python run.py show-rejected                    the reject pile, with reasons

Bare flags (`python run.py --dry-run`) still work — they route to `run`.
The feedback loop is the point of v2: operator outcomes are class-1 evidence
and move trust scores more than anything the internet says.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .config import CONFIG
from .logging_utils import RedactingFormatter
from .models import VERDICT_SURFACED
from .notify.digest import build_digest, trust_badge, write_digest_file
from .notify.email import EmailNotifier
from .notify.telegram import TelegramNotifier
from .pipeline import ingest_raw, ingest_raw_with_health, run_pipeline
from .settings import settings
from .store import Store
from .trust.registry import default_registry
from .trust.update import apply_feedback, event_deltas

_NOTIFIERS = {"telegram": TelegramNotifier, "email": EmailNotifier}
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _configured_logging_secrets() -> tuple[str, ...]:
    return tuple(filter(None, (
        settings.telegram_bot_token,
        settings.email_app_password,
        settings.imap_app_password,
        settings.adzuna_app_id,
        settings.adzuna_app_key,
        settings.rapidapi_key,
        settings.serper_api_key,
        settings.gemini_api_key,
    )))


def _setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(
        _LOG_FORMAT,
        secrets=_configured_logging_secrets(),
    ))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

    # HTTPX logs the complete URL of every successful request at INFO. Keep
    # JobHound's useful source/pipeline summaries, but only retain transport
    # warnings and errors; the formatter still redacts those defensively.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("jobhound").setLevel(logging.INFO)


# ── run ─────────────────────────────────────────────────────────────────────

def _maintenance(store: Store) -> tuple[list[str], str | None]:
    """P3 dynamics, cadence-gated via the meta table: decay-to-prior (daily
    check, cheap), incident news sweep (weekly), tuning report (weekly).
    Returns (digest alert lines, appendix or None)."""
    from datetime import datetime, timezone

    from .trust.update import apply_decay

    decayed = apply_decay()
    if decayed:
        logging.getLogger("jobhound").info("trust decay applied: %s", ", ".join(decayed))

    alerts: list[str] = []
    if CONFIG.incidents.enabled:
        since = store.days_since_meta("last_incident_sweep")
        if since is None or since >= CONFIG.incidents.every_days:
            from .trust.incidents import sweep
            alerts = asyncio.run(sweep(store))
            store.set_meta("last_incident_sweep", datetime.now(timezone.utc).isoformat())

    appendix: str | None = None
    if CONFIG.report.enabled:
        since = store.days_since_meta("last_tuning_report")
        if since is None or since >= CONFIG.report.every_days:
            from .notify.report import build_weekly_report
            appendix = build_weekly_report(store, days=CONFIG.report.every_days)
            store.set_meta("last_tuning_report", datetime.now(timezone.utc).isoformat())
    return alerts, appendix


def format_run_summary(counts: dict, new_count: int) -> str:
    """One-line ledger for the digest header (v3.5 item 7).

    Every terminal verdict is printed. The 2026-07-22 header only looked
    unreconciled because rejected_location (372) and rejected_listing_quality
    (43) were omitted — 415 jobs that were accounted for all along.
    """
    return (f"raw {counts['raw']} → deduped {counts['deduped']} | "
            f"surfaced {counts['surfaced']} (new {new_count}) | "
            f"rejected: scam {counts['rejected_scam']}, pay {counts['rejected_pay']}, "
            f"trust {counts['rejected_trust']}, ineligible {counts['rejected_ineligible']}, "
            f"location {counts['rejected_location']}, "
            f"listing quality {counts['rejected_listing_quality']}, "
            f"irrelevant {counts['rejected_irrelevant']} | "
            f"unaccounted {counts['unaccounted']}")


def _cmd_run_v3(args: argparse.Namespace) -> None:
    jobs, counts = asyncio.run(run_pipeline(limit=args.limit, only=args.source))

    # Persist everything; a job is "new" if unseen before this run's upsert.
    store = Store()
    new_surfaced = []
    for job in jobs:
        was_seen = store.is_seen(job.id)
        store.upsert(job)
        if job.verdict == VERDICT_SURFACED and not was_seen:
            new_surfaced.append(job)

    alerts, appendix = _maintenance(store)
    store.close()

    summary = format_run_summary(counts, len(new_surfaced))

    digest = build_digest(new_surfaced, summary=summary, top_n=CONFIG.digest.top_n_per_group,
                          alerts=alerts, appendix=appendix)
    path = write_digest_file(digest)

    print("\n" + summary)
    print(f"Digest written: {path}")

    if args.dry_run:
        print("Dry run — no push sent.")
        print("\n" + digest)
        return

    if not new_surfaced and not alerts:
        print("No new surfaced jobs — skipping push.")
        return

    for name in CONFIG.digest.channels:
        cls = _NOTIFIERS.get(name)
        if cls is None:
            print(f"Unknown digest channel {name!r} in config.yaml — skipped.")
            continue
        notifier = cls()
        if not notifier.ready:
            print(f"{name.capitalize()} not configured (.env) — no {name} push sent.")
            continue
        ok = asyncio.run(notifier.send(digest))
        print(f"{name.capitalize()} push: " + ("sent ✅" if ok else "FAILED ❌"))


# ── feedback ────────────────────────────────────────────────────────────────

def _run_v41(args: argparse.Namespace) -> None:
    from .v41.digest import build_digest as build_v41_digest
    from .v41.digest import write_digest as write_v41_digest
    from .v41.engine import evaluate_raw, refine_scam_with_llm
    from .v41.replay import comparison_report, write_snapshot
    from .v41.resolve import resolve_result_urls
    from .v41.store import V41Store

    async def fetch_evaluate():
        raw_records, source_health = await ingest_raw_with_health(
            limit=args.limit, only=args.source
        )
        evaluated = evaluate_raw(raw_records)
        evaluated.source_health = [item.as_dict() for item in source_health]
        if not args.skip_llm:
            await refine_scam_with_llm(evaluated)
        await resolve_result_urls(evaluated)
        return raw_records, evaluated

    raw, result = asyncio.run(fetch_evaluate())
    snapshot_path = None
    if CONFIG.engine.capture_snapshots:
        snapshot_path = write_snapshot(raw, result)
        result.metadata.snapshot_path = str(snapshot_path)

    v41_store = V41Store()
    notifiable = v41_store.annotate_transitions(result)
    v41_store.annotate_source_health(result)
    v41_store.record_run(result)

    # Trust maintenance remains shared with V3 and keeps its existing cadence
    # store. It cannot alter the immutable V4.2 decision record for this run.
    maintenance_store = Store()
    alerts, appendix = _maintenance(maintenance_store)
    maintenance_store.close()

    digest_build = build_v41_digest(
        result,
        items=notifiable,
        include_all=args.full_preview,
        alerts=alerts,
        appendix=appendix,
    )
    date_stamp = result.metadata.as_of.astimezone().strftime("%Y%m%d")
    path = write_v41_digest(digest_build.text, date_stamp=date_stamp)

    print("\n" + digest_build.text)
    print(f"Digest written: {path}")
    if snapshot_path is not None:
        print(f"Replay snapshot: {snapshot_path}")

    if args.shadow_v3 or CONFIG.engine.shadow_v3:
        if snapshot_path is None:
            print("V3 shadow comparison skipped: snapshot capture is disabled.")
        else:
            report = comparison_report(snapshot_path, result)
            report_path = path.with_name(f"shadow_v3_v41_{date_stamp}.md")
            report_path.write_text(report, encoding="utf-8")
            print(f"V3/V4.2 shadow report: {report_path}")

    if not result.accounting_ok or not digest_build.accounting_ok:
        v41_store.close()
        details = ", ".join(
            [*result.accounting_errors, *digest_build.accounting_errors]
        )
        raise SystemExit(f"V4.2 accounting failed; notification blocked: {details}")

    if args.dry_run:
        v41_store.close()
        print("Dry run — no push sent.")
        return

    if (
        not digest_build.displayed
        and not alerts
        and not digest_build.operational_alerts
    ):
        v41_store.close()
        print("No notification-worthy V4.2 jobs — skipping push.")
        return

    for name in CONFIG.digest.channels:
        cls = _NOTIFIERS.get(name)
        if cls is None:
            print(f"Unknown digest channel {name!r} in config.yaml — skipped.")
            continue
        notifier = cls()
        if not notifier.ready:
            print(f"{name.capitalize()} not configured (.env) — no {name} push sent.")
            continue
        ok = asyncio.run(notifier.send(digest_build.text))
        v41_store.mark_notified(
            result.metadata.run_id,
            digest_build.displayed,
            name,
            ok,
        )
        print(f"{name.capitalize()} push: " + ("sent ✅" if ok else "FAILED ❌"))
    v41_store.close()


def cmd_run(args: argparse.Namespace) -> None:
    if CONFIG.v55.enabled:
        raise SystemExit('V5 release policy is review-only until the release gates and rollout are approved. Use review capture or review replay; production configuration was not changed.')
    engine = args.engine or CONFIG.engine.active
    if engine == "v3":
        _cmd_run_v3(args)
        return
    if engine not in {"v4.1", "v4.2"}:
        raise SystemExit(f"unknown engine {engine!r}; choose v3 or v4.2")
    _run_v41(args)


def cmd_replay(args: argparse.Namespace) -> None:
    """Offline V4.2 replay; never touches DB, network or notifications."""
    from .v41.digest import build_digest as build_v41_digest
    from .v41.replay import comparison_report, latest_snapshot, replay

    snapshot = Path(args.snapshot) if args.snapshot else latest_snapshot()
    result = replay(snapshot)
    digest = build_v41_digest(result, include_all=True)
    output = (
        Path(args.output)
        if args.output
        else snapshot.with_name(f"replay_{snapshot.stem}.md")
    )
    output.write_text(digest.text, encoding="utf-8")
    print(f"Replay digest: {output}")
    print(f"Decision ledger: {'PASS' if result.accounting_ok else 'FAIL'}")
    print(f"Presentation ledger: {'PASS' if digest.accounting_ok else 'FAIL'}")
    if args.compare_v3:
        comparison_path = output.with_name(output.stem + "_comparison.md")
        comparison_path.write_text(
            comparison_report(snapshot, result),
            encoding="utf-8",
        )
        print(f"V3/V4.2 comparison: {comparison_path}")


def cmd_review_capture(args: argparse.Namespace) -> None:
    """Live public capture into an isolated namespace; never opens a store."""
    from .v41.review import capture_review
    snapshot, digest = asyncio.run(capture_review(
        args.output_dir, limit=args.limit, source=args.source
    ))
    print(f"Isolated review snapshot: {snapshot}")
    print(f"Isolated review digest: {digest}")


def cmd_review_replay(args: argparse.Namespace) -> None:
    """Strict offline review replay into an explicit namespace."""
    from .v41.review import replay_review
    output, fingerprint = replay_review(args.snapshot, args.output_dir)
    print(f"Offline review digest: {output}")
    print(f"Decision fingerprint: {fingerprint}")


def cmd_feedback(args: argparse.Namespace) -> None:
    try:
        deltas = event_deltas(args.event)  # fail fast on unknown events, before any writes
    except ValueError as e:
        raise SystemExit(str(e))

    store = Store()
    platform_key = args.platform
    if args.job and not platform_key:
        row = store.get_job(args.job)
        if row is None:
            store.close()
            raise SystemExit(f"job {args.job!r} not found in the DB")
        platform_key = row["platform_key"]
        print(f"job {args.job}: {row['title']} @ {row['company'] or '—'}"
              f" → platform {platform_key or 'unrated'}")

    if platform_key is None or not deltas:
        # Nothing to update in the registry — still log it; the event history is
        # threshold-tuning data (e.g. false_positive_scam, ghosted at a no-name).
        store.add_feedback(args.event, platform_key=platform_key, job_id=args.job,
                           amount=args.amount, note=args.note)
        store.close()
        why = "log-only event" if platform_key else "no platform"
        print(f"logged {args.event!r} ({why} → no trust update)")
        return

    prior = store.count_feedback(platform_key, args.event)
    try:
        res = apply_feedback(platform_key, args.event, amount=args.amount,
                             note=args.note, prior_count=prior)
    except ValueError as e:
        store.close()
        raise SystemExit(str(e))
    store.add_feedback(args.event, platform_key=platform_key, job_id=args.job,
                       amount=args.amount, note=args.note)
    store.close()

    print(f"{platform_key}: trust {res.old_trust:.3f} → {res.new_trust:.3f} ({res.delta:+.3f})")
    for dim, (old, new) in res.dim_changes.items():
        print(f"  {dim}: {old:.2f} → {new:.2f}")
    if prior and res.delta > 0:
        print(f"  (positive delta diminished — {prior} prior {args.event!r} event(s))")


# ── trust sweep / review ────────────────────────────────────────────────────

def cmd_trust_sweep(args: argparse.Namespace) -> None:
    """Force an incident news sweep now (cadence-independent)."""
    _setup_logging()
    from .trust.incidents import sweep
    store = Store()
    alerts = asyncio.run(sweep(store, dry_run=args.dry_run))
    store.close()
    if not alerts:
        print("No corroborated trust incidents found.")
        return
    for line in alerts:
        print(line)
    if args.dry_run:
        print("(dry run — nothing written)")


def cmd_trust_review(args: argparse.Namespace) -> None:
    """Log an aggregate review level (class-3 evidence, ±0.03 max)."""
    from .trust.update import apply_review
    try:
        res = apply_review(args.platform, args.rating, scale=args.scale, source=args.source)
    except ValueError as e:
        raise SystemExit(str(e))
    print(f"{args.platform}: trust {res.old_trust:.3f} → {res.new_trust:.3f} "
          f"({res.delta:+.3f}) [{args.source} {args.rating:g}/{args.scale:g}]")


# ── trust show ──────────────────────────────────────────────────────────────

def cmd_trust_show(args: argparse.Namespace) -> None:
    registry = default_registry()
    if args.platform:
        pt = registry.get(args.platform)
        if pt is None:
            raise SystemExit(f"unknown platform {args.platform!r} "
                             f"(known: {', '.join(sorted(registry))})")
        print(f"{pt.display_name}  [{pt.key}]  {trust_badge(pt.trust)}  "
              f"(prior {pt.prior:.2f}, reviewed {pt.last_reviewed})")
        for dim in ("payment_reliability", "work_consistency", "account_stability",
                    "support_quality", "onboarding_cost"):
            print(f"  {dim:<21} {getattr(pt, dim):.2f}")
        print(f"  {'availability_est':<21} {pt.availability_est:.2f}")
        print(f"  {'unpaid_overhead_est':<21} {pt.unpaid_overhead_est:.2f}")
        if pt.caveat:
            print(f"  caveat: {pt.caveat}")
        if pt.notes:
            print(f"  notes: {pt.notes.strip()}")
        print(f"  evidence ({len(pt.evidence)}):")
        for ev in pt.evidence:
            print(f"    {ev.date}  [{ev.evidence_class:>9}]  Δ{ev.delta:+.3f}  {ev.note}")
        return

    print(f"{'platform':<16} {'trust':<12} {'avail':<6} {'ovhd':<6} evidence")
    for pt in sorted(registry.values(), key=lambda p: -p.trust):
        print(f"{pt.key:<16} {trust_badge(pt.trust):<12} {pt.availability_est:<6.2f} "
              f"{pt.unpaid_overhead_est:<6.2f} {len(pt.evidence)}")


# ── join checklist (§10) ────────────────────────────────────────────────────

def cmd_join_show(args: argparse.Namespace) -> None:
    from .join import load_checklist
    items = load_checklist()
    print(f"{'platform':<16} {'status':<12} {'trust':<6} {'fit':<5} {'priority':<9} note")
    for c in items:
        warn = " ⚠" if c.warn and c.status == "todo" else ""
        print(f"{c.key:<16} {c.status:<12} {c.trust:<6.2f} {c.language_fit:<5.1f} "
              f"{c.priority:<9.2f}{warn} {c.note[:60]}")
        if c.status == "todo" and c.signup_url:
            print(f"{'':<16} {c.signup_url}")


def cmd_join_set(args: argparse.Namespace) -> None:
    from .join import set_status
    try:
        set_status(args.platform, args.status)
    except ValueError as e:
        raise SystemExit(str(e))
    print(f"{args.platform}: status → {args.status}")


# ── show-rejected ───────────────────────────────────────────────────────────

def cmd_show_rejected(args: argparse.Namespace) -> None:
    store = Store()
    rows = store.recent_rejected(args.limit)
    if not rows:
        print("Reject pile is empty.")
        store.close()
        return
    print(f"=== Reject pile (most recent {len(rows)}) ===")
    for r in rows:
        flags = r["scam_flags"] or r["fit_reasons"] or ""
        print(f"[{r['verdict']:>22}] scam={r['scam_score']:.2f} fit={r['fit_score']:.0f}  "
              f"{r['title']}  @ {r['company'] or '—'}")
        if flags:
            print(f"    reasons: {flags}")
        print(f"    {r['url']}")
    store.close()


# ── parser ──────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobhound",
                                     description="jobhound — remote job discovery")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="fetch, rank, digest, notify")
    p_run.add_argument("--dry-run", action="store_true", help="write output, don't notify")
    p_run.add_argument("--limit", type=int, default=None, help="cap jobs per source (debug)")
    p_run.add_argument("--source", type=str, default=None, help="run only this source")
    p_run.add_argument("--engine", choices=["v3", "v4.1", "v4.2"], default=None,
                       help="decision engine (default from config.yaml)")
    p_run.add_argument("--shadow-v3", action="store_true",
                       help="compare V3 and V4.2 on the same captured input")
    p_run.add_argument("--full-preview", action="store_true",
                       help="render all surfaced records, not notification transitions")
    p_run.add_argument("--skip-llm", action="store_true",
                       help="skip optional live Gemini scam refinement")
    p_run.set_defaults(func=cmd_run)

    p_replay = sub.add_parser("replay", help="offline V4.2 snapshot replay")
    p_replay.add_argument("snapshot", nargs="?", default=None,
                          help="snapshot JSONL (default: latest)")
    p_replay.add_argument("--compare-v3", action="store_true",
                          help="write a V3/V4.2 comparison report")
    p_replay.add_argument("--output", default=None, help="replay digest output path")
    p_replay.set_defaults(func=cmd_replay)

    p_review = sub.add_parser("review", help="isolated V5.5 capture/replay")
    review_sub = p_review.add_subparsers(dest="review_command", required=True)
    p_capture = review_sub.add_parser("capture", help="isolated public review capture")
    p_capture.add_argument("--output-dir", required=True,
                           help="explicit isolated output/cache namespace")
    p_capture.add_argument("--limit", type=int, default=None)
    p_capture.add_argument("--source", default=None)
    p_capture.set_defaults(func=cmd_review_capture)
    p_review_replay = review_sub.add_parser("replay", help="strict offline review replay")
    p_review_replay.add_argument("snapshot")
    p_review_replay.add_argument("--output-dir", required=True,
                                 help="explicit isolated output namespace")
    p_review_replay.set_defaults(func=cmd_review_replay)

    p_fb = sub.add_parser("feedback", help="record an operator outcome (class-1 evidence)")
    p_fb.add_argument("--platform", type=str, default=None, help="platform_key from the registry")
    p_fb.add_argument("--job", type=str, default=None, help="job id from the DB (resolves platform)")
    p_fb.add_argument("--event", type=str, required=True,
                      help="event from config feedback.events (e.g. paid_on_time)")
    p_fb.add_argument("--amount", type=float, default=None, help="payout amount, for the log")
    p_fb.add_argument("--note", type=str, default=None, help="free-text note for the evidence log")
    p_fb.set_defaults(func=cmd_feedback)

    p_trust = sub.add_parser("trust", help="inspect and maintain the trust registry")
    trust_sub = p_trust.add_subparsers(dest="trust_command", required=True)
    p_show = trust_sub.add_parser("show", help="scores; add a platform for the evidence log")
    p_show.add_argument("platform", nargs="?", default=None)
    p_show.set_defaults(func=cmd_trust_show)
    p_sweep = trust_sub.add_parser("sweep", help="run the incident news sweep now")
    p_sweep.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    p_sweep.set_defaults(func=cmd_trust_sweep)
    p_rev = trust_sub.add_parser("review", help="log a review-site rating (class-3 evidence)")
    p_rev.add_argument("platform")
    p_rev.add_argument("rating", type=float)
    p_rev.add_argument("--scale", type=float, default=5.0)
    p_rev.add_argument("--source", type=str, default="trustpilot")
    p_rev.set_defaults(func=cmd_trust_review)

    p_join = sub.add_parser("join", help="Tier B signup checklist (trust × language-fit)")
    join_sub = p_join.add_subparsers(dest="join_command", required=True)
    p_jshow = join_sub.add_parser("show", help="full checklist, priority-ordered")
    p_jshow.set_defaults(func=cmd_join_show)
    p_jset = join_sub.add_parser("set", help="update signup status")
    p_jset.add_argument("platform")
    p_jset.add_argument("status", choices=["todo", "in_progress", "joined", "skipped"])
    p_jset.set_defaults(func=cmd_join_set)

    p_rej = sub.add_parser("show-rejected", help="print the reject pile with reasons")
    p_rej.add_argument("--limit", type=int, default=100)
    p_rej.set_defaults(func=cmd_show_rejected)

    return parser


_COMMANDS = {"run", "replay", "review", "feedback", "trust", "join", "show-rejected"}


def main(argv: list[str] | None = None) -> None:
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    # Back-compat: bare flags (or nothing) mean `run`. `--show-rejected` predates
    # the subcommand and keeps working.
    if "--show-rejected" in argv:
        argv = ["show-rejected"] + [a for a in argv if a != "--show-rejected"]
    elif not argv or argv[0] not in _COMMANDS and argv[0] not in ("-h", "--help"):
        argv = ["run"] + argv

    args = build_parser().parse_args(argv)
    if args.command is None:
        args = build_parser().parse_args(["run"])
    _setup_logging()
    args.func(args)
