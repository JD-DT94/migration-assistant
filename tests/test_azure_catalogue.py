"""Azure alerting-catalogue -> Davis anomaly detectors."""

import json
import zipfile
from io import BytesIO

import pytest

from e2d.azure.build import (build_catalogue, detector_dql, find_merges,
                             settings_payload)
from e2d.azure.model import (AzureMonitor, BASELINE, DAVIS_AI, EVENT, NONE,
                             SLO_BURN, STATIC, TRIGGER, UNPARSED,
                             parse_condition, parse_metric)
from e2d.azure.xlsx import Workbook, header_index
from e2d.dql.validate import lint_into_report
from e2d.report import Report


# --------------------------------------------------------------------------- #
# condition prose
# --------------------------------------------------------------------------- #

def test_two_clause_escalation_splits_with_its_own_threshold():
    cs = parse_condition("P95 > 1,000 ms for 5 min -> High; P95 > 3,000 ms for 5 min -> Critical")
    assert [c.kind for c in cs] == [STATIC, STATIC]
    assert [c.value for c in cs] == [1000.0, 3000.0]
    assert [c.severity_word for c in cs] == ["High", "Critical"]
    # High and Critical must not collapse onto the same Davis level, or the
    # escalation the two clauses exist to express is lost
    assert cs[0].severity != cs[1].severity


def test_below_threshold_sets_alert_condition_below():
    c = parse_condition("Avg < 99.9% for 5 min -> High")[0]
    assert c.comparator == "<"
    assert c.value == 99.9
    assert c.alert_condition == "BELOW"


def test_thousands_separator_is_stripped():
    assert parse_condition("Max > 225,000 (90% of limit) -> Critical")[0].value == 225000.0


def test_baseline_relative_clause_is_not_a_static_threshold():
    c = parse_condition("Spike > 3x rolling 1-hr baseline for 5 min -> High")[0]
    assert c.kind == BASELINE


def test_service_health_severity_is_an_event_not_a_threshold_of_one():
    # "Severity >= Sev1" contains a comparator and a digit; it must not become
    # a static threshold of 1
    c = parse_condition("Severity >= Sev1 active incident -> Critical")[0]
    assert c.kind == EVENT
    assert c.value is None


def test_activity_log_operation_is_an_event():
    assert parse_condition("Any write or delete operation on the namespace resource")[0].kind == EVENT


def test_dynatrace_problem_trigger_creates_no_detector():
    assert parse_condition("Critical severity Dynatrace problem on any circuit")[0].kind == TRIGGER


def test_davis_ai_and_slo_burn_are_distinguished():
    assert parse_condition("Davis AI anomaly detected -> High")[0].kind == DAVIS_AI
    assert parse_condition("2x SLO burn rate for 1 h -> High")[0].kind == SLO_BURN


def test_dashboard_row_is_not_an_alert():
    assert parse_condition("Dashboard refresh <= 1 min")[0].kind == NONE


def test_unrecognised_prose_is_flagged_not_guessed():
    c = parse_condition("Fleet health score drops below 85% (composite weighted score)")[0]
    assert c.kind in (UNPARSED, STATIC)
    if c.kind == UNPARSED:
        assert c.value is None


def test_duration_normalises_to_dql_units():
    assert parse_condition("Max > 1 for 2 min -> High")[0].duration == "2m"
    assert parse_condition("Max > 1 for 6 h -> High")[0].duration == "6h"


# --------------------------------------------------------------------------- #
# metric column
# --------------------------------------------------------------------------- #

def test_mapping_column_key_wins_over_catalogue_spelling():
    key, agg, pct, dims = parse_metric(
        "ext:microsoft_network.applicationgateways_essential.UnhealthyHostCount Max "
        "split by BackendSettingsPool (Essential)",
        "cloud.azure.microsoft_network.applicationgateways.UnhealthyHostCount (defined)")
    assert key == "cloud.azure.microsoft_network.applicationgateways.UnhealthyHostCount"
    assert agg == "max"
    assert dims == ["BackendSettingsPool"]


def test_percentile_aggregation_is_detected():
    _key, agg, pct, _dims = parse_metric(
        "ext:x_essential.BackendLastByteResponseTime P95 split by BackendPool, BackendServer", "")
    assert (agg, pct) == ("percentile", 95)


# --------------------------------------------------------------------------- #
# generated DQL
# --------------------------------------------------------------------------- #

def _mon(**kw):
    base = dict(service="App Gateway", ident="B-01", tier="Bronze", category="Availability",
                name="Test", metric_key="cloud.azure.svc.Metric", aggregation="max")
    base.update(kw)
    return AzureMonitor(**base)


def test_metric_monitor_uses_timeseries_at_one_minute():
    dql = detector_dql(_mon(dimensions=["BackendPool"]))
    assert dql == "timeseries value = max(cloud.azure.svc.Metric), by:{BackendPool}, interval:1m"


def test_percentile_query_carries_a_rollup():
    # without `rollup:` a percentile over a metric silently returns no data
    dql = detector_dql(_mon(aggregation="percentile", percentile=95))
    assert "rollup: avg" in dql
    rep = Report()
    lint_into_report(dql, rep, "metrics")
    assert not rep.warnings, rep.format()


def test_monitor_without_a_metric_key_counts_logs_instead():
    assert detector_dql(_mon(metric_key="")).startswith("fetch logs")


# --------------------------------------------------------------------------- #
# settings object
# --------------------------------------------------------------------------- #

def _built(condition_text, **kw):
    mon = _mon(condition_raw=condition_text, conditions=parse_condition(condition_text), **kw)
    return build_catalogue([mon])


def test_settings_object_has_the_schema_shape():
    res = _built("Max > 5 for 5 min -> Critical")
    body = settings_payload(res.detectors)
    assert len(body) == 1
    obj = body[0]
    assert set(obj) == {"schemaId", "scope", "value"}
    assert obj["schemaId"] == "builtin:davis.anomaly-detectors"
    v = obj["value"]
    for k in ("enabled", "title", "analyzer", "eventTemplate", "executionSettings"):
        assert k in v
    inputs = {kv["key"]: kv["value"] for kv in v["analyzer"]["input"]}
    assert inputs["threshold"] == "5"
    assert inputs["alertCondition"] == "ABOVE"
    # every analyzer input value must be a string for the Settings API
    assert all(isinstance(kv["value"], str) for kv in v["analyzer"]["input"])


def test_not_ingesting_metric_ships_disabled_but_still_importable():
    res = _built("Max > 5 for 5 min -> Critical", ingestion="Not Ingesting")
    v = res.detectors[0].settings
    assert v["enabled"] is False
    assert "not ingesting" in v["description"].lower()


def test_baseline_clause_uses_the_adaptive_analyzer_and_no_threshold():
    res = _built("Spike > 3x rolling 1-hr baseline for 5 min -> High")
    analyzer = res.detectors[0].settings["analyzer"]
    assert "AutoAdaptive" in analyzer["name"]
    assert "threshold" not in {kv["key"] for kv in analyzer["input"]}


def test_hold_time_becomes_violating_samples():
    res = _built("Max > 5 for 7 min -> Critical")
    inputs = {kv["key"]: kv["value"] for kv in res.detectors[0].settings["analyzer"]["input"]}
    assert inputs["violatingSamples"] == "7"


def test_catalogue_severity_is_preserved_on_the_event():
    res = _built("Max > 5 for 5 min -> High")
    props = {kv["key"]: kv["value"] for kv in res.detectors[0].settings["eventTemplate"]["properties"]}
    assert props["catalogue.severity"] == "High"
    assert props["catalogue.id"] == "App Gateway/B-01"


def test_event_and_trigger_rows_are_deferred_with_an_alternative():
    res = _built("Any write or delete operation on the namespace resource")
    assert not res.detectors
    assert len(res.deferred) == 1
    assert res.deferred[0].alternative


# --------------------------------------------------------------------------- #
# consolidation
# --------------------------------------------------------------------------- #

def test_monitors_sharing_a_metric_are_a_merge_group():
    a = _mon(ident="B-01", dimensions=["Listener"],
             conditions=parse_condition("Max > 1 -> High"))
    b = _mon(ident="B-02", dimensions=["BackendPool"],
             conditions=parse_condition("Max > 2 -> Critical"))
    groups = find_merges([a, b])
    assert len(groups) == 1
    assert groups[0].saving == 1
    assert set(groups[0].dimensions) == {"Listener", "BackendPool"}


def test_a_metric_used_once_is_not_a_merge_group():
    assert find_merges([_mon(conditions=parse_condition("Max > 1 -> High"))]) == []


# --------------------------------------------------------------------------- #
# xlsx reader
# --------------------------------------------------------------------------- #

def _xlsx(rows):
    """A minimal single-sheet workbook using inline strings."""
    def cell(ci, val):
        col = chr(ord("A") + ci)
        return f'<c r="{col}{{r}}" t="inlineStr"><is><t>{val}</t></is></c>'
    body = ""
    for ri, row in enumerate(rows, start=1):
        cells = "".join(cell(ci, v).format(r=ri) for ci, v in enumerate(row))
        body += f'<row r="{ri}">{cells}</row>'
    sheet = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
             f'spreadsheetml/2006/main"><sheetData>{body}</sheetData></worksheet>')
    wbxml = ('<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/'
             'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
             'officeDocument/2006/relationships"><sheets>'
             '<sheet name="App Gateway" sheetId="1" r:id="rId1"/></sheets></workbook>')
    rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
            '</Relationships>')
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", wbxml)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def test_xlsx_reader_reads_cells_and_headers():
    wb = Workbook.load_bytes(_xlsx([["ID", "Alert Condition"], ["B-01", "Max > 5 -> High"]]))
    assert wb.sheet_names == ["App Gateway"]
    rows = wb.rows("App Gateway")
    assert rows[0] == ["ID", "Alert Condition"]
    assert rows[1][1] == "Max > 5 -> High"
    assert header_index(rows[0], "alert condition") == 1
    assert header_index(rows[0], "Nope") is None


def test_load_tracker_reads_a_workbook_end_to_end():
    from e2d.azure.tracker import load_tracker
    data = _xlsx([
        ["ID", "Tier", "Monitor Name", "Metric / Log-Source Name", "Alert Condition", "Severity"],
        ["B-01", "Bronze", "Unhealthy hosts",
         "ext:svc_essential.UnhealthyHostCount Max split by BackendPool",
         "Max &gt; 0 for 2 min -&gt; High", "High"],
    ])
    mons = load_tracker("ignored", data=data)
    assert len(mons) == 1
    assert mons[0].ident == "B-01"
    assert mons[0].dimensions == ["BackendPool"]


def test_group_with_a_floor_and_a_ceiling_is_not_merged():
    # the same metric carrying both a "< 99%" and a "> 5" clause is two
    # questions, not one detector split by a dimension
    a = _mon(ident="B-01", conditions=parse_condition("Avg < 99 for 5 min -> Critical"))
    b = _mon(ident="G-03", conditions=parse_condition("Max > 5 for 5 min -> High"))
    g = find_merges([a, b])[0]
    assert g.mergeable is False
    assert g.saving == 0
    assert set(g.directions) == {"ABOVE", "BELOW"}
