"""The catalogue row model, plus parsers for the two prose columns.

A tracker row carries its metric and its firing logic as English:

    metric:    ext:microsoft_network.applicationgateways_essential.UnhealthyHostCount
               Max split by BackendSettingsPool (Essential — DS-exportable)
    condition: Max > 0 for any pool for 2 min -> High;
               all hosts unhealthy in same pool -> Critical

`parse_metric` pulls the key, aggregation and split dimensions out of the first;
`parse_condition` turns the second into one `Condition` per `-> severity`
clause. Anything the grammar does not recognise is returned as an unparsed
clause rather than guessed at — a wrong threshold is worse than an absent one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

# Condition kinds. Only STATIC and LOG_COUNT become a static-threshold
# detector; the rest need a different Dynatrace construct (noted per kind).
STATIC = "static"        # a fixed threshold      -> static-threshold analyzer
BASELINE = "baseline"    # vs a rolling baseline  -> Davis auto-adaptive analyzer
DAVIS_AI = "davis_ai"    # "Davis AI anomaly"     -> Davis AI, no threshold
SLO_BURN = "slo_burn"    # SLO burn rate          -> detector over an SLO metric
EVENT = "event"          # state transition       -> log/event trigger, not a metric
LOG_COUNT = "log_count"  # count of log records   -> makeTimeseries over logs
TRIGGER = "trigger"      # fires off another alert-> Workflow subscription, no detector
NONE = "none"            # dashboard / NA         -> not an alert at all
UNPARSED = "unparsed"    # grammar miss           -> human sets it

# Severity words the catalogue uses, mapped to Dynatrace event severity levels.
# `dt.davis.event.severity_level` only offers error/warning/info, and most rows
# escalate High -> Critical across two clauses, so High lands on `warning` to
# keep that escalation visible in Davis. The catalogue's own word is preserved
# verbatim on the event as `catalogue.severity` for precise alerting-profile
# routing, so nothing is lost by the narrowing.
_SEVERITY = {
    "critical": "error", "high": "warning", "major": "warning",
    "medium": "warning", "minor": "warning", "warning": "warning",
    "info": "info", "informational": "info",
}

# Order matters: a clause reading "High/Critical" must resolve to the more
# severe word, and "Informational" must not match on the "info" substring first.
_SEVERITY_ORDER = ("critical", "high", "major", "medium", "minor",
                   "warning", "informational", "info")

_AGGS = {
    "max": "max", "maximum": "max", "min": "min", "minimum": "min",
    "sum": "sum", "avg": "avg", "average": "avg", "mean": "avg",
    "count": "count", "total": "sum",
}

# "P95" / "P99" / "P50"
_PCT_AGG = re.compile(r"\bP(\d{2,3})\b")


@dataclass
class Condition:
    """One `-> severity` clause of an Alert Condition cell."""
    kind: str
    severity: str = "error"          # Dynatrace event severity: error|warning|info
    comparator: str = ">"            # > >= < <= == !=
    value: Optional[float] = None    # the threshold, in the metric's own unit
    unit: str = ""                   # ms | % | CU | count ...
    duration: Optional[str] = None   # "5m" — how long it must hold
    raw: str = ""                    # the source clause, always kept
    severity_word: str = ""          # the catalogue's own word: High, Critical ...

    @property
    def alert_condition(self) -> str:
        """Davis analyzer alertCondition for this comparator."""
        return "BELOW" if self.comparator in ("<", "<=") else "ABOVE"


@dataclass
class AzureMonitor:
    """One catalogue row, normalised."""
    service: str                     # sheet name, e.g. "App Gateway"
    ident: str                       # "B-01"
    tier: str                        # Bronze | Silver | Gold
    category: str
    name: str
    description: str = ""
    metric_raw: str = ""             # the Metric / Log-Source cell, verbatim
    metric_key: str = ""             # resolved cloud.azure.* / ext:* key
    aggregation: str = "avg"         # max|min|sum|avg|count|percentile
    percentile: Optional[int] = None # set when aggregation == "percentile"
    dimensions: List[str] = field(default_factory=list)
    condition_raw: str = ""
    conditions: List[Condition] = field(default_factory=list)
    severity_raw: str = ""
    ingestion: str = ""              # Ingesting | Not Ingesting | N/A (Log-Based)
    mapping_note: str = ""           # the Dynatrace Metric Mapping cell

    @property
    def buildable(self) -> bool:
        """True when at least one clause becomes a deployable detector."""
        return any(c.kind in (STATIC, LOG_COUNT, BASELINE, DAVIS_AI, SLO_BURN)
                   for c in self.conditions)

    @property
    def blocked(self) -> bool:
        return self.ingestion == "Not Ingesting"

    @property
    def dql_agg(self) -> str:
        """The DQL aggregation expression for this monitor's metric."""
        if self.aggregation == "percentile" and self.percentile:
            return f"percentile({self.metric_key}, {self.percentile})"
        if self.aggregation == "count":
            return "count()"
        return f"{self.aggregation}({self.metric_key})"


# --------------------------------------------------------------------------- #
# metric column
# --------------------------------------------------------------------------- #

_KEY_RE = re.compile(r"((?:ext:|cloud\.azure\.)[A-Za-z0-9_.]+[A-Za-z0-9])")
_SPLIT_RE = re.compile(r"split by ([A-Za-z0-9_, ]+?)(?:\s*\(|$|;|\.\s)", re.I)


def parse_metric(metric_cell: str, mapping_cell: str = "") -> "tuple":
    """(key, aggregation, percentile, dimensions) from the two metric columns.

    The mapping column holds the key the tenant actually resolved to, so it wins
    over the catalogue's Azure-side spelling when present.
    """
    key = ""
    m = _KEY_RE.search(mapping_cell or "")
    if m:
        key = m.group(1)
    else:
        m = _KEY_RE.search(metric_cell or "")
        if m:
            key = m.group(1)
    key = key.rstrip(".")

    agg, pct = "avg", None
    pm = _PCT_AGG.search(metric_cell or "")
    if pm:
        agg, pct = "percentile", int(pm.group(1))
    else:
        # the aggregation word normally follows the key
        tail = (metric_cell or "")[m.end():] if m else (metric_cell or "")
        for word in re.findall(r"\b([A-Za-z]+)\b", tail[:40]):
            if word.lower() in _AGGS:
                agg = _AGGS[word.lower()]
                break

    dims: List[str] = []
    sm = _SPLIT_RE.search(metric_cell or "")
    if sm:
        dims = [d.strip() for d in sm.group(1).split(",") if d.strip()]
        # a trailing prose word ("split by Listener and BackendPool (Essential")
        dims = [d for d in dims if re.fullmatch(r"[A-Za-z0-9_]+", d)]
    return key, agg, pct, dims


# --------------------------------------------------------------------------- #
# condition column
# --------------------------------------------------------------------------- #

_DUR_RE = re.compile(r"for\s+(\d+)\s*(min(?:ute)?s?|h(?:our|r)?s?|s(?:ec(?:ond)?s?)?)\b", re.I)
_WINDOW_RE = re.compile(r"(\d+)\s*-?\s*(min(?:ute)?|h(?:our|r)?)\w*\s+window", re.I)
_NUM = r"(-?[\d,]+(?:\.\d+)?)"
_CMP_RE = re.compile(r"(>=|<=|>|<|==|=)\s*" + _NUM + r"\s*(%|ms|s\b|CU|GB|MB)?", re.I)
_SLO_BURN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*x\s+.*burn rate", re.I)
_SPIKE_RE = re.compile(r"(spike|drop|deviat|baseline|rolling)", re.I)

# Activity-Log style predicates: a control-plane operation, not a metric value.
_OPERATION_RE = re.compile(
    r"\b(write|delete|purge|create|update|modif\w*|deployment|role assignment|"
    r"policy (?:change|disabled|enabled)|sku tier|capacity unit)\b", re.I)
_ANY_OP_RE = re.compile(r"\bany\b.*\b(operation|change|write|delete|create)\b", re.I)
# "Health transitions to Degraded", or a bare continuation "to Unavailable"
_TRANSITION_RE = re.compile(r"(transitions?\s+to|^to\s+[A-Z])", re.I)
# Azure Service Health incident severity, e.g. "Severity >= Sev1 active incident"
_SEV_INCIDENT_RE = re.compile(r"\bsev\s*\d\b|\bactive incident\b|service health|"
                              r"azure publishes|planned maintenance", re.I)
# "Any threat intelligence log entry", "Any IDPS signature match with action Deny"
_ANY_LOG_RE = re.compile(r"\bany\b[^.]*\b(log entry|log entries|signature match|"
                         r"blocked query|audit (?:log|entry)|match with action)\b", re.I)
# A clause that fires off an existing Dynatrace problem rather than a metric —
# these become a Workflow subscription, never a second detector.
_DT_TRIGGER_RE = re.compile(r"dynatrace problem|problem (?:on|from)\s|davis problem", re.I)
# "drop to 0", "= Unhealthy (0%)", "drop to zero"
_TO_ZERO_RE = re.compile(r"(drop(?:s|ping)?\s+to\s+(?:0|zero)\b|\(0%\)|=\s*0\b)", re.I)


def _duration(text: str) -> Optional[str]:
    m = _DUR_RE.search(text) or _WINDOW_RE.search(text)
    if not m:
        return None
    n, unit = m.group(1), m.group(2).lower()
    if unit.startswith("h"):
        suffix = "h"
    elif unit.startswith("s"):
        suffix = "s"
    else:
        suffix = "m"
    return f"{n}{suffix}"


def severity_word(clause: str) -> str:
    """The catalogue's own severity word for a clause ('Critical', 'High' ...)."""
    tail = clause.split("->")[-1] if "->" in clause else clause
    for word in _SEVERITY_ORDER:
        if re.search(rf"\b{word}\b", tail, re.I):
            return word.capitalize()
    return ""


def _severity_of(clause: str, default: str = "error") -> str:
    word = severity_word(clause)
    return _SEVERITY.get(word.lower(), default) if word else default


def _parse_clause(clause: str, whole: str) -> Condition:
    """One `... -> Severity` fragment."""
    raw = clause.strip()
    low = raw.lower()
    sev = _severity_of(raw)
    word = severity_word(raw)
    dur = _duration(raw) or _duration(whole)

    if "dashboard" in low or "refresh" in low:
        return Condition(NONE, sev, severity_word=word, raw=raw)
    # Check the trigger form before DAVIS_AI: "Critical Dynatrace problem on X"
    # subscribes to an existing detector, it does not define a new one.
    if _DT_TRIGGER_RE.search(raw):
        return Condition(TRIGGER, sev, severity_word=word, duration=dur, raw=raw)
    if "davis ai" in low or "davis anomaly" in low:
        return Condition(DAVIS_AI, sev, severity_word=word, duration=dur, raw=raw)

    sm = _SLO_BURN_RE.search(raw)
    if sm:
        return Condition(SLO_BURN, sev, severity_word=word, comparator=">", value=float(sm.group(1)),
                         unit="x", duration=dur, raw=raw)

    cm = _CMP_RE.search(raw)
    # a baseline-relative clause ("> 3x rolling 1-hr baseline", "drop > 50% from
    # baseline") is NOT a static threshold even though it contains a comparator
    if _SPIKE_RE.search(raw) and ("baseline" in low or "rolling" in low
                                  or re.search(r"\d+\s*x\b", low)):
        val = float(cm.group(2).replace(",", "")) if cm else None
        return Condition(BASELINE, sev, severity_word=word, comparator=(cm.group(1) if cm else ">"),
                         value=val, unit=(cm.group(3) or "" if cm else ""),
                         duration=dur, raw=raw)

    # Control-plane / state-change predicates read an event stream, not a metric
    # series, so they are classified before the numeric-comparator branch — an
    # "Severity >= Sev1" incident clause must not become a static threshold of 1.
    if _SEV_INCIDENT_RE.search(raw):
        return Condition(EVENT, sev, severity_word=word, duration=dur, raw=raw)
    if _TRANSITION_RE.search(raw) or "transitions" in low:
        return Condition(EVENT, sev, severity_word=word, duration=dur, raw=raw)
    if _ANY_LOG_RE.search(raw):
        return Condition(EVENT, sev, severity_word=word, duration=dur, raw=raw)
    if _ANY_OP_RE.search(raw) or (raw.lower().startswith("any ") and _OPERATION_RE.search(raw)):
        return Condition(EVENT, sev, severity_word=word, duration=dur, raw=raw)
    if not cm and _OPERATION_RE.search(raw):
        return Condition(EVENT, sev, severity_word=word, duration=dur, raw=raw)
    # "drops to 0" / "(0%)" is a real static floor, just written in prose
    if not cm and _TO_ZERO_RE.search(raw):
        return Condition(STATIC, sev, severity_word=word, comparator="<=", value=0.0,
                         duration=dur, raw=raw)

    if cm:
        op = cm.group(1)
        op = "==" if op == "=" else op
        val = float(cm.group(2).replace(",", ""))
        unit = (cm.group(3) or "").strip()
        kind = LOG_COUNT if re.search(r"count of .*log|log entries", low) else STATIC
        return Condition(kind, sev, severity_word=word, comparator=op, value=val, unit=unit,
                         duration=dur, raw=raw)

    if "detected" in low or "event" in low or "any entry" in low:
        return Condition(EVENT, sev, severity_word=word, duration=dur, raw=raw)

    return Condition(UNPARSED, sev, severity_word=word, duration=dur, raw=raw)


def parse_condition(text: str) -> List[Condition]:
    """Split an Alert Condition cell into one Condition per severity clause."""
    text = (text or "").strip()
    if not text or text.upper() in ("NA", "N/A", "-"):
        return [Condition(NONE, "info", raw=text)]
    parts = [p for p in re.split(r"\s*;\s*", text) if p.strip()]
    out = [_parse_clause(p, text) for p in parts]
    # A trailing clause often omits the aggregation ("> 5% for 5 min -> Critical")
    # and inherits the subject of the first — nothing to do for the threshold
    # itself, but if it parsed as UNPARSED while the first was STATIC, the cell
    # is prose we should not guess at; leave it flagged.
    return out
