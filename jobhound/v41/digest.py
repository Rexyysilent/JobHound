"""V4.1 action-band digest with exhaustive presentation accounting."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path

from ..config import CONFIG
from ..notify.digest import trust_badge
from .models import ActionBand, EvaluatedJob, RunResult
from .provenance import public_url

_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class DigestBuild:
    text: str
    displayed: list[EvaluatedJob]
    display_counts: dict[str, int]
    accounting_ok: bool
    accounting_errors: list[str] = field(default_factory=list)
    operational_alerts: list[str] = field(default_factory=list)


def _source_line(item: EvaluatedJob) -> str:
    observation = item.canonical.best_observation
    host = observation.apply_domain or observation.publisher_domain or "unknown host"
    count = len(item.canonical.observations)
    corroboration = f", {count} observations" if count > 1 else ""
    if observation.resolution_attempted:
        resolution = (
            f"; resolution {observation.resolution_error}"
            if observation.resolution_error else "; URL resolved"
        )
    else:
        resolution = "; direct/unchanged URL" if observation.source_kind.value.startswith("original_") else "; URL not resolved"
    return f"{observation.source_kind.value} via {host}{corroboration}{resolution}"


def _access_line(item: EvaluatedJob) -> str:
    job = item.job
    tags = {tag.casefold() for tag in job.region_tags}
    location = job.location or (
        "Worldwide" if "worldwide" in tags else
        "India/APAC" if tags.intersection({"india", "apac", "asia"}) else
        "scope unknown"
    )
    remote = "Remote" if job.is_remote or item.assessment.work_format == "remote" else item.assessment.work_format.replace("_", " ")
    dimension = item.assessment.evidence_dimensions.get('work_arrangement')
    if dimension:
        remote = {'remote': 'Remote', 'onsite': 'Onsite', 'hybrid': 'Hybrid',
                  'conflicting': 'work arrangement conflicting',
                  'unknown': 'work arrangement unknown'}.get(dimension.state, remote)
    hours = "guaranteed hours" if job.guaranteed_hours else "hours unknown"
    return f"{location} · {remote} · {hours}"


def _requirements_line(item: EvaluatedJob) -> str:
    req = item.assessment.requirements_assessment
    languages = " + ".join(req.language_required) if req.language_required else "language not stated"
    experience = (
        f"{req.experience_min_years:g}+ years"
        if req.experience_min_years is not None else "experience not stated"
    )
    credentials = ", ".join(req.credentials_required) if req.credentials_required else "degree/domain/tools not stated"
    return f"{languages} · {experience} · {credentials} · completeness {req.completeness}"


def _freshness_line(item: EvaluatedJob) -> str:
    if item.job.posted_at is None:
        posted = "posted date unknown"
    else:
        posted = item.job.posted_at.astimezone(timezone.utc).date().isoformat()
    return f"{posted} · factor {item.assessment.freshness:.2f}"


def _ordered_watchouts(values: list[str]) -> list[str]:
    order = (
        ("eligibility", "location", "language", "requirements"),
        ("source", "resolution"),
        ("title", "company"),
        ("pay", "budget", "scope"),
        ("trust", "onboarding", "platform"),
        ("stale", "posting_age", "freshness"),
    )
    def key(value: str) -> tuple[int, str]:
        folded = value.casefold()
        for index, prefixes in enumerate(order):
            if any(prefix in folded for prefix in prefixes):
                return index, folded
        return len(order), folded
    return sorted(dict.fromkeys(values), key=key)


def _trust_line(item: EvaluatedJob) -> str:
    assessment = item.assessment
    if item.job.platform_trust is not None:
        return f"{trust_badge(item.job.platform_trust)} · registry evidence"
    return (
        f"❔ prior {assessment.platform_trust_estimate:.2f} · "
        f"confidence {assessment.platform_trust_confidence:.2f} · "
        f"conservative {assessment.conservative_trust:.2f}"
    )


def _pay_line(item: EvaluatedJob) -> str:
    selected = item.assessment.selected_pay
    if selected is None:
        return "unknown"
    if item.decision.policy_version == 'v5.0.0-rc1':
        basis = {
            'output_audio_hour': 'per recorded audio hour',
            'output_audio_minute': 'per recorded audio minute',
            'labor_hour': 'per working hour', 'hour': 'per working hour',
            'labor_hour_equivalent': 'publisher-stated hourly equivalent (estimate)',
            'fixed': 'fixed project budget', 'fixed_project': 'fixed project budget',
        }.get(selected.basis, selected.basis.replace('_', ' '))
        claim = selected.raw
        authority = selected.observation_source_kind.value if selected.observation_source_kind else 'unknown'
        support = f"{authority}; source {selected.source_field}, observation {selected.observation_id}"
        caveat = '; allocated task confirmed' if item.assessment.action_readiness == 'allocated' else '; allocation not confirmed'
        if item.assessment.time_to_cash_days is None:
            caveat += '; first-payment timing unknown'
        else:
            caveat += f'; captured first-payment estimate {item.assessment.time_to_cash_days:g} days (not guaranteed)'
        if not selected.labor_hourly_supported:
            caveat += (
                '; actual working-hour rate not established'
                if selected.actual_unit == 'labor_hour_equivalent'
                else '; working-hour equivalent unknown'
            )
        if item.assessment.pay_conflict:
            caveat += '; conflicting pay claims: verify before committing'
        return f'{claim} ({basis}; {support}){caveat}'
    source_kind = (
        selected.observation_source_kind.value
        if selected.observation_source_kind is not None else "unknown"
    )
    suffix = (
        f" [source {selected.source_field}/{source_kind}, "
        f"extraction {selected.confidence:.2f}, "
        f"credibility {item.assessment.pay_credibility:.2f}]"
    )
    if item.assessment.pay_conflict:
        suffix += " ⚠ conflicting candidates"
    if selected.basis == "fixed":
        amount = selected.fixed_amount_usd
        amount_text = f" (~${amount:g} fixed)" if amount is not None else ""
        return selected.raw + amount_text + suffix
    return item.job.pay_display_full + suffix


def _priority_line(item: EvaluatedJob, *, show_debug: bool) -> str:
    key = item.decision.priority_key
    tie = key.stable_tiebreaker if show_debug else "stable canonical ID (hidden)"
    return (
        "  Priority key: "
        f"role {key.role_priority} · "
        f"language edge {key.explicit_profile_language_edge} · "
        f"match {key.match_strength} · "
        f"access {key.accessibility_confidence} · "
        f"economics {key.economics_quality} · "
        f"source {key.source_actionability} · "
        f"trust {key.conservative_trust} · "
        f"freshness {key.freshness} · tie {tie}"
    )


def _entry(
    item: EvaluatedJob,
    *,
    show_transition: bool,
    show_priority_debug: bool = False,
) -> str:
    transition = item.decision.notification_transition
    marker = f"[{transition.upper()}] " if show_transition and transition != "none" else ""
    roles = ", ".join(item.assessment.role_families) or "unclassified"
    title_note = ""
    if item.job.title_truncated:
        title_note = " [title reconstructed; verify qualifiers]" if item.job.title_reconstructed else " [title truncated]"
    lines = [
        f"• {marker}{item.job.title}{title_note} — {item.job.company or '—'}",
        (
            f"  Match: {item.assessment.match_strength.value} · "
            f"Eligibility: {item.assessment.eligibility.value} · Roles: {roles}"
        ),
        f"  Access: {_access_line(item)}",
        f"  Requirements: {_requirements_line(item)}",
        f"  Pay: {_pay_line(item)}",
        f"  Apply evidence: {_source_line(item)}",
        f"  Platform: {_trust_line(item)}",
        f"  Freshness: {_freshness_line(item)}",
        f"  Why: {item.decision.explanation}",
    ]
    if item.decision.watch_outs:
        lines.append(
            f"  Watch-outs: {', '.join(_ordered_watchouts(item.decision.watch_outs))}"
        )
    lines.append(_priority_line(item, show_debug=show_priority_debug))
    lines.append(f"  {public_url(item.job.url)}")
    return "\n".join(lines)




def _platform_key(item: EvaluatedJob) -> str | None:
    """Return a flood-control key without grouping unrelated direct listings."""
    key = (item.job.platform_key or "").strip().casefold()
    return key or None

def _run_accounting(result: RunResult) -> str:
    counts = result.counts
    rejects = ", ".join(
        f"{name.removeprefix('reject_').replace('_', ' ')} {counts.get(name, 0)}"
        for name in (
            "reject_scam", "reject_source_quality", "reject_location",
            "reject_language", "reject_credentials", "reject_seniority",
            "reject_pay", "reject_trust", "reject_role", "reject_low_evidence",
        )
    )
    return (
        f"raw {counts['raw_observations']} → normalized "
        f"{counts['normalized_observations']} + failures "
        f"{counts['normalization_failures']} | canonical "
        f"{counts['canonical_jobs']} + duplicates collapsed "
        f"{counts['duplicate_observations_collapsed']} | primary "
        f"{counts['surface_primary']}, verify {counts['surface_verify']} | "
        f"rejected: {rejects}"
    )


def _source_health_alerts(
    result: RunResult,
    *,
    notify_only: bool = False,
) -> list[str]:
    alerts: list[str] = []
    for health in result.source_health:
        status = str(health.get("status") or "")
        transition = str(health.get("alert_transition") or "none")
        if notify_only and not bool(health.get("alert_notify", True)):
            continue
        if status not in {"partial", "failed"} and transition == "none":
            continue
        source = str(health.get("source") or "unknown").replace("_", " ").title()
        if health.get("stage"):
            # Retrieval rows count candidate outcomes, not discovery HTTP
            # failures. Do not render missing legacy counters as measured zeros.
            host = str(health.get("transport_host") or "")
            label = f"{source} ({host})" if host and host != "multiple" else source
            requests = (
                f"; {health['request_count']} HTTP requests"
                if health.get("request_count") is not None else ""
            )
            errors = health.get("error_codes") or {}
            detail = ", ".join(f"{code}: {count}" for code, count in sorted(errors.items()))
            detail = f"; errors {detail}" if detail else ""
            alerts.append(
                f"• {label}: {health['stage']} {status or 'unknown'}; "
                f"{health.get('attempted', 0)} candidates attempted, "
                f"{health.get('succeeded', 0)} succeeded, "
                f"{health.get('failed', 0)} failed, "
                f"{health.get('deferred', 0)} deferred{requests}{detail}. "
                "This is not evidence of job closure."
            )
            continue
        count = int(health.get("item_count") or 0)
        if transition == "recovered":
            alerts.append(f"• {source}: recovered; {count} items retained.")
            continue
        failed = int(health.get("failed_requests") or 0)
        retries = int(health.get("retries") or 0)
        error_type = str(health.get("error_type") or "request failure")
        state = "partial" if status == "partial" else (
            "unavailable" if status == "failed" else status or "unknown"
        )
        transition_note = (
            f"; transition {transition.replace('_', ' ')}"
            if transition != "none" else ""
        )
        alerts.append(
            f"• {source}: {state}; {count} items retained; "
            f"{error_type} after {failed} failed request(s) and {retries} retry/retries"
            f"{transition_note}."
        )
    return alerts


def build_digest(
    result: RunResult,
    *,
    items: list[EvaluatedJob] | None = None,
    include_all: bool = False,
    alerts: list[str] | None = None,
    appendix: str | None = None,
    min_priority: float | None = None,
    top_n: int | None = None,
    per_company_cap: int | None = None,
    per_platform_cap: int | None = None,
) -> DigestBuild:
    """Render notification candidates or a full inspection preview."""
    if result.metadata.engine_version == 'v5.0.0-rc1':
        return _build_release_digest(result, items=items, include_all=include_all,
            top_n=top_n, per_company_cap=per_company_cap,
            per_platform_cap=per_platform_cap, alerts=alerts)
    surfaced = [
        item for item in result.evaluated
        if item.decision.action_band != ActionBand.REJECT
    ]
    if include_all:
        pool = list(surfaced)
    elif items is not None:
        pool = list(items)
    else:
        pool = [
            item for item in surfaced
            if item.decision.notification_transition != "none"
        ]

    threshold = CONFIG.v41.min_display_priority if min_priority is None else min_priority
    band_cap = CONFIG.v41.top_n_per_band if top_n is None else top_n
    automation_cap = CONFIG.v41.automation_top_n if top_n is None else top_n
    company_cap = CONFIG.v41.per_company_cap if per_company_cap is None else per_company_cap
    platform_cap = (
        CONFIG.v41.per_platform_cap
        if per_platform_cap is None
        else per_platform_cap
    )
    suppressed = len(surfaced) - len(pool)
    priority_hidden = 0
    company_hidden = 0
    platform_hidden = 0
    digest_hidden = 0
    company_seen: Counter[str] = Counter()
    displayed: list[EvaluatedJob] = []
    by_section: dict[str, list[EvaluatedJob]] = {
        "primary": [],
        "automation": [],
        "verify": [],
    }
    for item in pool:
        if item.decision.priority_score < threshold:
            priority_hidden += 1
            continue
        if "workflow_automation" in item.assessment.role_families:
            by_section["automation"].append(item)
        elif item.decision.action_band == ActionBand.PRIMARY:
            by_section["primary"].append(item)
        else:
            by_section["verify"].append(item)

    rendered_sections: list[
        tuple[str, list[EvaluatedJob], int, int, int, int]
    ] = []
    for section, label, section_cap in (
        ("primary", "✅ Apply first", band_cap),
        ("automation", "🛠 Automation opportunities", automation_cap),
        ("verify", "🔎 Verify original/details", band_cap),
    ):
        ordered = sorted(
            by_section[section],
            key=lambda item: item.decision.priority_key.sort_tuple,
        )
        section_company_cap = company_cap
        if section == "automation":
            section_company_cap = min(
                company_cap, CONFIG.v41.automation_per_company_cap
            )
        shown: list[EvaluatedJob] = []
        band_company_hidden = 0
        band_platform_hidden = 0
        band_digest_hidden = 0
        platform_seen: Counter[str] = Counter()
        source_identities = {
            _platform_key(item) or f"direct:{item.canonical.canonical_id}"
            for item in ordered
        }
        platform_deferred: list[EvaluatedJob] = []
        for item in ordered:
            counterparty = (
                item.canonical.counterparty_key
                or f"listing:{item.canonical.canonical_id}"
            )
            if company_seen[counterparty] >= max(0, section_company_cap):
                company_hidden += 1
                band_company_hidden += 1
                continue
            platform = _platform_key(item)
            if (
                platform
                and platform_cap > 0
                and platform_seen[platform] >= platform_cap
            ):
                platform_deferred.append(item)
                continue
            if len(shown) >= max(0, section_cap):
                digest_hidden += 1
                band_digest_hidden += 1
                continue
            shown.append(item)
            displayed.append(item)
            company_seen[counterparty] += 1
            if platform:
                platform_seen[platform] += 1

        if (
            CONFIG.v41.platform_backfill_when_single_source
            and len(source_identities) == 1
            and len(shown) < max(0, section_cap)
        ):
            while platform_deferred and len(shown) < max(0, section_cap):
                item = platform_deferred.pop(0)
                counterparty = (
                    item.canonical.counterparty_key
                    or f"listing:{item.canonical.canonical_id}"
                )
                if company_seen[counterparty] >= max(0, section_company_cap):
                    company_hidden += 1
                    band_company_hidden += 1
                    continue
                shown.append(item)
                displayed.append(item)
                company_seen[counterparty] += 1
                platform = _platform_key(item)
                if platform:
                    platform_seen[platform] += 1

        platform_hidden += len(platform_deferred)
        band_platform_hidden += len(platform_deferred)
        rendered_sections.append((
            label,
            shown,
            len(ordered),
            band_company_hidden,
            band_platform_hidden,
            band_digest_hidden,
        ))

    display_counts = {
        "surface_total": len(surfaced),
        "displayed": len(displayed),
        "hidden_company_cap": company_hidden,
        "hidden_platform_cap": platform_hidden,
        "hidden_digest_cap": digest_hidden,
        "hidden_priority_floor": priority_hidden,
        "suppressed_by_notification_policy": suppressed,
    }
    errors: list[str] = []
    if display_counts["surface_total"] != sum(
        display_counts[key] for key in (
            "displayed", "hidden_company_cap", "hidden_platform_cap", "hidden_digest_cap",
            "hidden_priority_floor", "suppressed_by_notification_policy",
        )
    ):
        errors.append("presentation_accounting_equation")
    if not result.accounting_ok:
        errors.extend(result.accounting_errors)

    lines = [
        f"🐕 JobHound {result.metadata.engine_version.upper()} digest — {result.metadata.as_of.astimezone().date().isoformat()}",
        _run_accounting(result),
        (
            f"displayed {display_counts['displayed']} | hidden: company "
            f"{company_hidden}, platform {platform_hidden}, digest {digest_hidden}, "
            f"priority {priority_hidden}, "
            f"notification policy {suppressed}"
        ),
        "",
    ]
    source_health_lines = _source_health_alerts(result)
    operational_alerts = _source_health_alerts(result, notify_only=True)
    if source_health_lines:
        lines.append(
            f"⚠ Source coverage degraded/status ({len(source_health_lines)})"
        )
        lines.extend(source_health_lines)
        lines.append("")
    if alerts:
        lines.append(f"⚠ Trust events ({len(alerts)})")
        lines.extend(alerts)
        lines.append("")

    for (
        label,
        shown,
        eligible_count,
        band_company_hidden,
        band_platform_hidden,
        band_digest_hidden,
    ) in rendered_sections:
        if not eligible_count:
            continue
        hidden_detail = []
        if band_company_hidden:
            hidden_detail.append(f"company cap {band_company_hidden}")
        if band_platform_hidden:
            hidden_detail.append(f"platform cap {band_platform_hidden}")
        if band_digest_hidden:
            hidden_detail.append(f"digest cap {band_digest_hidden}")
        suffix = f"; hidden: {', '.join(hidden_detail)}" if hidden_detail else ""
        lines.append(
            f"{label} ({len(shown)} shown / {eligible_count} eligible{suffix})"
        )
        for item in shown:
            lines.append(_entry(
                item,
                show_transition=not include_all,
                show_priority_debug=include_all,
            ))
        lines.append("")

    if not displayed:
        lines.append(
            f"No {result.metadata.engine_version.upper()} jobs met this digest's "
            "display and notification policy."
        )
        lines.append("")

    lines.extend([
        "📊 Run accounting",
        f"  Engine: {result.metadata.engine_version} · run {result.metadata.run_id}",
        f"  Decision ledger: {'PASS' if result.accounting_ok else 'FAIL'}",
        f"  Presentation ledger: {'PASS' if not errors else 'FAIL'}",
        f"  Snapshot: {result.metadata.snapshot_path or 'not captured'}",
    ])
    if appendix:
        lines.extend(["", appendix])
    text = "\n".join(lines).rstrip() + "\n"
    return DigestBuild(
        text=text,
        displayed=displayed,
        display_counts=display_counts,
        accounting_ok=not errors,
        accounting_errors=errors,
        operational_alerts=operational_alerts,
    )


def _release_entry(item: EvaluatedJob) -> str:
    decision, assessment = item.decision, item.assessment
    lines = [f"• {decision.next_action.replace('_', ' ').upper()} | {item.job.title} | {item.job.company or 'Employer unverified'}"]
    if decision.action_band == ActionBand.VERIFY:
        for task in decision.verification_tasks[:2]:
            lines.append(f"  Check: {task['missing_fact'].replace('_', ' ')} — {task['next_step']}")
            lines.append(f"    Owner: {task['responsible_actor']}; attempts {task['attempts']}; recheck/expiry {task['expires_at']}.")
    lines.extend([
        f"  Fit: {decision.explanation}",
        f"  Pay: {_pay_line(item)}",
        f"  Requirements: {_requirements_line(item)}",
        f"  Readiness: {assessment.action_readiness.replace('_', ' ')}; {_access_line(item)}",
        f"  Next: {decision.next_step}",
    ])
    dimensions = assessment.evidence_dimensions
    if dimensions:
        publisher = dimensions['publisher'].value
        lines.append(
            f"  Evidence: {publisher['source_kind']} via {publisher['host']}; "
            f"relevance {dimensions['relevance'].state}; applicant eligibility {dimensions['eligibility'].state}; "
            f"pay {dimensions['economics'].state}."
        )
    if assessment.verified_open_at:
        lines.append(f"  Checked: matching role open at {assessment.verified_open_at.isoformat()}; selection is not guaranteed.")
    else:
        lines.append('  Checked: current opening not verified.')
    # Exact stored priority components, not a second additive score.
    key = decision.priority_key
    lines.append(f"  Ordering: readiness {key.action_readiness}, time-to-cash evidence {key.supported_time_to_cash:g}, action-cost evidence {key.action_cost_and_friction}, task fit {key.match_strength}/{key.role_priority}, economics evidence {key.economics_quality}, language edge {key.explicit_profile_language_edge}, source {key.source_actionability}, conservative trust {key.conservative_trust}, freshness {key.freshness}.")
    lines.append(f"  {public_url(item.job.url)}")
    return '\n'.join(lines)


def _build_release_digest(result, *, items, include_all, top_n, per_company_cap, per_platform_cap, alerts):
    surfaced = [row for row in result.evaluated if row.decision.action_band != ActionBand.REJECT]
    requested_ids = {row.canonical.canonical_id for row in items} if items is not None else None
    counts = dict(surface_total=len(surfaced), displayed=0, hidden_company_cap=0,
                  hidden_platform_cap=0, hidden_digest_cap=0, hidden_priority_floor=0,
                  suppressed_by_notification_policy=0, suppressed_watch=0,
                  suppressed_known_state=0, displayed_primary=0, displayed_verify=0)
    shown = []
    companies, marketplaces = Counter(), Counter()
    band_count = Counter()
    company_cap = CONFIG.v55.max_per_employer if per_company_cap is None else per_company_cap
    marketplace_cap = CONFIG.v55.max_per_marketplace if per_platform_cap is None else per_platform_cap
    for item in sorted(surfaced, key=lambda row: (0 if row.decision.action_band == ActionBand.PRIMARY else 1, *row.decision.priority_key.sort_tuple)):
        if item.decision.lifecycle != 'active':
            key = 'suppressed_known_state' if item.assessment.lifecycle_reason in {'known_account_block', 'application_already_submitted'} else 'suppressed_watch'
            counts[key] += 1
            continue
        if not include_all and ((requested_ids is not None and item.canonical.canonical_id not in requested_ids) or (requested_ids is None and item.decision.notification_transition == 'none')):
            counts['suppressed_by_notification_policy'] += 1
            continue
        company = item.canonical.counterparty_key or item.canonical.canonical_id
        marketplace = item.job.platform_key if item.job.platform_key == 'upwork' else None
        band = item.decision.action_band.value
        limit = top_n if top_n is not None else (CONFIG.v55.max_primary_actions if band == 'primary' else CONFIG.v55.max_verify_actions)
        if companies[company] >= company_cap:
            counts['hidden_company_cap'] += 1
        elif marketplace and marketplace_cap > 0 and marketplaces[marketplace] >= marketplace_cap:
            counts['hidden_platform_cap'] += 1
        elif band_count[band] >= limit:
            counts['hidden_digest_cap'] += 1
        else:
            shown.append(item)
            counts['displayed'] += 1
            counts[f'displayed_{band}'] += 1
            companies[company] += 1
            if marketplace:
                marketplaces[marketplace] += 1
            band_count[band] += 1
    equation = sum(counts[key] for key in ('displayed','hidden_company_cap','hidden_platform_cap','hidden_digest_cap','hidden_priority_floor','suppressed_by_notification_policy','suppressed_watch','suppressed_known_state'))
    errors = list(result.accounting_errors)
    if equation != len(surfaced):
        errors.append('presentation_accounting_equation')
    health = _source_health_alerts(result)
    lines = [f"JobHound V5 release preview — {result.metadata.as_of.date().isoformat()}", '']
    if not counts['displayed_primary']:
        lines.extend(['No new verified actions today.', ''])
    if health:
        lines.extend(['Coverage degraded/status:', *dict.fromkeys(health), ''])
    if errors:
        lines.extend(['INTEGRITY FAILURE — normal opportunity delivery blocked.', *errors])
    else:
        for band, title in ((ActionBand.PRIMARY, 'Apply / act first'), (ActionBand.VERIFY, 'Worth a bounded check')):
            rows = [row for row in shown if row.decision.action_band == band]
            if rows:
                lines.extend([title, '', *[_release_entry(row) for row in rows], ''])
    lines.extend(['Audit accounting:', _run_accounting(result),
        f"Presentation: {counts}",
        f"Decision ledger: {'PASS' if result.accounting_ok else 'FAIL'}; presentation ledger: {'PASS' if not errors else 'FAIL'}",
        f"Snapshot: {result.metadata.snapshot_path or 'not captured'}"])
    return DigestBuild(text='\n'.join(lines)+'\n', displayed=shown if not errors else [],
        display_counts=counts, accounting_ok=not errors, accounting_errors=errors,
        operational_alerts=list(dict.fromkeys(health)))


def write_digest(text: str, *, date_stamp: str, directory: Path | None = None) -> Path:
    target = directory or (_ROOT / "data")
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"digest_{date_stamp}.md"
    path.write_text(text, encoding="utf-8")
    return path
