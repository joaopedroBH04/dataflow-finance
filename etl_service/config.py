"""
config.py
---------
Centralizes all environment-driven configuration via pydantic-settings.
Reads from environment variables or a .env file at project root.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application-wide settings.

    All values can be overridden via environment variables
    (prefixed with DATAFLOW_) or a .env file.
    """

    model_config = SettingsConfigDict(
        env_prefix="DATAFLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ #
    # API / Server
    # ------------------------------------------------------------------ #
    app_name: str = Field(default="DataFlow Finance ETL", description="Application display name.")
    api_version: str = Field(default="v1", description="API version prefix.")
    debug: bool = Field(default=False, description="Enable debug mode (never True in production).")
    log_level: str = Field(default="INFO", description="Loguru log level (DEBUG, INFO, WARNING, ERROR).")

    # ------------------------------------------------------------------ #
    # Business Rules — iFood
    # ------------------------------------------------------------------ #
    ifood_commission_rate: float = Field(
        default=0.23,
        ge=0.0,
        le=1.0,
        description="iFood platform commission as a decimal (e.g. 0.23 = 23%).",
    )

    # ------------------------------------------------------------------ #
    # Business Rules — Acquirers (Stone / Cielo)
    # ------------------------------------------------------------------ #
    credit_card_fee: float = Field(
        default=0.0299,
        ge=0.0,
        le=1.0,
        description="Credit card MDR as a decimal (e.g. 0.0299 = 2.99%).",
    )
    debit_card_fee: float = Field(
        default=0.0149,
        ge=0.0,
        le=1.0,
        description="Debit card MDR as a decimal (e.g. 0.0149 = 1.49%).",
    )
    pix_fee: float = Field(
        default=0.0099,
        ge=0.0,
        le=1.0,
        description="PIX transaction fee as a decimal (e.g. 0.0099 = 0.99%).",
    )
    cash_fee: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Cash transaction fee (zero by default).",
    )

    # ------------------------------------------------------------------ #
    # Cash Gap Detection
    # ------------------------------------------------------------------ #
    gap_tolerance_brl: float = Field(
        default=0.05,
        ge=0.0,
        description="Monetary tolerance (in BRL) for considering two amounts equal during reconciliation.",
    )

    # ------------------------------------------------------------------ #
    # Output / Storage
    # ------------------------------------------------------------------ #
    output_dir: str = Field(
        default="./output",
        description="Local directory where ETL output files (DRE, gap report) will be saved.",
    )

    # ------------------------------------------------------------------ #
    # CORS
    # ------------------------------------------------------------------ #
    allowed_origins: list[str] = Field(
        default=["*"],
        description=(
            "List of allowed CORS origins (JSON array format). "
            "Default '*' is fine for local dev; restrict to specific domains in production. "
            "Example: [\"https://dataflowfinance.com.br\"]"
        ),
    )

    # ------------------------------------------------------------------ #
    # Security
    # ------------------------------------------------------------------ #
    leads_api_key: str = Field(
        default="",
        description=(
            "API key required to read GET /leads (protects lead PII). "
            "Leave empty to disable auth in development. Always set in production."
        ),
    )


# Singleton instance — import this throughout the codebase.
settings = Settings()
