"""
extractors/stone_cielo.py
-------------------------
Concrete extractor for Stone and Cielo acquirer settlement reports.

Both acquirers offer CSV exports from their portals. Stone uses a
semicolon-delimited format; Cielo uses commas. The extractor auto-detects
the format and maps the headers to the canonical AcquirerTransactionSchema.
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from etl_service.extractors.base import BaseExtractor
from etl_service.validators.schemas import AcquirerTransactionSchema


class AcquirerExtractor(BaseExtractor[AcquirerTransactionSchema]):
    """
    Extractor for Stone / Cielo financial settlement reports.

    Handles differences between Stone and Cielo export formats transparently.
    The acquirer_name parameter is used only for logging and DRE labels.

    Parameters
    ----------
    acquirer_name:
        'stone' or 'cielo' — used for logging and report labelling.

    Usage
    -----
    >>> extractor = AcquirerExtractor(acquirer_name="stone")
    >>> result = extractor.extract("/path/to/stone_settlement.csv")
    """

    schema_class = AcquirerTransactionSchema

    # Stone column map (semicolon-delimited)
    _STONE_COLUMN_MAP: dict[str, str] = {
        "codigo de autorizacao":    "authorization_code",
        "código de autorização":    "authorization_code",
        "nsu":                      "nsu",
        "data de liquidacao":       "settlement_date",
        "data liquidação":          "settlement_date",
        "data liquidacao":          "settlement_date",
        "valor bruto":              "gross_value",
        "valor liquido":            "net_value",
        "valor líquido":            "net_value",
        "taxa":                     "acquirer_fee",
        "taxa mdr":                 "acquirer_fee",
        "tipo de pagamento":        "payment_method",
        "forma de pagamento":       "payment_method",
        "parcelas":                 "installments",
        "numero de parcelas":       "installments",
    }

    # Cielo column map (comma-delimited, slightly different labels)
    _CIELO_COLUMN_MAP: dict[str, str] = {
        "codigo autorizacao":       "authorization_code",
        "codigo de autorizacao":    "authorization_code",
        "nsu":                      "nsu",
        "data pagamento":           "settlement_date",
        "data de pagamento":        "settlement_date",
        "valor bruto":              "gross_value",
        "valor liquido":            "net_value",
        "taxa adquirente":          "acquirer_fee",
        "produto":                  "payment_method",
        "tipo transacao":           "payment_method",
        "qtde parcelas":            "installments",
    }

    def __init__(self, acquirer_name: str = "stone") -> None:
        self.acquirer_name = acquirer_name.lower().strip()
        self.source_name = f"Acquirer ({self.acquirer_name.capitalize()})"
        self._COLUMN_MAP = (
            self._STONE_COLUMN_MAP if self.acquirer_name == "stone" else self._CIELO_COLUMN_MAP
        )

    def _read_raw(self, source: str) -> pd.DataFrame:
        """
        Reads the acquirer CSV, trying semicolon then comma as delimiter.
        """
        logger.debug("[{acq}] Reading file: {path}", acq=self.source_name, path=source)

        for sep in (";", ","):
            try:
                df = pd.read_csv(
                    source,
                    sep=sep,
                    encoding="utf-8-sig",
                    dtype=str,
                    skip_blank_lines=True,
                )
                if df.shape[1] > 1:
                    logger.debug(
                        "[{acq}] Parsed with sep='{sep}', shape={shape}",
                        acq=self.source_name, sep=sep, shape=df.shape,
                    )
                    return df
            except Exception as exc:
                logger.debug(
                    "[{acq}] Attempt with sep='{sep}' failed: {exc}",
                    acq=self.source_name, sep=sep, exc=str(exc),
                )

        raise ValueError(
            f"[{self.source_name}] Could not parse '{source}' as CSV. "
            "Download the financial settlement report from your acquirer portal."
        )

    def _normalise_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Maps acquirer headers to canonical schema fields.
        Also injects the acquirer_name into every row so the downstream
        transformer can label the records correctly.
        """
        df.columns = [col.strip().lower() for col in df.columns]

        rename_map = {
            raw: canonical
            for raw, canonical in self._COLUMN_MAP.items()
            if raw in df.columns
        }
        df = df.rename(columns=rename_map)

        # Always inject the acquirer identity so it survives into the merged DataFrame.
        df["acquirer_name"] = self.acquirer_name

        # Drop blank rows.
        df = df.dropna(how="all").reset_index(drop=True)

        # Strip string columns.
        str_cols = df.select_dtypes(include="object").columns
        df[str_cols] = df[str_cols].apply(lambda col: col.str.strip())

        # Keep only schema-expected fields.
        expected_fields = set(AcquirerTransactionSchema.model_fields.keys())
        present_expected = [c for c in df.columns if c in expected_fields]
        df = df[present_expected]

        logger.debug(
            "[{acq}] Normalised columns: {cols}",
            acq=self.source_name, cols=list(df.columns),
        )
        return df
