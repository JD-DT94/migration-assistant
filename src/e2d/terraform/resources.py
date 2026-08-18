"""Render converted artifacts as Terraform resource bodies for the child module.

Bodies only — no `terraform {}`, no `provider {}`, no wrapping `resource` line.
`TerraformModule` owns the file layout and the identifiers; these functions own
what goes inside a block. Keeping the split means resource naming, collision
handling and provider requirements are decided in exactly one place.

Titles are wired through `var.name_prefix` and detectors through
`var.detectors_enabled`, so the caller can rename and stage a rollout without
editing generated files.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from e2d.alerts.model import AUTO_ADAPTIVE_ANALYZER, SEASONAL_ANALYZER, STATIC_ANALYZER
from e2d.terraform.module import IDENT_TOKEN, Resource, hcl_str

_ANALYZER = STATIC_ANALYZER  # backward-compatible alias


def _prefixed(title: str) -> str:
    """An HCL expression interpolating the name prefix in front of a title."""
    inner = str(title).replace("\\", "\\\\").replace('"', '\\"')
    inner = inner.replace("${", "$${").replace("%{", "%%{")
    return f'"${{var.name_prefix}}{inner}"'


def _is_numeric(value) -> bool:
    try:
        float(str(value).strip().strip('"'))
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Davis anomaly detectors
# --------------------------------------------------------------------------- #

def detector_resource(spec, det, index: int, source_label: str = "") -> Resource:
    """One `dynatrace_davis_anomaly_detectors` body from a Detector."""
    title = f"{spec.name}: {det.title}"
    raw = str(det.threshold).strip().strip('"')
    numeric = _is_numeric(raw)
    threshold = raw if numeric else "0"
    analyzer = getattr(det, "analyzer", STATIC_ANALYZER) or STATIC_ANALYZER
    auto = analyzer != STATIC_ANALYZER

    description = f"Migrated from {spec.source_kind}"
    if auto:
        description += (" — auto-adaptive baseline; Davis learns from the previous 7 days, "
                        "expect noise in week one")
    elif not numeric:
        description += (f" — threshold was dynamic in the source ({raw}); set a real value "
                        "before enabling")

    fields = [
        ("alertCondition", det.alert_condition),
        ("alertOnMissingData", "false"),
        ("violatingSamples", "3"),
        ("slidingWindow", "5"),
        ("dealertingSamples", "5"),
        ("query", det.query),
    ]
    if auto:
        fields.append(("numberOfSignalFluctuations",
                       str(getattr(det, "signal_fluctuations", "1") or "1")))
    else:
        fields.append(("threshold", threshold))
    inputs = "\n".join(
        f'      analyzer_input_field {{\n'
        f'        key   = {hcl_str(k)}\n'
        f'        value = {hcl_str(v)}\n'
        f'      }}'
        for k, v in fields)

    # A non-numeric static threshold means the detector cannot be correct yet, so
    # it stays off regardless of what the caller sets. Auto-adaptive detectors are
    # valid as generated, so they follow the rollout variable.
    enabled = "var.detectors_enabled" if (auto or numeric) else "false"
    severity = "warning" if det.severity == "warning" else "error"

    body = f'''  title       = {_prefixed(title)}
  description = {hcl_str(description)}
  enabled     = {enabled}
  source      = "Davis Anomaly Detection"

  analyzer {{
    name = {hcl_str(analyzer)}
    input {{
{inputs}
    }}
  }}

  event_template {{
    properties {{
      property {{
        key   = "event.type"
        value = "CUSTOM_ALERT"
      }}
      property {{
        key   = "event.name"
        value = {_prefixed(title)}
      }}
      property {{
        key   = "dt.davis.event.severity_level"
        value = {hcl_str(severity)}
      }}
    }}
  }}

  execution_settings {{
    query_offset = 1
  }}'''

    comment = ("Threshold was dynamic in the source — this detector is pinned off until a "
               "real value is set." if not numeric and not auto else
               "Auto-adaptive baseline — Davis needs ~7 days of metric data before the "
               "baseline is trustworthy." if auto else "")
    return Resource(type="dynatrace_davis_anomaly_detectors",
                    name=f"{spec.name}_{index}" if index else spec.name,
                    body=body, group="detectors", comment=comment,
                    dql_slots=[(det.query, source_label)] if source_label and det.query else [])


# --------------------------------------------------------------------------- #
# OpenPipeline
# --------------------------------------------------------------------------- #

def pipeline_resource(name: str, res, json_rel: str = "") -> Resource:
    """One `dynatrace_openpipeline_v2_logs_pipelines` body."""
    from pathlib import Path
    from e2d.pipelines.tf import _ident

    stem = Path(name).stem
    rn = _ident(stem, "logs")
    lines: List[str] = [
        f"  display_name = {_prefixed(stem[:100])}",
        f"  custom_id    = {hcl_str(('pipeline_' + stem)[:60])}",
        "",
        "  processing {",
        "    processors {",
    ]
    counter = 0
    slots: List[tuple] = []
    for stage in res.stages:
        if stage.kind not in ("dql", "drop"):
            if stage.kind == "manual":
                lines.append(f"      # MANUAL: {stage.description} — add an AppEngine "
                             "function, or drop this step")
            elif stage.description:
                lines.append(f"      # {stage.description}")
            continue
        counter += 1
        ptype = "drop" if stage.kind == "drop" else "dql"
        pid = f"p{counter:03d}_{rn}"[:60]
        desc = stage.description or (stage.dql if stage.kind == "dql"
                                     else "drop matching records")
        lines += [
            "      processor {",
            f'        type        = {hcl_str(ptype)}',
            f'        id          = {hcl_str(pid)}',
            f"        description = {hcl_str(desc[:120])}",
            f"        enabled     = {'true' if stage.enabled else 'false'}",
            f"        matcher     = {hcl_str(stage.matcher)}",
        ]
        if stage.kind == "dql":
            lines += ["        dql {",
                      f"          script = {hcl_str(stage.dql)}",
                      "        }"]
            if json_rel and stage.dql:
                slots.append((stage.dql, f"{json_rel}#proc:{pid}"))
        lines.append("      }")
    lines += ["    }", "  }"]
    return Resource(type="dynatrace_openpipeline_v2_logs_pipelines", name=stem,
                    body="\n".join(lines), group="pipelines", dql_slots=slots)


# --------------------------------------------------------------------------- #
# Request attributes
# --------------------------------------------------------------------------- #

def _aligned(pairs: List[tuple], indent: int) -> List[str]:
    """Assignments with `=` aligned, so the output is already `terraform fmt` clean."""
    if not pairs:
        return []
    width = max(len(k) for k, _ in pairs)
    pad = " " * indent
    return [f"{pad}{k.ljust(width)} = {v}" for k, v in pairs]


def request_attribute_resource(attr) -> Resource:
    """One `dynatrace_request_attribute` body.

    Only HTTP-derived sources reach here. Method rules are excluded upstream
    because the provider requires a return type and visibility the AppD export
    does not carry, and a guessed rule applies cleanly while capturing nothing.
    """
    lines: List[str] = _aligned([
        ("name", _prefixed(attr.name)),
        ("enabled", "true" if attr.enabled else "false"),
        ("data_type", hcl_str(attr.data_type)),
        ("normalization", hcl_str(attr.normalization)),
        ("aggregation", hcl_str(attr.aggregation)),
        ("confidential", "true" if attr.confidential else "false"),
    ], indent=2)

    for src in attr.data_sources:
        lines.append("")
        lines.append("  data_sources {")
        pairs = [("enabled", "true"), ("source", hcl_str(src.get("source", "")))]
        if src.get("parameterName"):
            pairs.append(("parameter_name", hcl_str(src["parameterName"])))
        if src.get("technology"):
            pairs.append(("technology", hcl_str(src["technology"])))
        if src.get("capturingAndStorageLocation"):
            pairs.append(("capturing_and_storage_location",
                          hcl_str(src["capturingAndStorageLocation"])))
        lines += _aligned(pairs, indent=4)

        vp = src.get("valueProcessing")
        if isinstance(vp, dict) and vp.get("valueExtractorRegex"):
            lines.append("")
            lines.append("    value_processing {")
            lines += _aligned([
                ("value_extractor_regex", hcl_str(vp["valueExtractorRegex"])),
                ("trim", "true" if vp.get("trim") else "false"),
            ], indent=6)
            lines.append("    }")
        lines.append("  }")

    return Resource(type="dynatrace_request_attribute", name=attr.name,
                    body="\n".join(lines), group="request_attributes")


# --------------------------------------------------------------------------- #
# Workflows
# --------------------------------------------------------------------------- #

_ACTION_MAP = {
    "email": "dynatrace.automations:send-email-action",
    "webhook": "dynatrace.automations:http-function",
    "slack": "dynatrace.automations:http-function",
    "index": "dynatrace.automations:run-javascript",
    "unknown": "dynatrace.automations:run-javascript",
}


def workflow_resource(spec) -> Optional[Resource]:
    """A `dynatrace_automation_workflow` triggered by this alert's Davis event."""
    from e2d.alerts.model import Action

    actions = spec.actions or [Action("unknown", "notify")]
    lines: List[str] = [
        f"  title = {_prefixed(spec.name + ' (migrated)')}",
        "",
        "  trigger {",
        "    event {",
        "      active = true",
        "      config {",
        "        davis_event {",
        f'          custom_filter = "matchesPhrase(event.name, \\"{spec.name}\\")"',
        "        }",
        "      }",
        "    }",
        "  }",
        "",
        "  tasks {",
    ]
    for i, a in enumerate(actions):
        action = _ACTION_MAP.get(a.kind, _ACTION_MAP["unknown"])
        if a.kind == "email":
            payload = {"to": a.target, "subject": f"[{spec.name}] {{{{event.name}}}}",
                       "body": "Migrated alert. See the event for details."}
        elif a.kind in ("webhook", "slack"):
            payload = {"method": "POST", "url": f"https://{a.target}"}
            if a.secret:
                payload["credentialId"] = "REPLACE_WITH_DYNATRACE_CREDENTIAL_ID"
        else:
            payload = {"note": f"Original action `{a.kind}` — review"}
        inner = ", ".join(f'{k} = {hcl_str(v)}' for k, v in payload.items())
        secret_note = (f"      # set a Dynatrace credential for `{a.secret}`\n"
                       if a.secret else "")
        lines += [
            "    task {",
            f'      name   = {hcl_str(a.kind + "_" + str(i))}',
            f"      action = {hcl_str(action)}",
            secret_note.rstrip("\n") if secret_note else "",
            f"      input  = jsonencode({{ {inner} }})",
            "      position {",
            "        x = 0",
            f"        y = {i}",
            "      }",
            "    }",
        ]
    lines.append("  }")
    body = "\n".join(line for line in lines if line != "")
    return Resource(type="dynatrace_automation_workflow", name=spec.name,
                    body=body, group="workflows")


# --------------------------------------------------------------------------- #
# Dashboards (platform documents)
# --------------------------------------------------------------------------- #

def dashboard_resource(display_name: str, content: Dict[str, Any],
                       json_rel: str = "") -> Resource:
    """One `dynatrace_document` whose content is a sidecar JSON file.

    ``IDENT_TOKEN`` is rewritten to the unique resource name in
    ``TerraformModule.add``, so the ``file()`` path always matches the file we
    write even when two dashboards slug to the same identifier.
    """
    payload = json.dumps(content, indent=2) + "\n"
    body = f'''  type    = "dashboard"
  name    = {_prefixed(display_name)}
  content = file("${{path.module}}/documents/{IDENT_TOKEN}.json")'''
    return Resource(type="dynatrace_document", name=display_name, body=body,
                    group="dashboards",
                    files={f"documents/{IDENT_TOKEN}.json": payload},
                    refresh_from=json_rel)


# --------------------------------------------------------------------------- #
# Platform SLOs
# --------------------------------------------------------------------------- #

def slo_resource(name: str, dql: str, target_pct: Optional[float],
                 window: str = "", dql_rel: str = "") -> Resource:
    """One `dynatrace_platform_slo` with a custom DQL SLI."""
    from e2d.slo import slo_timeframe

    target = f"{float(target_pct):g}" if target_pct is not None else "99"
    timeframe = slo_timeframe(window)
    body = f'''  name        = {_prefixed(name)}
  description = "Migrated from a Kibana SLO"

  criteria {{
    criteria_detail {{
      target         = {target}
      timeframe_from = {hcl_str(timeframe)}
      timeframe_to   = "now"
    }}
  }}

  custom_sli {{
    indicator = {hcl_str(dql)}
  }}'''
    return Resource(type="dynatrace_platform_slo", name=name, body=body, group="slos",
                    dql_slots=[(dql, dql_rel)] if dql_rel and dql else [])


# --------------------------------------------------------------------------- #
# Maintenance windows (AppD schedules)
# --------------------------------------------------------------------------- #

_RANGE_START = "2020-01-01"
_RANGE_END = "2035-12-31"


def maintenance_resource(value: Dict[str, Any]) -> Resource:
    """One `dynatrace_maintenance` from a Settings `builtin:alerting.maintenance-window` value."""
    gp = value.get("generalProperties") or {}
    sched = value.get("schedule") or {}
    name = gp.get("name") or "maintenance"
    kind = (sched.get("scheduleType") or "DAILY").upper()
    block = (sched.get("weeklyRecurrence") or sched.get("dailyRecurrence") or {})
    tw = block.get("timeWindow") or {}
    rng = block.get("recurrenceRange") or {}
    start_date = rng.get("scheduleStartDate") or _RANGE_START
    end_date = rng.get("scheduleEndDate") or _RANGE_END
    start_time = _clock(tw.get("startTime") or "00:00")
    end_time = _clock(tw.get("endTime") or "01:00")
    zone = tw.get("timeZone") or "UTC"
    time_window = (
        "      time_window {\n"
        f"        start_time = {hcl_str(start_time)}\n"
        f"        end_time   = {hcl_str(end_time)}\n"
        f"        time_zone  = {hcl_str(zone)}\n"
        "      }"
    )
    recurrence_range = (
        "      recurrence_range {\n"
        f"        start_date = {hcl_str(start_date)}\n"
        f"        end_date   = {hcl_str(end_date)}\n"
        "      }"
    )
    if kind == "WEEKLY":
        day = block.get("dayOfWeek") or (block.get("days") or ["MONDAY"])[0]
        inner = (
            "    weekly_recurrence {\n"
            f"      day_of_week = {hcl_str(day)}\n"
            f"{recurrence_range}\n"
            f"{time_window}\n"
            "    }"
        )
    else:
        inner = (
            "    daily_recurrence {\n"
            f"{recurrence_range}\n"
            f"{time_window}\n"
            "    }"
        )
    body = f'''  enabled = true
  general_properties {{
    name              = {_prefixed(name)}
    description       = {hcl_str(gp.get("description") or "Migrated from an AppDynamics schedule")}
    type              = {hcl_str(gp.get("maintenanceType") or "PLANNED")}
    disable_synthetic = {"true" if gp.get("disableSyntheticMonitorExecution") else "false"}
    suppression       = {hcl_str(gp.get("suppression") or "DONT_DETECT_PROBLEMS")}
  }}
  schedule {{
    type = {hcl_str(kind if kind in ("DAILY", "WEEKLY", "MONTHLY", "ONCE") else "DAILY")}
{inner}
  }}'''
    return Resource(type="dynatrace_maintenance", name=name, body=body,
                    group="maintenance")


def _clock(value: str) -> str:
    """HH:MM or HH:MM:SS → HH:MM:SS for the maintenance time_window schema."""
    text = str(value or "00:00").strip()
    parts = text.split(":")
    if len(parts) >= 3:
        return f"{int(parts[0]):02d}:{parts[1]}:{parts[2][:2]}"
    if len(parts) == 2:
        return f"{int(parts[0]):02d}:{parts[1]}:00"
    return "00:00:00"
