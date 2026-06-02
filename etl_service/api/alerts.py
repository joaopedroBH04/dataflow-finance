"""
api/alerts.py
-------------
Real-time alerts router.

Flow:
1. Finance team registers webhook URLs (Slack, Telegram, WhatsApp Business,
   or custom) via POST /alerts/subscribers.
2. Whenever the ETL pipeline detects a gap above the configured threshold,
   the alerts module dispatches an HTTP POST to every registered subscriber
   with the gap payload.
3. GET /alerts/active lists alerts still open (not acknowledged).
4. POST /alerts/{alert_id}/ack marks an alert as acknowledged.

For the MVP, subscribers and alerts are persisted in-memory. In production,
swap for Postgres + Redis for distributed worker support.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, Security, status
from fastapi.security import APIKeyHeader
from loguru import logger
from pydantic import AnyHttpUrl, BaseModel, Field

from etl_service.config import settings


# ====================================================================== #
# Models
# ====================================================================== #

class AlertSeverity(str, Enum):
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"
    CRITICAL = "critical"


class AlertChannel(str, Enum):
    SLACK    = "slack"
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    EMAIL    = "email"
    GENERIC  = "generic_webhook"


class AlertSubscriber(BaseModel):
    """A webhook endpoint that should receive alerts."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    channel: AlertChannel
    webhook_url: AnyHttpUrl
    severity_filter: AlertSeverity = Field(default=AlertSeverity.MEDIUM)
    active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SubscriberCreateRequest(BaseModel):
    channel: AlertChannel
    webhook_url: AnyHttpUrl
    severity_filter: AlertSeverity = AlertSeverity.MEDIUM


class Alert(BaseModel):
    """Represents a single alert event (e.g. gap detected)."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    severity: AlertSeverity
    title: str
    message: str
    transaction_id: Optional[str] = None
    amount_brl: Optional[float] = None
    reference_month: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    acknowledged: bool = False
    acknowledged_at: Optional[datetime] = None


# ====================================================================== #
# In-memory store (replace with Postgres in production)
# ====================================================================== #

_SUBSCRIBERS: dict[str, AlertSubscriber] = {}
_ALERTS: dict[str, Alert] = {}


# ====================================================================== #
# Router
# ====================================================================== #

router = APIRouter(prefix="/alerts", tags=["Alerts"])

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def _require_alerts_api_key(
    api_key: Optional[str] = Security(_api_key_header),
) -> None:
    """Guards alert management endpoints with an API key (skipped when key is unset)."""
    configured = settings.leads_api_key
    if not configured:
        return  # Auth disabled — dev/local environment
    if api_key != configured:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing X-API-Key header.",
        )


# ---------------------------------------------------------------------- #
# Subscribers
# ---------------------------------------------------------------------- #

@router.post(
    "/subscribers",
    response_model=AlertSubscriber,
    status_code=status.HTTP_201_CREATED,
    summary="Register a webhook to receive alerts.",
    dependencies=[Depends(_require_alerts_api_key)],
)
async def register_subscriber(req: SubscriberCreateRequest) -> AlertSubscriber:
    sub = AlertSubscriber(
        channel=req.channel,
        webhook_url=req.webhook_url,
        severity_filter=req.severity_filter,
    )
    _SUBSCRIBERS[sub.id] = sub
    logger.info(
        "[Alerts] Subscriber registered — id={id}, channel={ch}, url={url}",
        id=sub.id, ch=sub.channel, url=sub.webhook_url,
    )
    return sub


@router.get(
    "/subscribers",
    response_model=list[AlertSubscriber],
    dependencies=[Depends(_require_alerts_api_key)],
)
async def list_subscribers() -> list[AlertSubscriber]:
    return list(_SUBSCRIBERS.values())


@router.delete(
    "/subscribers/{subscriber_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(_require_alerts_api_key)],
)
async def delete_subscriber(subscriber_id: str) -> Response:
    if subscriber_id not in _SUBSCRIBERS:
        raise HTTPException(status_code=404, detail="Subscriber not found.")
    del _SUBSCRIBERS[subscriber_id]
    logger.info("[Alerts] Subscriber deleted — id={id}", id=subscriber_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------- #
# Alerts
# ---------------------------------------------------------------------- #

@router.get(
    "/active",
    response_model=list[Alert],
    summary="List all unacknowledged alerts.",
    dependencies=[Depends(_require_alerts_api_key)],
)
async def list_active_alerts() -> list[Alert]:
    return [a for a in _ALERTS.values() if not a.acknowledged]


@router.get(
    "/",
    response_model=list[Alert],
    dependencies=[Depends(_require_alerts_api_key)],
)
async def list_all_alerts() -> list[Alert]:
    return list(_ALERTS.values())


@router.post(
    "/{alert_id}/ack",
    response_model=Alert,
    summary="Mark an alert as acknowledged.",
    dependencies=[Depends(_require_alerts_api_key)],
)
async def acknowledge_alert(alert_id: str) -> Alert:
    alert = _ALERTS.get(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found.")
    alert.acknowledged = True
    alert.acknowledged_at = datetime.now(timezone.utc)
    logger.info("[Alerts] Acknowledged alert={id}", id=alert_id)
    return alert


# ====================================================================== #
# Dispatcher (called by the ETL pipeline)
# ====================================================================== #

SEVERITY_ORDER = {
    AlertSeverity.LOW: 0,
    AlertSeverity.MEDIUM: 1,
    AlertSeverity.HIGH: 2,
    AlertSeverity.CRITICAL: 3,
}


async def dispatch_alert(alert: Alert) -> None:
    """
    Fan-out an alert to every subscriber whose severity filter
    is ≤ alert.severity. Invoked from the transformer when a gap
    is detected above the business threshold.
    """
    _ALERTS[alert.id] = alert

    targets = [
        s for s in _SUBSCRIBERS.values()
        if s.active and SEVERITY_ORDER[s.severity_filter] <= SEVERITY_ORDER[alert.severity]
    ]
    if not targets:
        logger.debug("[Alerts] No subscribers for severity '{sev}'.", sev=alert.severity)
        return

    payload = alert.model_dump(mode="json")
    async with httpx.AsyncClient(timeout=10.0) as client:
        await asyncio.gather(
            *[_post_to_subscriber(client, sub, payload, alert.id) for sub in targets],
            return_exceptions=True,
        )


async def _post_to_subscriber(
    client: httpx.AsyncClient,
    sub: "AlertSubscriber",
    payload: dict,
    alert_id: str,
) -> None:
    try:
        response = await client.post(str(sub.webhook_url), json=payload)
        logger.info(
            "[Alerts] Dispatched to {ch} ({status}) — alert={id}",
            ch=sub.channel, status=response.status_code, id=alert_id,
        )
    except httpx.HTTPError as exc:
        logger.error(
            "[Alerts] Dispatch FAILED to {url}: {exc}",
            url=sub.webhook_url, exc=str(exc),
        )


def build_alert_from_gap(gap: dict, reference_month: str) -> Alert:
    """Helper used by the transformer to turn a GapSummary dict into an Alert."""
    amount = float(gap.get("amount_brl", 0) or 0)
    if amount >= 5000:
        severity = AlertSeverity.CRITICAL
    elif amount >= 1000:
        severity = AlertSeverity.HIGH
    elif amount >= 100:
        severity = AlertSeverity.MEDIUM
    else:
        severity = AlertSeverity.LOW

    return Alert(
        severity=severity,
        title=f"[{gap.get('gap_type', 'GAP')}] Furo detectado - R$ {amount:.2f}",
        message=gap.get("detail", "Divergencia financeira detectada."),
        transaction_id=gap.get("transaction_id"),
        amount_brl=amount,
        reference_month=reference_month,
    )
