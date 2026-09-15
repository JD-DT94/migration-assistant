"""Read an Azure alerting-catalogue tracker (.xlsx) into `AzureMonitor` rows.

Column names are matched case-insensitively and a few known aliases are
accepted, so a tracker that renames "Monitor Name" to "Alert Name" still loads.
Sheets without an ID column (Summary, scratch tabs) are skipped.
"""

from __future__ import annotations

import re
from typing import List, Optional

from e2d.azure.model import (AzureMonitor, parse_condition, parse_metric,
                             _SEVERITY as _SEVERITY_LEVEL)
from e2d.azure.xlsx import Workbook, header_index

# Sheets that are never a service catalogue.
_SKIP = {"summary", "sheet1", "legend", "readme", "index"}

_COLS = {
    "ident": ("ID", "Monitor ID", "Ref"),
    "tier": ("Tier", "Level"),
    "category": ("Category", "Domain"),
    "name": ("Monitor Name", "Alert Name", "Name"),
    "description": ("Description", "Rationale", "Why"),
    "metric": ("Metric / Log-Source Name", "Metric", "Metric Name", "Metric / Log Source"),
    "condition": ("Alert Condition", "Condition", "Threshold"),
    "severity": ("Severity", "Priority"),
    "ingestion": ("Ingestion Status", "Ingestion"),
    "mapping": ("Dynatrace Metric Mapping", "Metric Mapping", "Dynatrace Metric"),
}


def _cell(row: List[str], idx: Optional[int]) -> str:
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _backfill_severity(conditions: list, severity_cell: str) -> None:
    """A clause that names no severity inherits from the row's Severity column.

    The column is written as an escalation ("High/Critical"), so the Nth clause
    takes the Nth word — which is exactly the pairing the two clauses encode.
    Falls back to the last word when there are more clauses than words.
    """
    words = [w.strip() for w in re.split(r"[/,]", severity_cell or "") if w.strip()]
    if not words:
        return
    for i, cond in enumerate(conditions):
        if cond.severity_word:
            continue
        word = words[i] if i < len(words) else words[-1]
        cond.severity_word = word.capitalize()
        cond.severity = _SEVERITY_LEVEL.get(word.lower(), cond.severity)


def load_tracker(path: str, data: Optional[bytes] = None) -> List[AzureMonitor]:
    """Every catalogue row across every service sheet, in sheet order."""
    wb = Workbook.load_bytes(data) if data is not None else Workbook.load(path)
    out: List[AzureMonitor] = []
    for sheet in wb.sheet_names:
        if sheet.strip().lower() in _SKIP:
            continue
        rows = wb.rows(sheet)
        if len(rows) < 2:
            continue
        header = rows[0]
        idx = {k: header_index(header, *names) for k, names in _COLS.items()}
        if idx["ident"] is None or idx["name"] is None:
            continue  # not a catalogue sheet
        for row in rows[1:]:
            ident = _cell(row, idx["ident"])
            if not ident:
                continue
            metric_cell = _cell(row, idx["metric"])
            mapping_cell = _cell(row, idx["mapping"])
            key, agg, pct, dims = parse_metric(metric_cell, mapping_cell)
            cond_cell = _cell(row, idx["condition"])
            severity_cell = _cell(row, idx["severity"])
            conditions = parse_condition(cond_cell)
            _backfill_severity(conditions, severity_cell)
            out.append(AzureMonitor(
                service=sheet,
                ident=ident,
                tier=_cell(row, idx["tier"]),
                category=_cell(row, idx["category"]),
                name=_cell(row, idx["name"]),
                description=_cell(row, idx["description"]),
                metric_raw=metric_cell,
                metric_key=key,
                aggregation=agg,
                percentile=pct,
                dimensions=dims,
                condition_raw=cond_cell,
                conditions=conditions,
                severity_raw=severity_cell,
                ingestion=_cell(row, idx["ingestion"]),
                mapping_note=mapping_cell,
            ))
    return out
