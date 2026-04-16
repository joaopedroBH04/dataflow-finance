"""
extractors/ifood.py
-------------------
Concrete extractor for iFood financial export files (CSV format).

iFood exports a semicolon-delimited CSV from its partner portal with
Portuguese column headers. This class handles the normalisation from
iFood's raw column names to the canonical schema fields.
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from etl_service.extractors.base import BaseExtractor, ExtractionResult
from etl_service.validators.schemas import IfoodOrderSchema


class IfoodExtractor(BaseExtractor[IfoodOrderSchema]):
    """
    Extractor for iFood partner portal financial exports.

    Expected CSV format (semicolon-delimited, UTF-8 with BOM):
        Código do Pedido;Data do Pedido;Valor Total;Comissão iFood;
        Forma de Pagamento;Status;ID Cliente;Taxa de Entrega

    Usage
    -----
    >>> extractor = IfoodExtractor()
    >>> result = extractor.extract("/path/to/ifood_export.csv")
    >>> result.valid_df.head()
    """

    source_name: str = "iFood"
    schema_class = IfoodOrderSchema

    # ------------------------------------------------------------------ #
    # Column mapping: iFood raw → schema field names
    # ------------------------------------------------------------------ #
    _COLUMN_MAP: dict[str, str] = {
        "código do pedido":   "order_id",
        "codigo do pedido":   "order_id",
        "id do pedido":       "order_id",
        "data do pedido":     "order_date",
        "data":               "order_date",
        "valor total":        "gross_value",
        "valor bruto":        "gross_value",
        "comissão ifood":     "ifood_commission",
        "comissao ifood":     "ifood_commission",
        "forma de pagamento": "payment_method",
        "pagamento":          "payment_method",
        "status":             "status",
        "id cliente":         "customer_id",
        "id do cliente":      "customer_id",
        "taxa de entrega":    "delivery_fee",
        "entrega":            "delivery_fee",
    }

    def _read_raw(self, source: str) -> pd.DataFrame:
        """
        Reads the iFood CSV export, trying common delimiters and encodings.

        Raises
        ------
        FileNotFoundError:
            If the file path does not exist.
        ValueError:
            If the file cannot be parsed as a valid CSV.
        """
        logger.debug("[iFood] Reading file: {path}", path=source)

        # iFood exports can be semicolon or comma-delimited, and sometimes
        # include a UTF-8 BOM character. We try both delimiters.
        for sep in (";", ","):
            try:
                df = pd.read_csv(
                    source,
                    sep=sep,
                    encoding="utf-8-sig",   # handles BOM gracefully
                    dtype=str,              # read everything as string first; validators cast types
                    skip_blank_lines=True,
                )
                # Heuristic: if we got only 1 column, the delimiter was wrong.
                if df.shape[1] > 1:
                    logger.debug("[iFood] Parsed with delimiter='{sep}', shape={shape}", sep=sep, shape=df.shape)
                    return df
            except Exception as exc:
                logger.debug("[iFood] Failed with sep='{sep}': {exc}", sep=sep, exc=str(exc))

        raise ValueError(
            f"[iFood] Could not parse '{source}' as CSV. "
            "Ensure it is exported from the iFood partner portal in CSV format."
        )

    def _normalise_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Renames iFood's Portuguese headers to canonical schema field names,
        drops unrecognised columns, and strips whitespace from string values.
        """
        # Normalise header strings: lowercase + strip whitespace.
        df.columns = [col.strip().lower() for col in df.columns]

        # Rename known columns.
        rename_map = {
            raw: canonical
            for raw, canonical in self._COLUMN_MAP.items()
            if raw in df.columns
        }
        df = df.rename(columns=rename_map)

        # Drop rows that are entirely empty (common in iFood exports).
        df = df.dropna(how="all").reset_index(drop=True)

        # Strip leading/trailing whitespace from all string columns.
        str_cols = df.select_dtypes(include="object").columns
        df[str_cols] = df[str_cols].apply(lambda col: col.str.strip())

        # Filter to only the columns the schema expects; extras are silently dropped.
        expected_fields = set(IfoodOrderSchema.model_fields.keys())
        present_expected = [c for c in df.columns if c in expected_fields]
        df = df[present_expected]

        logger.debug(
            "[iFood] Normalised columns: {cols}",
            cols=list(df.columns),
        )
        return df
