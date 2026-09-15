"""Markdown reports over a built Azure catalogue."""

from __future__ import annotations

from typing import List

from e2d.azure.build import CatalogueResult, BuiltDetector
from e2d.azure.model import BASELINE, DAVIS_AI


def _status(d: BuiltDetector) -> str:
    if d.monitor.blocked:
        return "BLOCKED"
    if d.report.has_blocking:
        return "MANUAL"
    if d.report.needs_review:
        return "REVIEW"
    return "READY"


def _esc(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def render_catalogue(res: CatalogueResult) -> str:
    c = res.counts()
    L = ["# Azure alerting catalogue — Dynatrace build list", "",
         f"{c['monitors']} catalogue rows produced **{c['detectors']} Davis anomaly detectors**, "
         f"of which **{c['detectors'] - c['blocked']} are deployable today** and "
         f"**{c['blocked']} ship disabled** because their metric is not ingesting. "
         f"{c['deferred']} clauses are not detectors at all — see `DEFERRED.md`. "
         f"{c['merge_saving']} detectors can be removed by merging — see `CONSOLIDATION.md`.", "",
         "| Status | Meaning |", "|---|---|",
         "| READY | Metric is ingesting, threshold derived, DQL lints clean — deploy as-is. |",
         "| BLOCKED | Built and importable, but `enabled:false` — the metric reports no data yet. |",
         "| REVIEW | Deployable, but the DQL linter or the mapping raised a note. |",
         "| MANUAL | The DQL linter found something that must be fixed first. |", ""]

    by_service = {}
    for d in res.detectors:
        by_service.setdefault(d.monitor.service, []).append(d)

    for service in sorted(by_service):
        ds = by_service[service]
        ready = sum(1 for d in ds if _status(d) == "READY")
        L += ["", f"## {service}  ({len(ds)} detectors, {ready} ready)", "",
              "| ID | Tier | Detector | Analyzer | Condition | Severity | Status |",
              "|---|---|---|---|---|---|---|"]
        for d in ds:
            cond = d.condition
            analyzer = "adaptive" if cond.kind in (BASELINE, DAVIS_AI) else "static"
            if cond.value is not None and cond.kind not in (BASELINE, DAVIS_AI):
                expr = f"`{cond.comparator} {cond.value:g}{cond.unit}`"
            else:
                expr = "_baseline_"
            hold = f" for {cond.duration}" if cond.duration else ""
            L.append(f"| {d.monitor.ident} | {d.monitor.tier} | {_esc(d.monitor.name)} | "
                     f"{analyzer} | {expr}{hold} | {cond.severity} | {_status(d)} |")

        L += ["", "<details><summary>DQL for each detector</summary>", ""]
        for d in ds:
            L.append(f"**{d.monitor.ident}** — {_esc(d.monitor.name)}")
            L.append("```dql")
            L.append(d.dql)
            L.append("```")
            for note in d.report.format_deduped():
                L.append(f"> {note}")
            L.append("")
        L.append("</details>")
    return "\n".join(L) + "\n"


def render_consolidation(res: CatalogueResult) -> str:
    saving = res.counts()["merge_saving"]
    mergeable = [g for g in res.merges if g.mergeable]
    blocked = [g for g in res.merges if not g.mergeable]
    L = ["# Consolidation — detectors that share a metric", "",
         f"{len(res.merges)} metrics are read by more than one catalogue row. "
         f"{len(mergeable)} of those groups collapse into a single detector split by their "
         f"dimensions, removing **{saving} detectors** with no loss of coverage: Davis raises "
         "one problem per dimension value, so a detector split by `BackendPool` already "
         "alerts per pool.", "",
         f"The other {len(blocked)} groups read the same metric but set a **floor and a "
         "ceiling** — two different questions — so they stay as separate detectors. They are "
         "listed at the end for completeness.", ""]
    for g in mergeable + blocked:
        verdict = (f"**saves {g.saving}**" if g.mergeable
                   else "**do not merge** — mixed threshold direction "
                        f"({' and '.join(g.directions)})")
        L += [f"## `{g.metric_key}`", "",
              f"- **Rows:** {len(g.monitors)} — {verdict}",
              f"- **Services:** {', '.join(g.services)}",
              f"- **Dimensions to split by:** "
              f"{', '.join('`' + d + '`' for d in g.dimensions) if g.dimensions else '_none declared_'}",
              ""]
        L += ["| ID | Service | Monitor | Condition |", "|---|---|---|---|"]
        for m in g.monitors:
            L.append(f"| {m.ident} | {m.service} | {_esc(m.name)} | {_esc(m.condition_raw)[:90]} |")
        if g.mergeable:
            by = f", by:{{{', '.join(g.dimensions)}}}" if g.dimensions else ""
            L += ["", "Merged query:", "", "```dql",
                  f"timeseries value = {g.monitors[0].aggregation}({g.metric_key}){by}, interval:1m",
                  "```", ""]
        else:
            L.append("")
    return "\n".join(L) + "\n"


def render_deferred(res: CatalogueResult) -> str:
    L = ["# Not anomaly detectors — build these another way", "",
         f"{len(res.deferred)} catalogue clauses cannot be a Davis anomaly detector. "
         "They are grouped by what they should be instead.", ""]
    by_reason = {}
    for d in res.deferred:
        by_reason.setdefault(d.alternative, []).append(d)
    for alt, items in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        L += [f"## {len(items)} clause(s)", "", alt, "",
              "| Service | ID | Monitor | Source condition |", "|---|---|---|---|"]
        for d in items:
            L.append(f"| {d.monitor.service} | {d.monitor.ident} | {_esc(d.monitor.name)} | "
                     f"{_esc(d.condition.raw)[:100]} |")
        L.append("")
    return "\n".join(L) + "\n"
