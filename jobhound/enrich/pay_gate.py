"""Effective-rate pay floor (v3 Patch #2). Replaces the nominal floor that
lived in enrich/pay.py.

    effective = nominal × availability × (1 − unpaid_overhead)

availability: 1.0 if the offer guarantees hours (a stated FT/guaranteed-hours
commitment overrides the platform's empty-queue statistics); else the
platform's availability_est from the trust registry; else 1.0 (unknown
platform -> effective == nominal -> behavior identical to a flat floor).

Consequences at floor=4.5 (encoded in tests/test_patch2.py): a $5/hr
guaranteed-FT OneForma offer clears at $4.75 eff; a $3.50/hr flexible maps
project rejects at $2.33 eff; Outlier needs $18 nominal to scrape past —
low-trust platforms must clear a higher nominal bar, which is the point.
"""
from __future__ import annotations


def _floor_from(cfg) -> float:
    # Accepts the pipeline's PayCfg or the patch-spec dict {"pay": {"floor_usd": x}}.
    if isinstance(cfg, dict):
        return float(cfg["pay"].get("floor_usd", 4.5))
    return float(cfg.floor_usd)


def clamp_effective(effective: float | None, nominal: float | None) -> float | None:
    """Effective pay cannot exceed nominal pay or fall below zero."""
    if effective is None or nominal is None:
        return effective
    return min(max(0.0, effective), max(0.0, nominal))


def apply_pay_gate(job, trust_registry, cfg, *, conservative: bool = False) -> None:
    floor = _floor_from(cfg)
    if conservative:
        nominal = job.pay_min_hourly_usd or job.pay_max_hourly_usd
        # V4 owns effective-rate calculation here. Do not retain the midpoint
        # estimate that trust enrichment may have attached.
        job.effective_hourly_usd = None
    else:
        nominal = job.pay_max_hourly_usd or job.pay_min_hourly_usd

    if nominal is None:
        job.pay_ok = None                      # unknown pay: down-rank, never reject
        job.reasons.append("pay_unknown (down-ranked, not rejected)")
        return

    trust = trust_registry.get(job.platform_key) if job.platform_key else None
    if conservative and trust is None and not job.guaranteed_hours:
        # A stated rate can clear the nominal floor without pretending that
        # queue availability and unpaid overhead are known.
        job.pay_ok = nominal >= floor
        detail = (
            f"${nominal:.2f} conservative stated rate; effective unknown "
            f"(no counterparty availability/overhead evidence) vs floor ${floor:.2f}"
        )
        if job.pay_ok:
            job.reasons.append(f"pay_ok: {detail}")
        else:
            job.verdict = "rejected_pay"
            job.reasons.append(f"below_floor: {detail}")
        return

    if job.guaranteed_hours:
        availability, why_avail = 1.0, "guaranteed hours"
    elif trust is not None:
        availability, why_avail = trust.availability_est, f"{job.platform_key} availability_est"
    else:
        availability, why_avail = 1.0, "unknown platform"
    overhead = trust.unpaid_overhead_est if trust is not None else 0.0

    raw_effective = nominal * availability * (1.0 - overhead)
    job.effective_hourly_usd = round(clamp_effective(raw_effective, nominal), 2)
    detail = (f"${nominal:.2f} x {availability:.2f} ({why_avail}) "
              f"x (1-{overhead:.2f}) = ${job.effective_hourly_usd:.2f} eff "
              f"vs floor ${floor:.2f}")

    if job.effective_hourly_usd >= floor:
        job.pay_ok = True
        job.reasons.append(f"pay_ok: {detail}")
    else:
        job.pay_ok = False
        job.verdict = "rejected_pay"
        job.reasons.append(f"below_floor: {detail}")
