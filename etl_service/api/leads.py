"""
api/leads.py
------------
Lead capture endpoint — receives submissions from the landing page
audit form and persists them for the sales team.

Current implementation: appends to a JSONL file.
Production-ready upgrade path: push to a CRM (Pipedrive, HubSpot) or
trigger a Zapier/Make webhook.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security, status
from fastapi.security import APIKeyHeader
from loguru import logger
from pydantic import BaseModel, EmailStr, Field, field_validator

from etl_service.config import settings

# ====================================================================== #
# In-memory sliding-window rate limiter (resets on process restart)
# ====================================================================== #

_RATE_WINDOW_SECONDS = 60
_RATE_MAX_REQUESTS = 5
_rate_store: dict[str, deque] = defaultdict(deque)


def _resolve_client_ip(request: Request) -> str:
    """Returns the real client IP, honoring X-Forwarded-For when behind a proxy."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(client_ip: str) -> None:
    """Raises HTTP 429 if the client IP exceeds _RATE_MAX_REQUESTS per window."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=_RATE_WINDOW_SECONDS)
    timestamps = _rate_store[client_ip]
    while timestamps and timestamps[0] < cutoff:
        timestamps.popleft()
    if len(timestamps) >= _RATE_MAX_REQUESTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Muitas tentativas. Aguarde 1 minuto antes de tentar novamente.",
            headers={"Retry-After": str(_RATE_WINDOW_SECONDS)},
        )
    timestamps.append(now)


# ====================================================================== #
# Models
# ====================================================================== #

class LeadSubmission(BaseModel):
    """Payload from the landing page form."""

    name: str = Field(..., min_length=3, max_length=120)
    restaurant: str = Field(..., min_length=2, max_length=120)
    revenue: str = Field(..., description="Revenue bracket selected in the form.")
    systems: list[str] = Field(..., min_length=1, description="Systems currently used by the restaurant.")
    phone: str = Field(..., min_length=10, max_length=20)
    email: Optional[EmailStr] = None
    source: str = Field(default="landing_page")

    @field_validator("phone")
    @classmethod
    def strip_phone(cls, v: str) -> str:
        """Keep only digits."""
        digits = "".join(c for c in v if c.isdigit())
        if len(digits) < 10 or len(digits) > 11:
            raise ValueError("Telefone inválido. Use DDD + número (10 ou 11 dígitos).")
        return digits


class LeadResponse(BaseModel):
    id: str
    received_at: str
    message: str


# ====================================================================== #
# Router
# ====================================================================== #

router = APIRouter(prefix="/leads", tags=["Leads"])

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def _require_leads_api_key(api_key: Optional[str] = Security(_api_key_header)) -> None:
    """Guards read endpoints with an optional API key (disabled when DATAFLOW_LEADS_API_KEY is empty)."""
    configured = settings.leads_api_key
    if not configured:
        return  # Auth disabled — dev/local environment
    if api_key != configured:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing X-API-Key header.",
        )


def _get_leads_file() -> Path:
    """Ensures the leads JSONL file exists and returns its path."""
    leads_dir = Path(settings.output_dir) / "leads"
    leads_dir.mkdir(parents=True, exist_ok=True)
    return leads_dir / "leads.jsonl"


@router.post(
    "/",
    response_model=LeadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a lead from the landing page audit form.",
)
async def create_lead(lead: LeadSubmission, request: Request) -> LeadResponse:
    """
    Persists a new lead submission and returns a confirmation payload.
    The CRM / sales team should poll GET /leads or receive webhook notifications.
    """
    client_ip = _resolve_client_ip(request)
    _check_rate_limit(client_ip)

    lead_id = f"LEAD-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    received_at = datetime.now(timezone.utc).isoformat()

    record = {
        "id":          lead_id,
        "received_at": received_at,
        **lead.model_dump(),
    }

    try:
        with open(_get_leads_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.error("[Leads] Could not persist lead: {exc}", exc=str(exc))
        raise HTTPException(
            status_code=500,
            detail="Não foi possível registrar sua solicitação. Tente novamente.",
        ) from exc

    logger.success(
        "[Leads] New lead — id={id}, restaurant='{rest}', revenue={rev}",
        id=lead_id, rest=lead.restaurant, rev=lead.revenue,
    )

    return LeadResponse(
        id=lead_id,
        received_at=received_at,
        message="Solicitação recebida. Entraremos em contato em até 48 horas.",
    )


@router.get(
    "/",
    response_model=list[dict],
    summary="List captured leads — sales team only (requires X-API-Key in production).",
)
async def list_leads(
    skip: int = Query(0, ge=0, description="Number of leads to skip (offset)."),
    limit: int = Query(100, ge=1, le=1000, description="Maximum leads to return per page."),
    _: None = Depends(_require_leads_api_key),
) -> list[dict]:
    """Returns a paginated slice of leads. Use ?skip=N&limit=M to page through results."""
    leads_file = _get_leads_file()
    if not leads_file.exists():
        return []

    leads: list[dict] = []
    with open(leads_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    leads.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("[Leads] Skipping malformed line.")

    return leads[skip : skip + limit]


@router.get(
    "/count",
    summary="Return the total number of captured leads (lightweight, no data loaded).",
    dependencies=[Depends(_require_leads_api_key)],
)
async def count_leads() -> dict:
    """Streams the JSONL file line-by-line to count leads without loading full payloads."""
    leads_file = _get_leads_file()
    if not leads_file.exists():
        return {"count": 0}
    count = sum(1 for line in leads_file.open("r", encoding="utf-8") if line.strip())
    return {"count": count}
