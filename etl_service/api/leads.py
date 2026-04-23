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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from fastapi.security import APIKeyHeader
from loguru import logger
from pydantic import BaseModel, EmailStr, Field, field_validator

from etl_service.config import settings


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
async def create_lead(lead: LeadSubmission) -> LeadResponse:
    """
    Persists a new lead submission and returns a confirmation payload.
    The CRM / sales team should poll GET /leads or receive webhook notifications.
    """
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
