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
from datetime import datetime
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


def _read_all_dre_artefacts() -> list[dict]:
    """Reads every `*_dre.json` file in the output directory, sorted by date."""
    output_dir = Path(settings.output_dir)
    if not output_dir.exists():
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
        generated_at=artefact.get("generated_at", datetime.now().isoformat()),
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
