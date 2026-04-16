"""
loaders/report.py
-----------------
Handles the Load phase of the ETL pipeline.

Responsibilities:
1. Persist the unified ledger as an auditable Excel workbook (multiple sheets).
2. Write a JSON summary of the DRE for downstream consumption (APIs, dashboards).
3. Write a standalone gap report CSV for the finance team.

Design decision: we write Excel (not just CSV) because the finance team
needs a single deliverable they can open without tooling. The workbook
includes:
  - Sheet 1: DRE Summary
  - Sheet 2: Unified Ledger (all transactions)
  - Sheet 3: Gap Report (discrepancies)
  - Sheet 4: Quarantine Log (rejected rows)
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
from loguru import logger

from etl_service.config import settings
from etl_service.transformers.financial import DRESummary
from etl_service.validators.schemas import GapSummary


class ReportLoader:
    """
    Saves ETL output artefacts to disk.

    Parameters
    ----------
    output_dir:
        Directory where output files will be written.
        Defaults to settings.output_dir.

    Usage
    -----
    >>> loader = ReportLoader()
    >>> output_path = loader.save(
    ...     dre=dre_summary,
    ...     ledger=unified_df,
    ...     gaps=gap_list,
    ...     quarantine=quarantine_df,
    ...     reference_month="2026-03",
    ... )
    """

    def __init__(self, output_dir: str | None = None) -> None:
        self.output_dir = Path(output_dir or settings.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    def save(
        self,
        dre: DRESummary,
        ledger: pd.DataFrame,
        gaps: list[GapSummary],
        quarantine: pd.DataFrame,
        reference_month: str,
    ) -> str:
        """
        Persists all output artefacts for the given reference month.

        Returns
        -------
        str
            Absolute path to the main Excel workbook.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"dataflow_{reference_month}_{timestamp}"

        excel_path = self.output_dir / f"{base_name}.xlsx"
        json_path  = self.output_dir / f"{base_name}_dre.json"

        logger.info("[Loader] Writing output artefacts to: {dir}", dir=self.output_dir)

        self._write_excel(
            excel_path=excel_path,
            dre=dre,
            ledger=ledger,
            gaps=gaps,
            quarantine=quarantine,
        )
        self._write_json(json_path=json_path, dre=dre, gaps=gaps)

        logger.success("[Loader] All artefacts saved. Main file: {path}", path=excel_path)
        return str(excel_path)

    # ------------------------------------------------------------------ #
    # Excel workbook writer
    # ------------------------------------------------------------------ #

    def _write_excel(
        self,
        excel_path: Path,
        dre: DRESummary,
        ledger: pd.DataFrame,
        gaps: list[GapSummary],
        quarantine: pd.DataFrame,
    ) -> None:
        """
        Writes a multi-sheet Excel workbook with formatting applied.
        """
        with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
            # ---- Sheet 1: DRE Summary ----
            dre_rows = [
                ("", ""),
                ("DEMONSTRATIVO DE RESULTADO DO EXERCÍCIO", ""),
                ("Período de Referência", dre.reference_month),
                ("Gerado em", datetime.now().strftime("%d/%m/%Y %H:%M")),
                ("", ""),
                ("RECEITA BRUTA", ""),
                ("  Vendas PDV (físico)", f"R$ {dre.gross_revenue_pdv:,.2f}"),
                ("  Pedidos iFood (bruto)", f"R$ {dre.gross_revenue_ifood:,.2f}"),
                ("  TOTAL RECEITA BRUTA", f"R$ {dre.total_gross_revenue:,.2f}"),
                ("", ""),
                ("DEDUÇÕES", ""),
                ("  Comissão iFood (23%)", f"- R$ {dre.total_ifood_commission:,.2f}"),
                ("  Taxas MDR (cartões)", f"- R$ {dre.total_card_fees:,.2f}"),
                ("  TOTAL DEDUÇÕES", f"- R$ {dre.total_deductions:,.2f}"),
                ("", ""),
                ("RECEITA LÍQUIDA", f"R$ {dre.net_revenue:,.2f}"),
                ("", ""),
                ("FUROS DE CAIXA DETECTADOS", str(len(gaps))),
                ("", ""),
                ("RECEITA POR FORMA DE PAGAMENTO", ""),
            ]
            for method, amount in dre.revenue_by_payment.items():
                dre_rows.append((f"  {method.capitalize()}", f"R$ {amount:,.2f}"))

            dre_df = pd.DataFrame(dre_rows, columns=["Item", "Valor"])
            dre_df.to_excel(writer, sheet_name="DRE Consolidado", index=False)

            # ---- Sheet 2: Unified Ledger ----
            if not ledger.empty:
                ledger_out = ledger.copy()
                # Rename columns to Portuguese for the finance team.
                rename = {
                    "source":                    "Origem",
                    "record_id":                 "ID Transação",
                    "date":                      "Data",
                    "gross_value":               "Valor Bruto (R$)",
                    "mdr_fee":                   "Taxa MDR (R$)",
                    "ifood_commission_computed":  "Comissão iFood (R$)",
                    "net_value_final":            "Valor Líquido (R$)",
                    "payment_method":            "Forma de Pagamento",
                    "status":                    "Status",
                }
                ledger_out = ledger_out.rename(columns={k: v for k, v in rename.items() if k in ledger_out.columns})
                ledger_out.to_excel(writer, sheet_name="Ledger Unificado", index=False)
            else:
                pd.DataFrame({"info": ["Sem dados no ledger."]}).to_excel(
                    writer, sheet_name="Ledger Unificado", index=False
                )

            # ---- Sheet 3: Gap Report ----
            if gaps:
                gaps_df = pd.DataFrame([
                    {
                        "ID Transação":   g.transaction_id,
                        "Origem":         g.source,
                        "Valor (R$)":     g.amount_brl,
                        "Tipo de Furo":   g.gap_type,
                        "Detalhe":        g.detail,
                    }
                    for g in gaps
                ])
            else:
                gaps_df = pd.DataFrame({"info": ["Nenhum furo de caixa detectado. ✓"]})
            gaps_df.to_excel(writer, sheet_name="Relatório de Furos", index=False)

            # ---- Sheet 4: Quarantine Log ----
            if not quarantine.empty:
                quarantine.to_excel(writer, sheet_name="Quarentena (Erros)", index=False)
            else:
                pd.DataFrame({"info": ["Nenhuma linha rejeitada. Todos os dados passaram na validação. ✓"]}).to_excel(
                    writer, sheet_name="Quarentena (Erros)", index=False
                )

        logger.info("[Loader] Excel workbook written: {path}", path=excel_path)

    # ------------------------------------------------------------------ #
    # JSON DRE writer
    # ------------------------------------------------------------------ #

    def _write_json(
        self,
        json_path: Path,
        dre: DRESummary,
        gaps: list[GapSummary],
    ) -> None:
        """
        Writes the DRE summary and gap list as a machine-readable JSON file.
        Useful for feeding dashboards, BI tools, or webhook integrations.
        """
        payload = {
            **dre.to_dict(),
            "gaps": [
                {
                    "transaction_id": g.transaction_id,
                    "source":         g.source,
                    "amount_brl":     g.amount_brl,
                    "gap_type":       g.gap_type,
                    "detail":         g.detail,
                }
                for g in gaps
            ],
            "generated_at": datetime.now().isoformat(),
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        logger.info("[Loader] JSON DRE written: {path}", path=json_path)
