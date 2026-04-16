"""
validators/schemas.py
---------------------
Pydantic v2 schemas for each data source.

Design decision: we validate *row by row* (not with Pandera DataFrame schemas)
so we get granular per-record error messages — including exact row index,
field name, and received value. Invalid rows are quarantined rather than
crashing the entire pipeline.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ====================================================================== #
# Shared Enums
# ====================================================================== #

class PaymentMethod(str, Enum):
    CREDIT  = "credito"
    DEBIT   = "debito"
    PIX     = "pix"
    CASH    = "dinheiro"
    VOUCHER = "voucher"
    OTHER   = "outro"


class OrderStatus(str, Enum):
    CONCLUDED = "concluido"
    CANCELLED = "cancelado"
    PENDING   = "pendente"


# ====================================================================== #
# iFood Order Schema
# ====================================================================== #

class IfoodOrderSchema(BaseModel):
    """
    Represents a single row from an iFood financial export (CSV/API).

    Required fields are enforced strictly. Optional fields accept None
    gracefully so that the row is not automatically quarantined when
    iFood omits non-critical columns.
    """

    order_id: str = Field(..., min_length=1, description="Unique iFood order identifier.")
    order_date: datetime = Field(..., description="Date and time the order was placed (UTC or local).")
    gross_value: float = Field(..., description="Total order value before any deductions (BRL).")
    ifood_commission: Optional[float] = Field(
        default=None,
        description="Commission amount already listed by iFood; if None, it will be computed from rate.",
    )
    payment_method: PaymentMethod = Field(..., description="Payment method used by the customer.")
    status: OrderStatus = Field(..., description="Final order status.")
    customer_id: Optional[str] = Field(default=None, description="Anonymised customer identifier.")
    delivery_fee: Optional[float] = Field(default=0.0, description="Delivery fee charged to the customer.")

    @field_validator("gross_value", mode="before")
    @classmethod
    def parse_and_validate_gross_value(cls, v: object) -> float:
        """Coerces string/comma-decimal values and rejects negatives."""
        if isinstance(v, str):
            v = v.replace("R$", "").replace(".", "").replace(",", ".").strip()
        try:
            value = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"gross_value must be numeric; received '{v}' ({type(v).__name__})."
            ) from exc
        if value < 0:
            raise ValueError(f"gross_value must be ≥ 0; received {value}.")
        return round(value, 2)

    @field_validator("delivery_fee", "ifood_commission", mode="before")
    @classmethod
    def parse_optional_float(cls, v: object) -> Optional[float]:
        """Tolerantly parses optional monetary fields."""
        if v is None or (isinstance(v, str) and v.strip() in ("", "-", "N/A")):
            return None
        if isinstance(v, str):
            v = v.replace("R$", "").replace(".", "").replace(",", ".").strip()
        try:
            return round(float(v), 2)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Expected a numeric value; received '{v}'.") from exc

    @field_validator("order_date", mode="before")
    @classmethod
    def parse_order_date(cls, v: object) -> datetime:
        """Parses multiple common date formats exported by iFood."""
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(v.strip(), fmt)
                except ValueError:
                    continue
        raise ValueError(f"Cannot parse '{v}' as a datetime. Expected formats: YYYY-MM-DD HH:MM:SS or DD/MM/YYYY.")

    @field_validator("payment_method", mode="before")
    @classmethod
    def normalise_payment_method(cls, v: object) -> str:
        """Maps iFood's raw payment strings to our canonical enum values."""
        mapping = {
            "cartão de crédito": PaymentMethod.CREDIT,
            "credito":            PaymentMethod.CREDIT,
            "crédito":            PaymentMethod.CREDIT,
            "cartão de débito":  PaymentMethod.DEBIT,
            "debito":             PaymentMethod.DEBIT,
            "débito":             PaymentMethod.DEBIT,
            "pix":                PaymentMethod.PIX,
            "dinheiro":           PaymentMethod.CASH,
            "voucher":            PaymentMethod.VOUCHER,
        }
        if isinstance(v, str):
            normalised = mapping.get(v.lower().strip())
            if normalised:
                return normalised.value
        return str(v)

    @field_validator("status", mode="before")
    @classmethod
    def normalise_status(cls, v: object) -> str:
        mapping = {
            "concluído": OrderStatus.CONCLUDED,
            "concluido": OrderStatus.CONCLUDED,
            "cancelado":  OrderStatus.CANCELLED,
            "pendente":   OrderStatus.PENDING,
        }
        if isinstance(v, str):
            mapped = mapping.get(v.lower().strip())
            if mapped:
                return mapped.value
        return str(v)


# ====================================================================== #
# PDV Transaction Schema
# ====================================================================== #

class PDVTransactionSchema(BaseModel):
    """
    Represents a single POS (PDV) transaction.

    PDV systems vary significantly. This schema covers the minimum
    required columns expected after a standardised export.
    """

    transaction_id: str = Field(..., min_length=1)
    transaction_date: datetime
    gross_value: float
    payment_method: PaymentMethod
    cashier_id: Optional[str] = Field(default=None)
    table_number: Optional[str] = Field(default=None)
    notes: Optional[str] = Field(default=None)

    @field_validator("gross_value", mode="before")
    @classmethod
    def parse_gross_value(cls, v: object) -> float:
        if isinstance(v, str):
            v = v.replace("R$", "").replace(".", "").replace(",", ".").strip()
        try:
            value = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"PDV gross_value must be numeric; received '{v}'.") from exc
        if value < 0:
            raise ValueError(f"PDV gross_value must be ≥ 0; received {value}.")
        return round(value, 2)

    @field_validator("transaction_date", mode="before")
    @classmethod
    def parse_date(cls, v: object) -> datetime:
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(v.strip(), fmt)
                except ValueError:
                    continue
        raise ValueError(f"Cannot parse '{v}' as a datetime.")

    @field_validator("payment_method", mode="before")
    @classmethod
    def normalise_payment_method(cls, v: object) -> str:
        mapping = {
            "crédito": PaymentMethod.CREDIT,   "credito":  PaymentMethod.CREDIT,
            "débito":  PaymentMethod.DEBIT,    "debito":   PaymentMethod.DEBIT,
            "pix":     PaymentMethod.PIX,
            "dinheiro":PaymentMethod.CASH,     "especie":  PaymentMethod.CASH,
            "voucher": PaymentMethod.VOUCHER,
        }
        if isinstance(v, str):
            mapped = mapping.get(v.lower().strip())
            if mapped:
                return mapped.value
        return str(v)


# ====================================================================== #
# Stone / Cielo Acquirer Schema
# ====================================================================== #

class AcquirerTransactionSchema(BaseModel):
    """
    Represents a single acquirer (Stone/Cielo) transaction from their
    financial settlement report.
    """

    authorization_code: str = Field(..., min_length=1, description="Unique auth code from the acquirer.")
    settlement_date: datetime = Field(..., description="Date the transaction settled to the merchant.")
    gross_value: float = Field(..., description="Charged amount (BRL).")
    net_value: float = Field(..., description="Amount after acquirer fees (BRL).")
    acquirer_fee: float = Field(default=0.0, description="Fee charged by the acquirer (BRL).")
    payment_method: PaymentMethod
    installments: int = Field(default=1, ge=1, le=24)
    acquirer_name: str = Field(default="stone", description="'stone' or 'cielo'.")
    nsu: Optional[str] = Field(default=None, description="NSU (Número Sequencial Único) for cross-referencing.")

    @field_validator("gross_value", "net_value", "acquirer_fee", mode="before")
    @classmethod
    def parse_monetary(cls, v: object) -> float:
        if isinstance(v, str):
            v = v.replace("R$", "").replace(".", "").replace(",", ".").strip()
        try:
            return round(float(v), 2)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Expected numeric monetary value; received '{v}'.") from exc

    @field_validator("settlement_date", mode="before")
    @classmethod
    def parse_date(cls, v: object) -> datetime:
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
                try:
                    return datetime.strptime(v.strip(), fmt)
                except ValueError:
                    continue
        raise ValueError(f"Cannot parse '{v}' as a datetime.")

    @model_validator(mode="after")
    def net_cannot_exceed_gross(self) -> "AcquirerTransactionSchema":
        if self.net_value > self.gross_value + 0.01:
            raise ValueError(
                f"net_value ({self.net_value}) cannot exceed gross_value ({self.gross_value})."
            )
        return self

    @field_validator("payment_method", mode="before")
    @classmethod
    def normalise_payment_method(cls, v: object) -> str:
        mapping = {
            "credit":  PaymentMethod.CREDIT,   "credito": PaymentMethod.CREDIT,
            "debit":   PaymentMethod.DEBIT,    "debito":  PaymentMethod.DEBIT,
            "pix":     PaymentMethod.PIX,
            "voucher": PaymentMethod.VOUCHER,
        }
        if isinstance(v, str):
            mapped = mapping.get(v.lower().strip())
            if mapped:
                return mapped.value
        return str(v)


# ====================================================================== #
# FastAPI Request / Response Payloads
# ====================================================================== #

class ETLRequest(BaseModel):
    """Payload accepted by POST /api/v1/run-etl."""

    ifood_file_path: str = Field(..., description="Absolute or relative path to the iFood CSV export.")
    pdv_file_path: str = Field(..., description="Absolute or relative path to the PDV CSV export.")
    acquirer_file_path: str = Field(..., description="Absolute or relative path to the Stone/Cielo CSV export.")
    reference_month: str = Field(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="Reference period in YYYY-MM format (e.g. '2026-03').",
    )
    acquirer_name: str = Field(default="stone", description="Acquirer identifier for logging.")


class GapSummary(BaseModel):
    """Represents a single detected cash-flow gap."""

    transaction_id: str
    source: str
    amount_brl: float
    gap_type: str
    detail: str


class ETLResponse(BaseModel):
    """Response returned by POST /api/v1/run-etl."""

    status: str
    reference_month: str
    total_ifood_orders: int
    total_pdv_transactions: int
    total_acquirer_transactions: int
    quarantined_rows: int
    gross_revenue_brl: float
    total_fees_brl: float
    net_revenue_brl: float
    gaps_detected: int
    gap_details: list[GapSummary]
    output_file_path: str
    execution_time_seconds: float
