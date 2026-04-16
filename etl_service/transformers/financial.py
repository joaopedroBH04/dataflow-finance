"""
transformers/financial.py
--------------------------
Core ETL transformation engine.

Responsibilities:
1. Merge validated DataFrames from iFood, PDV, and the acquirer.
2. Apply business rules: compute net revenue after commissions and MDR fees.
3. Detect cash-flow gaps (transactions present in one system but missing
   from another, or amounts that don't reconcile within tolerance).
4. Produce a final consolidated DRE (Demonstrativo de Resultado do Exercício).

Design notes:
- All monetary operations use Python's built-in round(..., 2) to avoid
  floating-point drift. In a future hardened version this should use
  the 'decimal' module with ROUND_HALF_UP.
- 'gap tolerance' is read from settings so it can be tuned without
  touching code (e.g. R$ 0.05 rounding tolerance on card transactions).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from etl_service.config import settings
from etl_service.validators.schemas import GapSummary, PaymentMethod


# ====================================================================== #
# DRE Summary Container
# ====================================================================== #

@dataclass
class DRESummary:
    """
    Consolidated financial result for the reference period.

    Monetary values in BRL (R$), rounded to 2 decimal places.
    """

    reference_month: str                    # "YYYY-MM"

    # Revenue
    gross_revenue_pdv: float = 0.0          # Total from PDV (physical sales)
    gross_revenue_ifood: float = 0.0        # Total from iFood (before commission)
    total_gross_revenue: float = 0.0

    # Deductions
    total_ifood_commission: float = 0.0
    total_card_fees: float = 0.0
    total_deductions: float = 0.0

    # Net
    net_revenue: float = 0.0

    # Breakdowns by payment method
    revenue_by_payment: dict[str, float] = field(default_factory=dict)

    # Gap detection
    gaps: list[GapSummary] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "reference_month":          self.reference_month,
            "gross_revenue_pdv_brl":    round(self.gross_revenue_pdv, 2),
            "gross_revenue_ifood_brl":  round(self.gross_revenue_ifood, 2),
            "total_gross_revenue_brl":  round(self.total_gross_revenue, 2),
            "total_ifood_commission_brl": round(self.total_ifood_commission, 2),
            "total_card_fees_brl":      round(self.total_card_fees, 2),
            "total_deductions_brl":     round(self.total_deductions, 2),
            "net_revenue_brl":          round(self.net_revenue, 2),
            "revenue_by_payment":       {k: round(v, 2) for k, v in self.revenue_by_payment.items()},
            "gaps_detected":            len(self.gaps),
        }


# ====================================================================== #
# Financial Transformer
# ====================================================================== #

class FinancialTransformer:
    """
    Orchestrates all transformation and reconciliation logic.

    Usage
    -----
    >>> transformer = FinancialTransformer(reference_month="2026-03")
    >>> dre, gaps = transformer.run(ifood_df, pdv_df, acquirer_df)
    """

    # Fee lookup table keyed by canonical PaymentMethod enum value.
    _FEE_MAP: dict[str, float] = {
        PaymentMethod.CREDIT.value:  settings.credit_card_fee,
        PaymentMethod.DEBIT.value:   settings.debit_card_fee,
        PaymentMethod.PIX.value:     settings.pix_fee,
        PaymentMethod.CASH.value:    settings.cash_fee,
        PaymentMethod.VOUCHER.value: settings.debit_card_fee,   # Vouchers treated as debit
        PaymentMethod.OTHER.value:   settings.credit_card_fee,  # Conservative default
    }

    def __init__(self, reference_month: str) -> None:
        """
        Parameters
        ----------
        reference_month:
            String in 'YYYY-MM' format. Used to label the DRE and filter
            cross-month records if necessary.
        """
        self.reference_month = reference_month
        logger.info(
            "[Transformer] Initialised for reference period: {period}",
            period=reference_month,
        )

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    def run(
        self,
        ifood_df: pd.DataFrame,
        pdv_df: pd.DataFrame,
        acquirer_df: pd.DataFrame,
    ) -> tuple[DRESummary, list[GapSummary]]:
        """
        Full transformation pipeline:
        1. Enrich iFood orders with computed commissions.
        2. Enrich PDV transactions with MDR fees from acquirer data.
        3. Merge all sources into a unified ledger.
        4. Apply net-revenue rules.
        5. Detect cash-flow gaps.
        6. Build the DRE summary.

        Returns
        -------
        tuple[DRESummary, list[GapSummary]]
        """
        logger.info("[Transformer] Starting transformation pipeline.")

        # Step 1 — Enrich iFood
        ifood_enriched = self._enrich_ifood(ifood_df)

        # Step 2 — Enrich PDV with MDR fees by joining acquirer data
        pdv_enriched = self._enrich_pdv_with_acquirer(pdv_df, acquirer_df)

        # Step 3 — Merge into unified ledger
        ledger = self._build_unified_ledger(ifood_enriched, pdv_enriched, acquirer_df)

        # Step 4 — Apply net revenue computation
        ledger = self._compute_net_revenue_per_row(ledger)

        # Step 5 — Gap detection
        gaps = self._detect_cash_gaps(pdv_df, acquirer_df, ifood_df)

        # Step 6 — Aggregate into DRE
        dre = self._aggregate_dre(ledger, gaps)

        logger.success(
            "[Transformer] Pipeline complete — "
            "gross={gross:.2f}, deductions={ded:.2f}, net={net:.2f}, gaps={gaps}",
            gross=dre.total_gross_revenue,
            ded=dre.total_deductions,
            net=dre.net_revenue,
            gaps=len(gaps),
        )
        return dre, gaps

    # ------------------------------------------------------------------ #
    # Step 1: Enrich iFood orders
    # ------------------------------------------------------------------ #

    def _enrich_ifood(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Adds computed columns to the iFood DataFrame:
        - ifood_commission_computed: gross_value × ifood_commission_rate
        - net_value_ifood: gross_value - commission - delivery_fee (if borne by restaurant)
        - source: label for ledger merging

        Cancelled orders are kept in the ledger with negative gross_value
        so they correctly reduce revenue totals.
        """
        if df.empty:
            logger.warning("[Transformer] iFood DataFrame is empty — skipping enrichment.")
            return df

        df = df.copy()

        # Use the commission already listed by iFood if present; otherwise compute it.
        df["ifood_commission_computed"] = df.apply(
            lambda row: (
                row["ifood_commission"]
                if pd.notna(row.get("ifood_commission")) and row.get("ifood_commission", 0) > 0
                else round(row["gross_value"] * settings.ifood_commission_rate, 2)
            ),
            axis=1,
        )

        # Cancelled orders: invert the gross_value so they reduce totals.
        df.loc[df["status"] == "cancelado", "gross_value"] = (
            -df.loc[df["status"] == "cancelado", "gross_value"]
        )
        df.loc[df["status"] == "cancelado", "ifood_commission_computed"] = 0.0

        # Net iFood value = gross minus commission (delivery fee is charged to the customer,
        # not the restaurant, so we do NOT deduct it here).
        df["net_value"] = df["gross_value"] - df["ifood_commission_computed"]
        df["source"] = "ifood"
        df["mdr_fee"] = 0.0   # MDR does not apply to iFood (settled by the platform)

        logger.debug(
            "[Transformer] iFood enriched — rows={n}, gross={g:.2f}, commission={c:.2f}",
            n=len(df),
            g=df.loc[df["gross_value"] > 0, "gross_value"].sum(),
            c=df["ifood_commission_computed"].sum(),
        )
        return df

    # ------------------------------------------------------------------ #
    # Step 2: Enrich PDV with MDR fees from acquirer data
    # ------------------------------------------------------------------ #

    def _enrich_pdv_with_acquirer(
        self,
        pdv_df: pd.DataFrame,
        acquirer_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Joins PDV transactions with acquirer settlement data on gross_value + date proximity,
        and computes the MDR fee for each transaction.

        When an acquirer match is found, the actual fee reported by Stone/Cielo is used.
        When no match is found (e.g. cash transactions), the fee is computed from the
        configured rate for that payment method.
        """
        if pdv_df.empty:
            logger.warning("[Transformer] PDV DataFrame is empty — skipping enrichment.")
            return pdv_df

        df = pdv_df.copy()
        df["source"] = "pdv"

        # Build a lookup from the acquirer DataFrame indexed by (gross_value, date).
        acquirer_fee_lookup = self._build_acquirer_fee_lookup(acquirer_df)

        def _compute_fee(row: pd.Series) -> float:
            """Looks up actual fee from acquirer; falls back to computed rate."""
            key = (round(row["gross_value"], 2), str(row["transaction_date"].date()))
            if key in acquirer_fee_lookup:
                return acquirer_fee_lookup[key]
            # Fallback: apply configured MDR rate for this payment method.
            rate = self._FEE_MAP.get(str(row.get("payment_method", "")), settings.credit_card_fee)
            return round(row["gross_value"] * rate, 2)

        df["mdr_fee"] = df.apply(_compute_fee, axis=1)
        df["ifood_commission_computed"] = 0.0   # Not applicable to PDV
        df["net_value"] = df["gross_value"] - df["mdr_fee"]

        logger.debug(
            "[Transformer] PDV enriched — rows={n}, gross={g:.2f}, mdr={mdr:.2f}",
            n=len(df),
            g=df["gross_value"].sum(),
            mdr=df["mdr_fee"].sum(),
        )
        return df

    def _build_acquirer_fee_lookup(self, acquirer_df: pd.DataFrame) -> dict[tuple, float]:
        """
        Builds a (gross_value, date_str) → acquirer_fee lookup dict
        from the acquirer DataFrame.
        """
        if acquirer_df.empty:
            return {}
        lookup: dict[tuple, float] = {}
        for _, row in acquirer_df.iterrows():
            try:
                key = (round(float(row["gross_value"]), 2), str(row["settlement_date"].date()))
                lookup[key] = float(row.get("acquirer_fee", 0) or 0)
            except (KeyError, TypeError, ValueError):
                continue
        return lookup

    # ------------------------------------------------------------------ #
    # Step 3: Build unified ledger
    # ------------------------------------------------------------------ #

    def _build_unified_ledger(
        self,
        ifood_df: pd.DataFrame,
        pdv_df: pd.DataFrame,
        acquirer_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Concatenates all enriched DataFrames into a single unified ledger.

        Columns present in all frames:
            source, gross_value, net_value, mdr_fee, ifood_commission_computed, payment_method
        """
        frames = []

        ifood_cols = ["source", "order_id", "order_date", "gross_value",
                      "net_value", "mdr_fee", "ifood_commission_computed", "payment_method", "status"]
        pdv_cols   = ["source", "transaction_id", "transaction_date", "gross_value",
                      "net_value", "mdr_fee", "ifood_commission_computed", "payment_method"]

        def _safe_select(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
            present = [c for c in cols if c in df.columns]
            return df[present].copy()

        if not ifood_df.empty:
            frames.append(_safe_select(ifood_df, ifood_cols))
        if not pdv_df.empty:
            frames.append(_safe_select(pdv_df, pdv_cols))

        if not frames:
            logger.error("[Transformer] No data in any source. Cannot build ledger.")
            return pd.DataFrame()

        ledger = pd.concat(frames, ignore_index=True)

        # Unified date column
        ledger["date"] = ledger.get("order_date", ledger.get("transaction_date"))
        if "order_date" in ledger.columns and "transaction_date" in ledger.columns:
            ledger["date"] = ledger["order_date"].combine_first(ledger["transaction_date"])

        # Unified ID column
        ledger["record_id"] = (
            ledger.get("order_id", pd.Series(dtype=str))
            .combine_first(ledger.get("transaction_id", pd.Series(dtype=str)))
        )

        # Fill numeric NaNs with 0.
        for col in ("gross_value", "net_value", "mdr_fee", "ifood_commission_computed"):
            if col in ledger.columns:
                ledger[col] = pd.to_numeric(ledger[col], errors="coerce").fillna(0.0)

        logger.info(
            "[Transformer] Unified ledger built — total rows: {n}",
            n=len(ledger),
        )
        return ledger

    # ------------------------------------------------------------------ #
    # Step 4: Net revenue per row
    # ------------------------------------------------------------------ #

    def _compute_net_revenue_per_row(self, ledger: pd.DataFrame) -> pd.DataFrame:
        """
        Computes or validates net_value for each row in the unified ledger.
        net_value = gross_value - mdr_fee - ifood_commission_computed
        """
        if ledger.empty:
            return ledger

        ledger = ledger.copy()
        ledger["net_value_final"] = (
            ledger["gross_value"]
            - ledger.get("mdr_fee", 0)
            - ledger.get("ifood_commission_computed", 0)
        ).round(2)

        return ledger

    # ------------------------------------------------------------------ #
    # Step 5: Cash gap detection
    # ------------------------------------------------------------------ #

    def _detect_cash_gaps(
        self,
        pdv_df: pd.DataFrame,
        acquirer_df: pd.DataFrame,
        ifood_df: pd.DataFrame,
    ) -> list[GapSummary]:
        """
        Identifies financial discrepancies between data sources.

        Gap types detected:
        1. PDV_MISSING_IN_ACQUIRER:
           A card transaction in PDV has no matching settlement in Stone/Cielo.
           Indicates a potential chargeback, failed auth, or integration failure.

        2. ACQUIRER_MISSING_IN_PDV:
           A settlement exists in Stone/Cielo with no corresponding PDV transaction.
           Indicates a transaction registered at the terminal but never input to the PDV.

        3. IFOOD_CANCELLED_IN_REVENUE:
           A cancelled iFood order that was (incorrectly) counted as revenue in the PDV.

        4. AMOUNT_MISMATCH:
           A transaction pair is matched by ID/date but the amounts diverge beyond tolerance.
        """
        gaps: list[GapSummary] = []
        tol = settings.gap_tolerance_brl

        logger.info("[Transformer] Running cash gap detection.")

        # ---- Gap Type 1 & 4: Card PDV transactions vs. acquirer settlements ----
        if not pdv_df.empty and not acquirer_df.empty:
            card_methods = {PaymentMethod.CREDIT.value, PaymentMethod.DEBIT.value, PaymentMethod.VOUCHER.value}

            card_pdv = pdv_df[pdv_df["payment_method"].isin(card_methods)].copy()

            for _, pdv_row in card_pdv.iterrows():
                pdv_amount = round(float(pdv_row.get("gross_value", 0)), 2)
                pdv_date   = pdv_row.get("transaction_date")

                # Look for a matching acquirer record by amount + date.
                if not acquirer_df.empty and "gross_value" in acquirer_df.columns:
                    acq_match = acquirer_df[
                        (abs(acquirer_df["gross_value"].astype(float) - pdv_amount) <= tol) &
                        (acquirer_df["settlement_date"].dt.date == pd.Timestamp(pdv_date).date()
                         if pd.notna(pdv_date) else False)
                    ]
                    if acq_match.empty:
                        gaps.append(GapSummary(
                            transaction_id=str(pdv_row.get("transaction_id", "N/A")),
                            source="PDV",
                            amount_brl=pdv_amount,
                            gap_type="PDV_MISSING_IN_ACQUIRER",
                            detail=(
                                f"Card transaction R$ {pdv_amount:.2f} on "
                                f"{pdv_date} found in PDV but not in acquirer settlement."
                            ),
                        ))
                        logger.warning(
                            "[Gap] PDV_MISSING_IN_ACQUIRER — id={id}, amount={amt:.2f}",
                            id=pdv_row.get("transaction_id", "N/A"),
                            amt=pdv_amount,
                        )
                    else:
                        # Check for amount mismatch within the match window.
                        for _, acq_row in acq_match.iterrows():
                            acq_gross = round(float(acq_row.get("gross_value", 0)), 2)
                            if abs(acq_gross - pdv_amount) > tol:
                                gaps.append(GapSummary(
                                    transaction_id=str(pdv_row.get("transaction_id", "N/A")),
                                    source="PDV vs Acquirer",
                                    amount_brl=abs(acq_gross - pdv_amount),
                                    gap_type="AMOUNT_MISMATCH",
                                    detail=(
                                        f"PDV recorded R$ {pdv_amount:.2f} but acquirer settled R$ {acq_gross:.2f}. "
                                        f"Difference: R$ {abs(acq_gross - pdv_amount):.2f}."
                                    ),
                                ))

        # ---- Gap Type 2: Acquirer settlements with no PDV counterpart ----
        if not acquirer_df.empty and not pdv_df.empty:
            for _, acq_row in acquirer_df.iterrows():
                acq_amount = round(float(acq_row.get("gross_value", 0)), 2)
                acq_date   = acq_row.get("settlement_date")

                match_in_pdv = pdv_df[
                    (abs(pdv_df["gross_value"].astype(float) - acq_amount) <= tol) &
                    (pdv_df["transaction_date"].dt.date == pd.Timestamp(acq_date).date()
                     if pd.notna(acq_date) else False)
                ]
                if match_in_pdv.empty:
                    gaps.append(GapSummary(
                        transaction_id=str(acq_row.get("authorization_code", "N/A")),
                        source="Acquirer",
                        amount_brl=acq_amount,
                        gap_type="ACQUIRER_MISSING_IN_PDV",
                        detail=(
                            f"Acquirer settled R$ {acq_amount:.2f} on {acq_date} "
                            f"(auth: {acq_row.get('authorization_code', 'N/A')}) "
                            "but no matching PDV transaction found."
                        ),
                    ))
                    logger.warning(
                        "[Gap] ACQUIRER_MISSING_IN_PDV — auth={auth}, amount={amt:.2f}",
                        auth=acq_row.get("authorization_code", "N/A"),
                        amt=acq_amount,
                    )

        # ---- Gap Type 3: iFood cancelled orders that may appear in PDV ----
        if not ifood_df.empty and "status" in ifood_df.columns:
            cancelled = ifood_df[ifood_df["status"] == "cancelado"].copy()
            for _, row in cancelled.iterrows():
                cancelled_amount = abs(round(float(row.get("gross_value", 0)), 2))
                gaps.append(GapSummary(
                    transaction_id=str(row.get("order_id", "N/A")),
                    source="iFood",
                    amount_brl=cancelled_amount,
                    gap_type="IFOOD_ORDER_CANCELLED",
                    detail=(
                        f"iFood order {row.get('order_id', 'N/A')} was cancelled "
                        f"(R$ {cancelled_amount:.2f}). Verify it is not counted as revenue."
                    ),
                ))

        logger.info(
            "[Transformer] Gap detection complete — {n} gap(s) identified.",
            n=len(gaps),
        )
        return gaps

    # ------------------------------------------------------------------ #
    # Step 6: Aggregate DRE
    # ------------------------------------------------------------------ #

    def _aggregate_dre(
        self,
        ledger: pd.DataFrame,
        gaps: list[GapSummary],
    ) -> DRESummary:
        """
        Aggregates the unified ledger into a DRESummary object.
        """
        dre = DRESummary(reference_month=self.reference_month)

        if ledger.empty:
            dre.gaps = gaps
            return dre

        # Revenue by source
        ifood_rows = ledger[ledger["source"] == "ifood"]
        pdv_rows   = ledger[ledger["source"] == "pdv"]

        dre.gross_revenue_ifood = round(
            ifood_rows["gross_value"].clip(lower=0).sum(), 2
        )
        dre.gross_revenue_pdv = round(
            pdv_rows["gross_value"].clip(lower=0).sum(), 2
        )
        dre.total_gross_revenue = round(
            dre.gross_revenue_ifood + dre.gross_revenue_pdv, 2
        )

        # Deductions
        dre.total_ifood_commission = round(
            ledger.get("ifood_commission_computed", pd.Series([0])).sum(), 2
        )
        dre.total_card_fees = round(
            ledger.get("mdr_fee", pd.Series([0])).sum(), 2
        )
        dre.total_deductions = round(
            dre.total_ifood_commission + dre.total_card_fees, 2
        )

        # Net revenue
        dre.net_revenue = round(dre.total_gross_revenue - dre.total_deductions, 2)

        # Revenue breakdown by payment method
        if "payment_method" in ledger.columns and "gross_value" in ledger.columns:
            breakdown = (
                ledger[ledger["gross_value"] > 0]
                .groupby("payment_method")["gross_value"]
                .sum()
                .round(2)
                .to_dict()
            )
            dre.revenue_by_payment = breakdown

        dre.gaps = gaps

        logger.info(
            "[DRE] Gross={gross:.2f} | iFood Commission={ifc:.2f} | "
            "Card Fees={fees:.2f} | Net={net:.2f}",
            gross=dre.total_gross_revenue,
            ifc=dre.total_ifood_commission,
            fees=dre.total_card_fees,
            net=dre.net_revenue,
        )
        return dre
