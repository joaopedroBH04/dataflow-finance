"""
extractors/pdv.py
-----------------
Concrete extractor for local POS (PDV) system exports.

PDV systems vary widely; this implementation targets the most common
export format: a CSV or Excel file with Portuguese column headers.
Column mapping is configurable at instantiation time to handle
different PDV vendors without subclassing.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from loguru import logger

from etl_service.extractors.base import BaseExtractor, ExtractionResult
from etl_service.validators.schemas import PDVTransactionSchema


class PDVExtractor(BaseExtractor[PDVTransactionSchema]):
    """
    Extractor for local POS (PDV) system exports.

    Supports CSV and XLSX input formats. Column mapping can be
    customised at instantiation for different PDV vendors.

    Parameters
    ----------
    column_map:
        Optional override for the default column name mapping.
        Keys are raw column names (lowercase), values are schema field names.

    Usage
    -----
    >>> extractor = PDVExtractor()
    >>> result = extractor.extract("/path/to/pdv_export.csv")

    # For a PDV vendor with non-standard headers:
    >>> extractor = PDVExtractor(column_map={"num_trans": "transaction_id", ...})
    """

    source_name: str = "PDV"
    schema_class = PDVTransactionSchema

    # Default column mapping covers the most common PDV export headers.
    _DEFAULT_COLUMN_MAP: dict[str, str] = {
        "id transacao":        "transaction_id",
        "id da transacao":     "transaction_id",
        "numero transacao":    "transaction_id",
        "transacao":           "transaction_id",
        "cod. transacao":      "transaction_id",
        "data":                "transaction_date",
        "data transacao":      "transaction_date",
        "data/hora":           "transaction_date",
        "data e hora":         "transaction_date",
        "valor":               "gross_value",
        "valor total":         "gross_value",
        "valor bruto":         "gross_value",
        "forma pagamento":     "payment_method",
        "forma de pagamento":  "payment_method",
        "tipo pagamento":      "payment_method",
        "operador":            "cashier_id",
        "caixa":               "cashier_id",
        "id caixa":            "cashier_id",
        "mesa":                "table_number",
        "numero mesa":         "table_number",
        "observacao":          "notes",
        "obs":                 "notes",
        "observações":         "notes",
    }

    def __init__(self, column_map: dict[str, str] | None = None) -> None:
        if column_map:
            self._COLUMN_MAP = {**self._DEFAULT_COLUMN_MAP, **column_map}
        else:
            self._COLUMN_MAP = self._DEFAULT_COLUMN_MAP

    def _read_raw(self, source: str) -> pd.DataFrame:
        """
        Reads a CSV or XLSX file from the PDV system.

        Raises
        ------
        FileNotFoundError:
            If the file does not exist.
        ValueError:
            If the file extension is not supported.
        """
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"[PDV] File not found: '{source}'")

        suffix = path.suffix.lower()

        if suffix == ".csv":
            df = self._read_csv(source)
        elif suffix in (".xlsx", ".xls"):
            df = self._read_excel(source)
        else:
            raise ValueError(
                f"[PDV] Unsupported file format '{suffix}'. Expected .csv, .xlsx or .xls."
            )

        logger.debug("[PDV] Raw file read — rows={n}, cols={cols}", n=len(df), cols=list(df.columns))
        return df

    def _read_csv(self, source: str) -> pd.DataFrame:
        """Attempts to read the CSV with semicolon then comma as delimiter."""
        for sep in (";", ",", "\t"):
            try:
                df = pd.read_csv(
                    source,
                    sep=sep,
                    encoding="utf-8-sig",
                    dtype=str,
                    skip_blank_lines=True,
                )
                if df.shape[1] > 1:
                    return df
            except Exception:
                continue
        raise ValueError(f"[PDV] Could not parse '{source}' as CSV.")

    def _read_excel(self, source: str) -> pd.DataFrame:
        """Reads the first sheet of an Excel file."""
        try:
            return pd.read_excel(source, dtype=str, sheet_name=0)
        except Exception as exc:
            raise ValueError(f"[PDV] Could not read Excel file '{source}': {exc}") from exc

    def _normalise_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Maps PDV column headers to schema field names and cleans the data.
        """
        df.columns = [col.strip().lower() for col in df.columns]

        rename_map = {
            raw: canonical
            for raw, canonical in self._COLUMN_MAP.items()
            if raw in df.columns
        }
        df = df.rename(columns=rename_map)

        # Drop entirely blank rows (common in Excel PDV exports).
        df = df.dropna(how="all").reset_index(drop=True)

        # Strip whitespace from string columns.
        str_cols = df.select_dtypes(include="object").columns
        df[str_cols] = df[str_cols].apply(lambda col: col.str.strip())

        # Keep only expected schema fields.
        expected_fields = set(PDVTransactionSchema.model_fields.keys())
        present_expected = [c for c in df.columns if c in expected_fields]

        if "transaction_id" not in present_expected:
            logger.warning(
                "[PDV] 'transaction_id' column not found after normalisation. "
                "Available columns: {cols}. Gap detection may be inaccurate.",
                cols=list(df.columns),
            )

        df = df[present_expected]
        return df
