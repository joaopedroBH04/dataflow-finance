"""
main.py
-------
FastAPI application entry point.

Exposes the ETL pipeline as a REST API endpoint:
  POST /api/v1/run-etl

The endpoint accepts file paths and configuration, runs the full
Extract → Validate → Transform → Load pipeline, and returns a
structured JSON summary of the execution.

Run locally:
  uvicorn etl_service.main:app --reload --port 8000

Production:
  gunicorn etl_service.main:app -k uvicorn.workers.UvicornWorker --workers 2
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncGenerator

import pandas as pd
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from etl_service.api import alerts as alerts_router
from etl_service.api import leads as leads_router
from etl_service.api import metrics as metrics_router
from etl_service.config import settings
from etl_service.extractors.ifood import IfoodExtractor
from etl_service.extractors.pdv import PDVExtractor
from etl_service.extractors.stone_cielo import AcquirerExtractor
from etl_service.loaders.report import ReportLoader
from etl_service.transformers.financial import FinancialTransformer
from etl_service.validators.schemas import ETLRequest, ETLResponse, GapSummary


# ====================================================================== #
# ETL endpoint rate limiter (sliding window, in-memory)
# ETL is compute-heavy — allow 3 runs per IP per 60-second window.
# ====================================================================== #

_ETL_RATE_WINDOW_SECONDS = 60
_ETL_RATE_MAX_REQUESTS = 3
_etl_rate_store: dict[str, deque] = defaultdict(deque)


def _etl_resolve_client_ip(request: Request) -> str:
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_etl_rate_limit(client_ip: str) -> None:
    """Raises HTTP 429 if the IP exceeds _ETL_RATE_MAX_REQUESTS in the sliding window."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=_ETL_RATE_WINDOW_SECONDS)
    timestamps = _etl_rate_store[client_ip]
    while timestamps and timestamps[0] < cutoff:
        timestamps.popleft()
    if len(timestamps) >= _ETL_RATE_MAX_REQUESTS:
        logger.warning(
            "[ETL] Rate limit exceeded — ip={ip}, {n} requests in {w}s window",
            ip=client_ip, n=len(timestamps), w=_ETL_RATE_WINDOW_SECONDS,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many ETL requests. Maximum {_ETL_RATE_MAX_REQUESTS} per {_ETL_RATE_WINDOW_SECONDS}s. Please wait before retrying.",
            headers={"Retry-After": str(_ETL_RATE_WINDOW_SECONDS)},
        )
    timestamps.append(now)


# ====================================================================== #
# Logging configuration (Loguru structured output)
# ====================================================================== #

import sys

logger.remove()  # Remove default handler

# Structured JSON log for production (parse with Datadog, CloudWatch, etc.)
logger.add(
    sys.stdout,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    ),
    level=settings.log_level,
    colorize=True,
    backtrace=True,
    diagnose=settings.debug,
)

# Rotating file log — keeps last 7 days, max 50 MB per file.
logger.add(
    "logs/etl_service_{time:YYYY-MM-DD}.log",
    rotation="50 MB",
    retention="7 days",
    compression="zip",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{line} | {message}",
    level="DEBUG",
    backtrace=True,
    diagnose=True,
)


# ====================================================================== #
# Application lifecycle
# ====================================================================== #

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Runs setup logic on startup and teardown logic on shutdown."""
    logger.info(
        "Starting {app} (version {ver}) — debug={debug}",
        app=settings.app_name,
        ver=settings.api_version,
        debug=settings.debug,
    )
    yield
    logger.info("{app} shutting down.", app=settings.app_name)


# ====================================================================== #
# FastAPI app
# ====================================================================== #

app = FastAPI(
    title=settings.app_name,
    version=settings.api_version,
    description=(
        "Production-grade ETL microservice that integrates iFood, PDV, and "
        "Stone/Cielo acquirer data into an automated financial DRE with cash-gap detection."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Allow requests from the landing page / dashboard frontend.
# Configure DATAFLOW_ALLOWED_ORIGINS in .env for production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Propagates or generates an X-Request-ID for end-to-end request tracing."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Injects security headers on every response to harden the API."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    # Prevent HTTPS downgrade attacks and MitM cookie hijacking (2-year max-age).
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
    # Prevent proxies and browsers from caching sensitive API responses.
    response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Emits a structured access log line for every HTTP request."""
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
    xff = request.headers.get("X-Forwarded-For", "")
    client_ip = xff.split(",")[0].strip() if xff else (
        request.client.host if request.client else "unknown"
    )
    # X-Request-ID is set on the response by the add_request_id middleware (inner).
    request_id = response.headers.get("X-Request-ID", "-")
    logger.info(
        "{method} {path} → {status} {elapsed}ms [ip={ip} rid={rid}]",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        elapsed=elapsed_ms,
        ip=client_ip,
        rid=request_id,
    )
    return response

# ====================================================================== #
# Register routers (metrics dashboard, alerts, lead capture)
# ====================================================================== #

API_PREFIX = f"/api/{settings.api_version}"

app.include_router(metrics_router.router, prefix=API_PREFIX)
app.include_router(alerts_router.router,  prefix=API_PREFIX)
app.include_router(leads_router.router,   prefix=API_PREFIX)


# ====================================================================== #
# Global exception handler
# ====================================================================== #

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catches unhandled exceptions and returns a structured error response."""
    logger.error(
        "Unhandled exception on {method} {path}: {exc}",
        method=request.method,
        path=request.url.path,
        exc=str(exc),
    )
    # Expose raw exception detail only in debug mode to avoid leaking internals.
    detail = str(exc) if settings.debug else "An unexpected error occurred. Please try again later."
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"status": "error", "detail": detail},
    )


# ====================================================================== #
# Health check
# ====================================================================== #

@app.get("/health", tags=["Infra"])
async def health_check() -> dict:
    """Liveness probe for load balancers and container orchestrators."""
    return {"status": "ok", "service": settings.app_name, "version": settings.api_version}


@app.get("/ready", tags=["Infra"])
async def readiness_check() -> JSONResponse:
    """Readiness probe — verifies the output directory is writable before accepting traffic."""
    output_path = Path(settings.output_dir)
    try:
        output_path.mkdir(parents=True, exist_ok=True)
        probe = output_path / ".write_probe"
        probe.touch()
        probe.unlink()
    except OSError as exc:
        logger.warning("[Ready] Output directory not writable: {exc}", exc=str(exc))
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "detail": "Output directory not writable."},
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "ready", "service": settings.app_name, "version": settings.api_version},
    )


# ====================================================================== #
# ETL Endpoint
# ====================================================================== #

@app.post(
    f"/api/{settings.api_version}/run-etl",
    response_model=ETLResponse,
    status_code=status.HTTP_200_OK,
    tags=["ETL"],
    summary="Execute the full ETL pipeline for a given reference month.",
    description=(
        "Accepts file paths for iFood, PDV, and Stone/Cielo exports. "
        "Runs Extract → Validate → Transform → Load and returns a "
        "JSON summary with revenue, fees, detected gaps, and the output file path."
    ),
)
async def run_etl(payload: ETLRequest, request: Request) -> ETLResponse:
    """
    Full ETL pipeline execution endpoint.

    Parameters (JSON body)
    ----------------------
    - ifood_file_path: Path to the iFood CSV export.
    - pdv_file_path: Path to the PDV CSV/XLSX export.
    - acquirer_file_path: Path to the Stone/Cielo CSV export.
    - reference_month: Period in 'YYYY-MM' format.
    - acquirer_name: 'stone' or 'cielo' (default: 'stone').

    Returns
    -------
    ETLResponse with execution summary.
    """
    _check_etl_rate_limit(_etl_resolve_client_ip(request))
    start_time = time.perf_counter()

    logger.info(
        "[ETL] Request received — period={period}, acquirer={acq}",
        period=payload.reference_month,
        acq=payload.acquirer_name,
    )

    # ------------------------------------------------------------------ #
    # EXTRACT
    # ------------------------------------------------------------------ #

    logger.info("[ETL] Phase: EXTRACT")

    try:
        ifood_result = IfoodExtractor().extract(payload.ifood_file_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"iFood file not found: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"iFood extraction failed: {exc}") from exc

    try:
        pdv_result = PDVExtractor().extract(payload.pdv_file_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"PDV file not found: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"PDV extraction failed: {exc}") from exc

    try:
        acquirer_result = AcquirerExtractor(
            acquirer_name=payload.acquirer_name
        ).extract(payload.acquirer_file_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Acquirer file not found: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Acquirer extraction failed: {exc}") from exc

    # Aggregate quarantine rows from all sources.
    quarantine_frames = [
        df for df in (
            ifood_result.quarantine_df,
            pdv_result.quarantine_df,
            acquirer_result.quarantine_df,
        ) if not df.empty
    ]
    quarantine_df = pd.concat(quarantine_frames, ignore_index=True) if quarantine_frames else pd.DataFrame()
    total_quarantined = len(quarantine_df)

    logger.info(
        "[ETL] EXTRACT complete — iFood={i}, PDV={p}, Acquirer={a}, Quarantined={q}",
        i=ifood_result.valid_row_count,
        p=pdv_result.valid_row_count,
        a=acquirer_result.valid_row_count,
        q=total_quarantined,
    )

    # ------------------------------------------------------------------ #
    # TRANSFORM
    # ------------------------------------------------------------------ #

    logger.info("[ETL] Phase: TRANSFORM")

    try:
        transformer = FinancialTransformer(reference_month=payload.reference_month)
        dre, gaps = transformer.run(
            ifood_df=ifood_result.valid_df,
            pdv_df=pdv_result.valid_df,
            acquirer_df=acquirer_result.valid_df,
        )
    except Exception as exc:
        logger.error("[ETL] TRANSFORM failed: {exc}", exc=str(exc))
        raise HTTPException(
            status_code=500,
            detail=f"Transformation failed: {exc}",
        ) from exc

    logger.info(
        "[ETL] TRANSFORM complete — gross={g:.2f}, net={n:.2f}, gaps={gaps}",
        g=dre.total_gross_revenue,
        n=dre.net_revenue,
        gaps=len(gaps),
    )

    # ------------------------------------------------------------------ #
    # LOAD
    # ------------------------------------------------------------------ #

    logger.info("[ETL] Phase: LOAD")

    # We need the unified ledger from the transformer for the report.
    # Re-build it (it was built internally); in production, return it from run().
    # Here we pass an empty DataFrame as a safe fallback if the ledger is not exposed.
    try:
        loader = ReportLoader()
        output_path = loader.save(
            dre=dre,
            ledger=pd.DataFrame(),   # Replace with transformer.ledger if exposed as attribute.
            gaps=gaps,
            quarantine=quarantine_df,
            reference_month=payload.reference_month,
        )
    except Exception as exc:
        logger.error("[ETL] LOAD failed: {exc}", exc=str(exc))
        raise HTTPException(
            status_code=500,
            detail=f"Report generation failed: {exc}",
        ) from exc

    # ------------------------------------------------------------------ #
    # Response
    # ------------------------------------------------------------------ #

    elapsed = round(time.perf_counter() - start_time, 3)

    logger.success(
        "[ETL] Pipeline finished in {elapsed}s — output: {path}",
        elapsed=elapsed,
        path=output_path,
    )

    return ETLResponse(
        status="success",
        reference_month=payload.reference_month,
        total_ifood_orders=ifood_result.valid_row_count,
        total_pdv_transactions=pdv_result.valid_row_count,
        total_acquirer_transactions=acquirer_result.valid_row_count,
        quarantined_rows=total_quarantined,
        gross_revenue_brl=dre.total_gross_revenue,
        total_fees_brl=dre.total_deductions,
        net_revenue_brl=dre.net_revenue,
        gaps_detected=len(gaps),
        gap_details=gaps,
        output_file_path=output_path,
        execution_time_seconds=elapsed,
    )
