"""
extractors/base.py
------------------
Abstract base class (ABC) for all data source extractors.

Design rationale (Factory / Template Method pattern):
- BaseExtractor defines the *contract* and the *pipeline skeleton*.
- Subclasses implement only the source-specific details (extract, _read_raw).
- The validate() method is implemented here using Pydantic v2 and is shared
  by all extractors, since the quarantine logic is identical regardless of source.
- Adding a new source (e.g. Rappi) = implement two methods, zero changes to
  any existing code.  This is the Open/Closed Principle in practice.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Generic, Type, TypeVar

import pandas as pd
from loguru import logger
from pydantic import BaseModel, ValidationError

SchemaT = TypeVar("SchemaT", bound=BaseModel)


# ====================================================================== #
# Validation result container
# ====================================================================== #

@dataclass
class ExtractionResult:
    """
    Holds the outcome of one extraction + validation cycle.

    Attributes
    ----------
    valid_df:
        DataFrame containing only rows that passed Pydantic validation.
    quarantine_df:
        DataFrame of rows that failed validation (preserves original data
        plus an extra 'validation_error' column for downstream inspection).
    source_name:
        Human-readable label of the data source (for logging / reports).
    """

    valid_df: pd.DataFrame
    quarantine_df: pd.DataFrame
    source_name: str
    raw_row_count: int = 0
    valid_row_count: int = field(init=False)
    quarantine_row_count: int = field(init=False)

    def __post_init__(self) -> None:
        self.valid_row_count = len(self.valid_df)
        self.quarantine_row_count = len(self.quarantine_df)

    def log_summary(self) -> None:
        logger.info(
            "[{source}] Extraction complete — raw={raw}, valid={valid}, quarantined={quarantine}",
            source=self.source_name,
            raw=self.raw_row_count,
            valid=self.valid_row_count,
            quarantine=self.quarantine_row_count,
        )
        if self.quarantine_row_count > 0:
            logger.warning(
                "[{source}] {count} row(s) quarantined — check quarantine_df for details.",
                source=self.source_name,
                count=self.quarantine_row_count,
            )


# ====================================================================== #
# Abstract Base Extractor
# ====================================================================== #

class BaseExtractor(abc.ABC, Generic[SchemaT]):
    """
    Abstract extractor that every data-source connector must extend.

    Subclasses must implement:
    - source_name: class-level string label.
    - schema_class: the Pydantic model to validate each row against.
    - _read_raw(source): reads the raw file and returns a raw DataFrame.
    - _normalise_columns(df): renames/casts columns to match schema fields.
    """

    source_name: str
    schema_class: Type[SchemaT]

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    def extract(self, source: str) -> ExtractionResult:
        """
        Full extraction pipeline: read → normalise → validate.

        Parameters
        ----------
        source:
            A file path (CSV, XLSX) or connection string, depending on subclass.

        Returns
        -------
        ExtractionResult
            Contains valid_df (clean rows) and quarantine_df (invalid rows).
        """
        logger.info("[{src}] Starting extraction from: {source}", src=self.source_name, source=source)

        raw_df = self._read_raw(source)
        logger.debug("[{src}] Raw row count: {n}", src=self.source_name, n=len(raw_df))

        normalised_df = self._normalise_columns(raw_df.copy())

        result = self._validate_rows(normalised_df)
        result.raw_row_count = len(raw_df)
        result.log_summary()

        return result

    # ------------------------------------------------------------------ #
    # Shared validation logic (Template Method)
    # ------------------------------------------------------------------ #

    def _validate_rows(self, df: pd.DataFrame) -> ExtractionResult:
        """
        Iterates over each row, instantiates the Pydantic schema,
        and splits rows into valid / quarantine buckets.

        This gives us granular per-row error messages and ensures the
        pipeline never crashes silently on dirty data.
        """
        valid_records: list[dict[str, Any]] = []
        quarantine_records: list[dict[str, Any]] = []

        for idx, row in df.iterrows():
            row_dict = row.to_dict()
            try:
                validated = self.schema_class.model_validate(row_dict)
                valid_records.append(validated.model_dump())
            except ValidationError as exc:
                # Extract a concise human-readable error summary.
                error_summary = "; ".join(
                    f"{e['loc'][0] if e['loc'] else 'root'}: {e['msg']}"
                    for e in exc.errors()
                )
                logger.warning(
                    "[{src}] Row {idx} quarantined — {error}",
                    src=self.source_name,
                    idx=idx,
                    error=error_summary,
                )
                quarantine_record = row_dict.copy()
                quarantine_record["validation_error"] = error_summary
                quarantine_record["source_row_index"] = idx
                quarantine_records.append(quarantine_record)

        valid_df      = pd.DataFrame(valid_records)      if valid_records      else pd.DataFrame()
        quarantine_df = pd.DataFrame(quarantine_records) if quarantine_records else pd.DataFrame()

        return ExtractionResult(
            valid_df=valid_df,
            quarantine_df=quarantine_df,
            source_name=self.source_name,
        )

    # ------------------------------------------------------------------ #
    # Abstract methods (subclasses must implement)
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def _read_raw(self, source: str) -> pd.DataFrame:
        """
        Read the raw data from `source` and return an unmodified DataFrame.

        Parameters
        ----------
        source:
            File path or connection string.
        """
        ...

    @abc.abstractmethod
    def _normalise_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Rename, cast, and clean columns so they match the Pydantic schema's
        field names and expected types.

        Parameters
        ----------
        df:
            The raw DataFrame returned by _read_raw.
        """
        ...
