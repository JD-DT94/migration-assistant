"""Metric-existence check + OpenPipeline metric creation.

A `metrics.alert.threshold` rule references an Elastic metric name (e.g.
`system.cpu.total.norm.pct`). That key almost never exists verbatim in Grail, so
firing an anomaly detector on it would silently return nothing. So we **check**
every metric a detector reads and, for the ones that don't look like Dynatrace
metrics, emit a starter **OpenPipeline `value_metric` processor** (with a matcher)
that *creates* the metric from logs — the supported way to make a metric in
Dynatrace (`dynatrace_openpipeline_v2_logs_pipelines`).
"""

from __future__ import annotations

import re
from typing import List, Tuple

from e2d.alerts.model import AlertSpec, Detector

# A metric the platform already provides: the `dt.` namespace, or a Grail
# builtin (`builtin:`). Anything else (Elastic/metricbeat names) needs creating.
_KNOWN_PREFIXES = ("dt.", "builtin:")


def is_dynatrace_metric(key: str) -> bool:
    return bool(key) and key.startswith(_KNOWN_PREFIXES)


def missing_metrics(spec: AlertSpec) -> List[Tuple[str, Detector]]:
    """(metric_key, detector) for every detector reading a non-Dynatrace metric."""
    seen, out = set(), []
    for d in spec.detectors:
        if d.metric_key and not is_dynatrace_metric(d.metric_key) and d.metric_key not in seen:
            seen.add(d.metric_key)
            out.append((d.metric_key, d))
    return out


def _safe_key(metric: str) -> str:
    # a creatable Grail metric key under a clear migration namespace
    cleaned = re.sub(r"[^a-z0-9_.]", "_", metric.lower()).strip("._")
    return f"log.{cleaned}" if not cleaned.startswith("log.") else cleaned


def render_metric_creation(spec: AlertSpec) -> str:
    """Markdown guide + a starter OpenPipeline `value_metric` processor per missing
    metric. It is *not* a standalone Terraform file (the processor block drops into
    an existing logs pipeline's `processing { processors { ... } }`), so it is
    emitted as Markdown with HCL snippets — keeping it out of `terraform validate`
    on the detector module beside it."""
    missing = missing_metrics(spec)
    if not missing:
        return ""
    L = [f"# Metric creation for `{spec.name}`", "",
         "These metrics are referenced by the alert but are **not** Dynatrace metrics, so the anomaly "
         "detector won't fire until they exist. Create each via OpenPipeline — paste the `processor` "
         "into a `dynatrace_openpipeline_v2_logs_pipelines` pipeline's `processing { processors { ... } }` "
         "block, then point the detector at the new `metric_key`. **Review the matcher and source "
         "field** — the numeric value must be present on a matching log record.", ""]
    for i, (metric, _det) in enumerate(missing):
        key = _safe_key(metric)
        # one `dimensions` block containing one `dimension` entry per group field
        dims = ""
        if spec.group_by:
            entries = "\n".join(
                f'''      dimension {{
        extraction_type   = "field"
        strategy          = "equals"
        source_field_name = "{g}"
      }}''' for g in spec.group_by)
            dims = f'''
    dimensions {{
{entries}
    }}'''
        L.append(f"## `{metric}` → `{key}`")
        L.append("")
        L.append("```hcl")
        L.append(f'''processor {{
  type        = "valueMetric"
  id          = "create_{re.sub(r"[^a-z0-9_]", "_", key)}_{i}"
  description = "Create {key} (was Elastic {metric}) for the migrated alert"
  matcher     = "true"   // TODO: scope to the records carrying this value
  value_metric {{
    metric_key    = "{key}"
    field         = "{metric}"   // TODO: the log field holding the numeric value{dims}
  }}
  enabled = true
}}''')
        L.append("```")
        L.append("")
    return "\n".join(L)


def check_metrics(spec: AlertSpec, report) -> None:
    """Fold the existence check into the alert's report (called during translate)."""
    for metric, _ in missing_metrics(spec):
        report.warn(f"Metric `{metric}` is not a Dynatrace metric (no `dt.*`); the detector won't fire "
                    "until it exists. `e2d alert --terraform` also emits an OpenPipeline `value_metric` "
                    "processor to create it — review its matcher and source field.")
    _report_dropped_fields(spec, report)


def _report_dropped_fields(spec: AlertSpec, report) -> None:
    """Flag fields the KQL translator demoted to full-text search so the reader
    knows an OpenPipeline extractor is a prereq for the exact-match behaviour
    the source alert had."""
    seen = set()
    for f, _v in getattr(spec, "dropped_fields", []) or []:
        if f in seen:
            continue
        seen.add(f)
        report.manual(
            f"Field `{f}` is not a Dynatrace built-in log attribute; the filter fell back to "
            f"`matchesPhrase(content, ...)` on the log body. **Prereq:** add an OpenPipeline "
            f"processor to extract `{f}` from the log body so the exact-match filter (and a "
            "long-term metric on this alert) can be restored.")


def render_field_extractors(spec: AlertSpec) -> str:
    """OpenPipeline field-extractor + value-metric starter for each dropped
    field: extract it from the log body, then optionally emit a metric with
    the alert's filter as the matcher so the alert can become metric-based."""
    dropped = getattr(spec, "dropped_fields", []) or []
    if not dropped:
        return ""
    # unique in first-seen order, keeping one sample value per field for the matcher
    order, samples = [], {}
    for f, v in dropped:
        if f not in samples:
            order.append(f); samples[f] = v
    metric_key = _safe_key(_san_metric_from_alert(spec.name))
    matcher_terms = " and ".join(
        f'matchesPhrase(content, "{s}")' for s in samples.values() if s
    ) or "true"
    L = [f"# OpenPipeline prerequisites for `{spec.name}`", "",
         "The KQL filter references field(s) that don't exist in Dynatrace out of the box, so the "
         "converter fell back to a full-text `matchesPhrase(content, ...)` search on the log body. "
         "To restore an exact-field match — and to move this alert onto a **metric** long-term — "
         "add these OpenPipeline steps to the ingest pipeline that carries the source logs "
         "(`dynatrace_openpipeline_v2_logs_pipelines`, `processing { processors { ... } }`).",
         "",
         "## 1. Extract each field",
         ""]
    for i, f in enumerate(order):
        safe = re.sub(r"[^a-z0-9_]", "_", f.lower())
        L.append(f"### `{f}`")
        L.append("")
        L.append("```hcl")
        L.append(f'''processor {{
  type        = "fieldsAdd"
  id          = "extract_{safe}_{i}"
  description = "Extract {f} from log body"
  matcher     = "true"   // TODO: narrow to the log source that carries this field
  fields_add {{
    field {{
      name  = "{f}"
      value = "{{{{ /* TODO: DPL expression, e.g. matchesJsonPath(content, \\"$.{f}\\") */ }}}}"
    }}
  }}
  enabled = true
}}''')
        L.append("```")
        L.append("")
    L.append("## 2. Emit a metric so this alert can be metric-based")
    L.append("")
    L.append("Once the fields exist, publish a metric whose matcher is the alert's filter. The "
             "alert then reads a metric time-series (cheaper, faster, and Davis picks it up as a "
             "first-class signal) instead of counting logs.")
    L.append("")
    L.append("```hcl")
    dims = ""
    if spec.group_by or order:
        entries = "\n".join(
            f'''      dimension {{
        extraction_type   = "field"
        strategy          = "equals"
        source_field_name = "{g}"
      }}''' for g in (spec.group_by or order)
        )
        dims = f'''
    dimensions {{
{entries}
    }}'''
    L.append(f'''processor {{
  type        = "valueMetric"
  id          = "emit_{metric_key.replace('.', '_')}"
  description = "Emit {metric_key} whenever the alert's filter matches"
  matcher     = "{matcher_terms}"
  value_metric {{
    metric_key  = "{metric_key}"
    value       = "1"          // one point per matching record; count() over the metric{dims}
  }}
  enabled = true
}}''')
    L.append("```")
    L.append("")
    L.append(f"After the metric exists, rewrite the detector DQL as:")
    L.append("")
    L.append("```dql")
    by = ", by:{" + ", ".join(spec.group_by) + "}" if spec.group_by else ""
    L.append(f"timeseries {metric_key} = sum({metric_key}){by}, interval:1m")
    L.append("```")
    L.append("")
    return "\n".join(L)


def _san_metric_from_alert(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "alert"
