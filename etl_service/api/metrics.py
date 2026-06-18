"""
api/metrics.py
--------------
Dashboard metrics endpoint.

Exposes aggregated KPIs computed from the latest ETL runs — suitable for
feeding a web dashboard, BI tool, or C-level executive report.

Design note: in production this would read from a warehouse (Postgres,
ClickHouse, or a materialized view). For the MVP we read directly from
the JSON artefacts dropped by the ReportLoader into `output/`.
"""

from __future__ import annotations

import glob
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Path as FPath, Query
from loguru import logger
from pydantic import BaseModel, Field

from etl_service.api import alerts as _alerts_module
from etl_service.config import settings


# ====================================================================== #
# Response models
# ====================================================================== #

class PeriodMetrics(BaseModel):
    """Financial KPIs for a single reference period."""

    reference_month: str
    gross_revenue_brl: float
    net_revenue_brl: float
    total_deductions_brl: float
    ifood_commission_brl: float
    card_fees_brl: float
    net_margin_pct: float = Field(..., description="Net / Gross × 100")
    gaps_count: int
    gaps_value_brl: float
    generated_at: str


class TrendPoint(BaseModel):
    month: str
    gross_revenue: float
    net_revenue: float
    gaps_count: int


class DashboardResponse(BaseModel):
    """Response for GET /metrics/dashboard."""

    latest_period: Optional[PeriodMetrics]
    trend_last_n_months: list[TrendPoint]
    total_recovered_brl: float
    total_hours_saved: float
    alerts_open: int


# ====================================================================== #
# Router
# ====================================================================== #

router = APIRouter(prefix="/metrics", tags=["Metrics"])


_DRE_CACHE: tuple[float, list[dict]] | None = None
_DRE_CACHE_TTL = 30.0  # seconds — re-read disk at most once every 30s


def _read_all_dre_artefacts() -> list[dict]:
    """Reads every `*_dre.json` file in the output directory, sorted by date.

    Results are cached in-memory for _DRE_CACHE_TTL seconds to avoid
    hammering disk on every dashboard / period request.
    """
    global _DRE_CACHE
    now = time.monotonic()
    if _DRE_CACHE is not None and (now - _DRE_CACHE[0]) < _DRE_CACHE_TTL:
        return _DRE_CACHE[1]

    output_dir = Path(settings.output_dir)
    if not output_dir.exists():
        _DRE_CACHE = (now, [])
        return []

    pattern = str(output_dir / "*_dre.json")
    files = sorted(glob.glob(pattern))
    artefacts: list[dict] = []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                artefacts.append(json.load(fh))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("[Metrics] Could not read '{f}': {exc}", f=f, exc=exc)

    _DRE_CACHE = (now, artefacts)
    return artefacts


def _as_period_metrics(artefact: dict) -> PeriodMetrics:
    """Builds a PeriodMetrics from a DRE JSON artefact."""
    gross = float(artefact.get("total_gross_revenue_brl", 0) or 0)
    net   = float(artefact.get("net_revenue_brl", 0) or 0)
    margin = round((net / gross * 100), 2) if gross > 0 else 0.0

    gaps = artefact.get("gaps", []) or []
    gaps_value = round(sum(float(g.get("amount_brl", 0) or 0) for g in gaps), 2)

    return PeriodMetrics(
        reference_month=artefact.get("reference_month", "N/A"),
        gross_revenue_brl=gross,
        net_revenue_brl=net,
        total_deductions_brl=float(artefact.get("total_deductions_brl", 0) or 0),
        ifood_commission_brl=float(artefact.get("total_ifood_commission_brl", 0) or 0),
        card_fees_brl=float(artefact.get("total_card_fees_brl", 0) or 0),
        net_margin_pct=margin,
        gaps_count=int(artefact.get("gaps_detected", 0) or 0),
        gaps_value_brl=gaps_value,
        generated_at=artefact.get("generated_at", datetime.now(timezone.utc).isoformat()),
    )


# ---------------------------------------------------------------------- #
# GET /metrics/dashboard
# ---------------------------------------------------------------------- #

@router.get(
    "/dashboard",
    response_model=DashboardResponse,
    summary="Consolidated dashboard KPIs across all recorded ETL runs.",
)
async def dashboard(trend_months: int = Query(6, ge=1, le=24)) -> DashboardResponse:
    """
    Returns executive-level KPIs:
    - Latest period's financial snapshot
    - Trend of the last N months (default 6)
    - Total recovered gaps across history
    - Estimated hours saved (gaps_count × 2h average)
    - Open alerts (placeholder until alerts.py is wired)
    """
    artefacts = _read_all_dre_artefacts()
    if not artefacts:
        logger.info("[Metrics] No DRE artefacts found in output/.")
        return DashboardResponse(
            latest_period=None,
            trend_last_n_months=[],
            total_recovered_brl=0.0,
            total_hours_saved=0.0,
            alerts_open=0,
        )

    periods = [_as_period_metrics(a) for a in artefacts]
    periods.sort(key=lambda p: p.reference_month)

    latest = periods[-1]
    trend = [
        TrendPoint(
            month=p.reference_month,
            gross_revenue=p.gross_revenue_brl,
            net_revenue=p.net_revenue_brl,
            gaps_count=p.gaps_count,
        )
        for p in periods[-trend_months:]
    ]

    total_recovered = round(sum(p.gaps_value_brl for p in periods), 2)
    total_hours_saved = round(sum(p.gaps_count for p in periods) * 2.0, 1)
    alerts_open = sum(1 for a in _alerts_module._ALERTS.values() if not a.acknowledged)

    return DashboardResponse(
        latest_period=latest,
        trend_last_n_months=trend,
        total_recovered_brl=total_recovered,
        total_hours_saved=total_hours_saved,
        alerts_open=alerts_open,
    )


# ---------------------------------------------------------------------- #
# GET /metrics/periods  — lightweight index of available months
# ---------------------------------------------------------------------- #

class PeriodSummary(BaseModel):
    """Compact summary returned by GET /metrics/periods."""

    reference_month: str
    gross_revenue_brl: float
    net_revenue_brl: float
    gaps_count: int


@router.get(
    "/periods",
    response_model=list[PeriodSummary],
    summary="List all available reference periods with a compact financial summary.",
)
async def list_periods() -> list[PeriodSummary]:
    """
    Returns one entry per processed month, sorted chronologically.
    Lightweight alternative to /dashboard — suitable for populating a
    period-selector dropdown in the frontend.
    """
    artefacts = _read_all_dre_artefacts()
    summaries = [
        PeriodSummary(
            reference_month=a.get("reference_month", "N/A"),
            gross_revenue_brl=float(a.get("total_gross_revenue_brl", 0) or 0),
            net_revenue_brl=float(a.get("net_revenue_brl", 0) or 0),
            gaps_count=int(a.get("gaps_detected", 0) or 0),
        )
        for a in artefacts
    ]
    summaries.sort(key=lambda s: s.reference_month)
    return summaries


# ---------------------------------------------------------------------- #
# GET /metrics/period/{reference_month}
# ---------------------------------------------------------------------- #

@router.get(
    "/period/{reference_month}",
    response_model=PeriodMetrics,
    summary="Financial metrics for a specific period (YYYY-MM).",
)
async def period_metrics(
    reference_month: Annotated[
        str,
        FPath(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Period in YYYY-MM format, e.g. '2026-03'."),
    ],
) -> PeriodMetrics:
    """Returns the DRE snapshot for the requested month."""
    artefacts = _read_all_dre_artefacts()
    match = next(
        (a for a in artefacts if a.get("reference_month") == reference_month),
        None,
    )
    if not match:
        raise HTTPException(
            status_code=404,
            detail=f"No DRE artefact found for reference month '{reference_month}'.",
        )
    return _as_period_metrics(match)


# ---------------------------------------------------------------------- #
# GET /metrics/period/{reference_month}/gaps  — gap drill-down
# ---------------------------------------------------------------------- #

class GapDetail(BaseModel):
    """Full detail of a single detected cash-flow gap."""

    transaction_id: Optional[str] = None
    source: Optional[str] = None
    gap_type: str
    amount_brl: float
    detail: str


@router.get(
    "/period/{reference_month}/gaps",
    response_model=list[GapDetail],
    summary="List all cash-flow gaps detected for a specific reference period.",
)
async def period_gaps(
    reference_month: Annotated[
        str,
        FPath(
            pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
            description="Period in YYYY-MM format, e.g. '2026-03'.",
        ),
    ],
    min_amount: float = Query(0.0, ge=0, description="Filter gaps with amount ≥ this value (BRL)."),
) -> list[GapDetail]:
    """
    Returns the full gap list for the requested month, optionally filtered
    by a minimum gap amount. Ordered by amount descending (largest first).
    """
    artefacts = _read_all_dre_artefacts()
    match = next(
        (a for a in artefacts if a.get("reference_month") == reference_month),
        None,
    )
    if not match:
        raise HTTPException(
            status_code=404,
            detail=f"No DRE artefact found for reference month '{reference_month}'.",
        )

    raw_gaps: list[dict] = match.get("gaps", []) or []
    details = [
        GapDetail(
            transaction_id=g.get("transaction_id"),
            source=g.get("source"),
            gap_type=g.get("gap_type", "UNKNOWN"),
            amount_brl=round(float(g.get("amount_brl", 0) or 0), 2),
            detail=g.get("detail", ""),
        )
        for g in raw_gaps
        if float(g.get("amount_brl", 0) or 0) >= min_amount
    ]
    details.sort(key=lambda g: g.amount_brl, reverse=True)
    logger.info(
        "[Metrics] Gap drill-down — period={p}, returned={n}, min_amount={m}",
        p=reference_month, n=len(details), m=min_amount,
    )
    return details
