"""Turn parsed catalogue rows into deployable detectors, and analyse the set.

Three outputs:

  * `detectors`  — one Settings 2.0 object per firing clause that can be a
                   Davis anomaly detector, with its DQL linted.
  * `deferred`   — rows that cannot be a detector (event sources, workflow
                   triggers, dashboards) or whose condition needs a human.
  * `merges`     — groups of monitors reading the same metric, which collapse
                   into one detector split by a dimension.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from e2d.azure.model import (AzureMonitor, Condition, BASELINE, DAVIS_AI, EVENT,
                             LOG_COUNT, NONE, SLO_BURN, STATIC, TRIGGER, UNPARSED)
from e2d.dql.validate import lint_into_report
from e2d.report import Report
from e2d.sinks.dynatrace import ANOMALY_SCHEMA

STATIC_ANALYZER = "dt.statistics.ui.anomaly_detection.StaticThresholdAnomalyDetectionAnalyzer"
ADAPTIVE_ANALYZER = "dt.statistics.ui.anomaly_detection.AutoAdaptiveThresholdAnomalyDetectionAnalyzer"

# How each non-detector clause kind should actually be built in Dynatrace.
ALTERNATIVE = {
    EVENT: ("Not a metric series. Ingest the Azure Activity Log / Resource Health "
            "feed into Grail and raise the event from a Workflow (or a log-event "
            "rule) filtered on the operation — one Workflow covers every row of "
            "this kind across all services."),
    TRIGGER: ("Fires off an existing detector's problem, so it needs no detector "
              "of its own — build it as a Workflow subscribed to that problem's "
              "event, with the ServiceNow/notification action attached."),
    NONE: ("Not an alert — a dashboard or notebook. Build it in the Dashboards "
           "app; the DQL for its tiles is the same query the sibling detectors use."),
    UNPARSED: ("The catalogue states this condition in prose the parser will not "
               "guess at (composite score, forecast, or a clause that refers to a "
               "different metric). Set the threshold by hand."),
}


@dataclass
class BuiltDetector:
    monitor: AzureMonitor
    condition: Condition
    key: str                       # stable id: SERVICE/IDENT#n
    title: str
    dql: str
    settings: Dict[str, Any]       # the Settings 2.0 object
    report: Report = field(default_factory=Report)


@dataclass
class Deferred:
    monitor: AzureMonitor
    condition: Condition
    reason: str
    alternative: str


@dataclass
class MergeGroup:
    metric_key: str
    services: List[str]
    monitors: List[AzureMonitor]
    dimensions: List[str]
    directions: List[str] = field(default_factory=list)  # ABOVE / BELOW seen

    @property
    def mergeable(self) -> bool:
        """A floor and a ceiling on the same metric are two different questions,
        so a group holding both stays as separate detectors."""
        return len(self.directions) <= 1

    @property
    def saving(self) -> int:
        """Detectors removed by merging this group into one."""
        if not self.mergeable:
            return 0
        return max(0, len(self.monitors) - 1)


@dataclass
class CatalogueResult:
    monitors: List[AzureMonitor]
    detectors: List[BuiltDetector]
    deferred: List[Deferred]
    merges: List[MergeGroup]

    def counts(self) -> Dict[str, int]:
        return {
            "monitors": len(self.monitors),
            "detectors": len(self.detectors),
            "deferred": len(self.deferred),
            "blocked": sum(1 for d in self.detectors if d.monitor.blocked),
            "merge_groups": len(self.merges),
            "merge_saving": sum(g.saving for g in self.merges),
        }


# --------------------------------------------------------------------------- #
# DQL
# --------------------------------------------------------------------------- #

def _by_clause(dims: List[str]) -> str:
    return f", by:{{{', '.join(dims)}}}" if dims else ""


def detector_dql(mon: AzureMonitor) -> str:
    """The 1-minute single-series query the analyzer evaluates.

    A metric-backed monitor uses `timeseries` (the metric already exists in
    Grail); a log-count monitor has no metric, so it counts log records with
    `makeTimeseries` instead.
    """
    if not mon.metric_key:
        return (f"fetch logs\n| makeTimeseries value = count(), interval:1m"
                f"{_by_clause(mon.dimensions)}")
    # `percentile` over a metric needs an explicit rollup or the series comes
    # back empty — the raw datapoints have no defined reduction into the interval.
    rollup = ", rollup: avg" if mon.aggregation == "percentile" else ""
    return (f"timeseries value = {mon.dql_agg}{_by_clause(mon.dimensions)}"
            f", interval:1m{rollup}")


# --------------------------------------------------------------------------- #
# Settings 2.0 object
# --------------------------------------------------------------------------- #

def _kv(pairs: List[Tuple[str, str]]) -> List[Dict[str, str]]:
    return [{"key": k, "value": v} for k, v in pairs]


def _samples(duration: Optional[str]) -> int:
    """Violating samples for a 1-minute series, from the clause's hold time."""
    if not duration:
        return 3
    try:
        n = int("".join(ch for ch in duration if ch.isdigit()))
    except ValueError:
        return 3
    if duration.endswith("h"):
        n *= 60
    elif duration.endswith("s"):
        n = max(1, n // 60)
    return max(1, min(n, 60))


def build_settings(mon: AzureMonitor, cond: Condition, title: str, dql: str) -> Dict[str, Any]:
    """The `builtin:davis.anomaly-detectors` value for one firing clause."""
    samples = _samples(cond.duration)
    if cond.kind in (BASELINE, DAVIS_AI):
        # No fixed number to compare against — let Davis learn the baseline.
        analyzer = {
            "name": ADAPTIVE_ANALYZER,
            "input": _kv([
                ("query", dql),
                ("alertCondition", cond.alert_condition),
                ("violatingSamples", str(samples)),
                ("slidingWindow", str(max(samples, 5))),
                ("dealertingSamples", str(max(samples, 5))),
                ("alertOnMissingData", "false"),
            ]),
        }
        enabled = True
    else:
        threshold = cond.value
        enabled = threshold is not None
        analyzer = {
            "name": STATIC_ANALYZER,
            "input": _kv([
                ("query", dql),
                ("alertCondition", cond.alert_condition),
                ("threshold", _fmt(threshold if threshold is not None else 0)),
                ("violatingSamples", str(samples)),
                ("slidingWindow", str(max(samples, 5))),
                ("dealertingSamples", str(max(samples, 5))),
                ("alertOnMissingData", "false"),
            ]),
        }
    return {
        "enabled": enabled and not mon.blocked,
        "title": title[:500],
        "description": _description(mon, cond),
        "source": "e2d-azure",
        "analyzer": analyzer,
        "eventTemplate": {
            "properties": _kv([
                ("event.name", title[:500]),
                ("event.type", "CUSTOM_ALERT"),
                ("dt.davis.event.severity_level", cond.severity),
                ("azure.service", mon.service),
                ("catalogue.id", f"{mon.service}/{mon.ident}"),
                ("catalogue.tier", mon.tier or "Bronze"),
                # the catalogue's own word, so alerting profiles can route on
                # High vs Critical even though Davis only carries error/warning
                ("catalogue.severity", cond.severity_word or "Critical"),
            ]),
        },
        "executionSettings": {},
    }


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


def _description(mon: AzureMonitor, cond: Condition) -> str:
    bits = [f"{mon.service} {mon.ident} ({mon.tier})", f"source condition: {cond.raw}"]
    if mon.blocked:
        bits.append("DISABLED: metric is not ingesting in this tenant "
                    f"({mon.mapping_note[:120]})")
    elif cond.value is None and cond.kind not in (BASELINE, DAVIS_AI):
        bits.append("DISABLED: no threshold could be derived — set one")
    return " — ".join(bits)[:1000]


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #

_DETECTOR_KINDS = (STATIC, LOG_COUNT, BASELINE, DAVIS_AI, SLO_BURN)


def build_catalogue(monitors: List[AzureMonitor]) -> CatalogueResult:
    detectors: List[BuiltDetector] = []
    deferred: List[Deferred] = []

    for mon in monitors:
        for n, cond in enumerate(mon.conditions, start=1):
            if cond.kind not in _DETECTOR_KINDS:
                deferred.append(Deferred(
                    mon, cond,
                    reason=f"condition is `{cond.kind}`, not a metric threshold",
                    alternative=ALTERNATIVE.get(cond.kind, ALTERNATIVE[UNPARSED])))
                continue
            if cond.kind == SLO_BURN:
                deferred.append(Deferred(
                    mon, cond,
                    reason="SLO burn-rate condition",
                    alternative=("Define the SLO first, then point a detector at its "
                                 "burn-rate metric — the catalogue's SLO target is in "
                                 "the Metric column, not the Condition column.")))
                continue

            title = f"[{mon.service}] {mon.name}"
            if len([c for c in mon.conditions if c.kind in _DETECTOR_KINDS]) > 1:
                title += f" ({cond.severity})"
            dql = detector_dql(mon)
            rep = Report()
            lint_into_report(dql, rep, "logs" if not mon.metric_key else "metrics")
            if mon.blocked:
                rep.warn(f"Metric `{mon.metric_key}` is not ingesting in this tenant; "
                         "the detector ships DISABLED so it can be created now and "
                         "enabled once data flows.")
            if not mon.metric_key:
                rep.warn("No Dynatrace metric key resolved from the catalogue row; "
                         "the query counts log records instead — confirm the source.")
            detectors.append(BuiltDetector(
                monitor=mon, condition=cond,
                key=f"{mon.service}/{mon.ident}#{n}",
                title=title, dql=dql,
                settings=build_settings(mon, cond, title, dql),
                report=rep))

    return CatalogueResult(monitors, detectors, deferred, find_merges(monitors))


def find_merges(monitors: List[AzureMonitor]) -> List[MergeGroup]:
    """Monitors reading the same metric key are one detector split by dimension.

    Grouping is by metric key alone, deliberately: two rows on the same metric
    in the same service are always the same series, and the same metric across
    services means the same Azure resource type, so a single detector split by
    `dt.entity.cloud_application` (or the resource dimension) covers both.
    """
    by_key: Dict[str, List[AzureMonitor]] = {}
    for m in monitors:
        if not m.metric_key:
            continue
        if not any(c.kind in _DETECTOR_KINDS for c in m.conditions):
            continue
        by_key.setdefault(m.metric_key, []).append(m)

    groups: List[MergeGroup] = []
    for key, mons in by_key.items():
        if len(mons) < 2:
            continue
        dims: List[str] = []
        dirs: List[str] = []
        for m in mons:
            for d in m.dimensions:
                if d not in dims:
                    dims.append(d)
            for c in m.conditions:
                if c.kind in (STATIC, LOG_COUNT) and c.alert_condition not in dirs:
                    dirs.append(c.alert_condition)
        groups.append(MergeGroup(
            metric_key=key,
            services=sorted({m.service for m in mons}),
            monitors=mons,
            dimensions=dims,
            directions=dirs))
    groups.sort(key=lambda g: (-g.saving, g.metric_key))
    return groups


def settings_payload(detectors: List[BuiltDetector]) -> List[Dict[str, Any]]:
    """The POST body for /api/v2/settings/objects."""
    return [{"schemaId": ANOMALY_SCHEMA, "scope": "environment", "value": d.settings}
            for d in detectors]
