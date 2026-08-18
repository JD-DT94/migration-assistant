"""Watcher doc_count bucket-agg detectors and AppD baseline -> auto-adaptive conversion."""

import json

from e2d.alerts.model import AUTO_ADAPTIVE_ANALYZER, STATIC_ANALYZER
from e2d.alerts.translate import translate_alert
from e2d.appd.health_rules import translate_health_rule
from e2d.dql.validate import lint_dql
from e2d.sinks.dynatrace import detector_settings_value


# --- watcher: doc_count of a filter agg ------------------------------------- #

WATCHER_FILTER_DOC_COUNT = {
    "trigger": {"schedule": {"interval": "5m"}},
    "input": {"search": {"request": {
        "indices": ["logs-*"],
        "body": {
            "query": {"bool": {"must": [{"match": {"service.name": "checkout"}}],
                               "filter": [{"range": {"@timestamp": {"gte": "now-5m"}}}]}},
            "aggs": {
                "error_count": {
                    "filter": {"term": {"log.level": "ERROR"}},
                    "aggs": {"count": {"value_count": {"field": "Records"}}},
                }
            },
        },
    }}},
    "condition": {"compare": {"ctx.payload.aggregations.error_count.doc_count": {"gt": 100}}},
    "actions": {},
}


def test_watcher_filter_agg_doc_count_builds_detector():
    res = translate_alert(WATCHER_FILTER_DOC_COUNT, name="high-errors")
    s = res.spec
    assert s.detectors, "doc_count of a filter agg must produce a detector"
    det = s.detectors[0]
    assert "makeTimeseries error_count = count(), interval: 1m" in det.query
    # the filter agg's predicate is inlined, and the watcher's own filter survives
    assert 'loglevel == "ERROR"' in det.query
    assert 'service.name == "checkout"' in det.query
    assert det.threshold == "100"
    assert det.alert_condition == "ABOVE"
    assert lint_dql(det.query) == []
    # no more "could not build" warning
    assert not any("Could not build an anomaly-detector series" in w.message
                   for w in res.report.warnings)


def test_watcher_terms_agg_doc_count_groups_by_field():
    doc = json.loads(json.dumps(WATCHER_FILTER_DOC_COUNT))
    doc["input"]["search"]["request"]["body"]["aggs"] = {
        "by_service": {"terms": {"field": "service.name", "size": 10}}
    }
    doc["condition"] = {"compare": {"ctx.payload.aggregations.by_service.doc_count": {"gt": 5}}}
    res = translate_alert(doc, name="per-service-count")
    s = res.spec
    assert s.detectors
    det = s.detectors[0]
    assert "makeTimeseries by_service = count(), interval: 1m, by: {service.name}" in det.query
    assert lint_dql(det.query) == []


def test_watcher_unknown_subject_still_warns():
    doc = json.loads(json.dumps(WATCHER_FILTER_DOC_COUNT))
    doc["condition"] = {"compare": {"ctx.payload.aggregations.nonexistent.doc_count": {"gt": 5}}}
    res = translate_alert(doc, name="missing-agg")
    assert res.spec.detectors == []
    assert any("Could not build an anomaly-detector series" in w.message
               for w in res.report.warnings)


# --- AppD baseline -> OOTB or auto-adaptive --------------------------------- #

def _baseline_rule(metric_path, sigmas=3, name="c"):
    return {"name": "Baseline rule", "enabled": True,
            "affects": {"affectedEntityType": "BUSINESS_TRANSACTION_PERFORMANCE"},
            "evalCriterias": {"criticalCriteria": {"conditionAggregationType": "ANY",
                "conditions": [{"name": name, "evalDetail": {
                    "evalDetailType": "SINGLE_METRIC", "metricPath": metric_path,
                    "metricEvalDetail": {"metricEvalDetailType": "BASELINE_TYPE",
                                         "baselineCondition": "GREATER_THAN_BASELINE",
                                         "baselineName": "All Data",
                                         "compareValue": sigmas,
                                         "baselineUnit": "STANDARD_DEVIATIONS"}}}]}}}


def test_baseline_on_builtin_metric_is_covered_out_of_the_box():
    res = translate_health_rule(_baseline_rule("Average Response Time (ms)"))
    assert res.classification == "covered-by-davis"
    assert res.spec.detectors == []
    assert any("covered out of the box" in n for n in res.report.format_deduped())


def test_baseline_on_other_metric_converts_to_auto_adaptive():
    # "GC time spent per min (ms)" maps to a Dynatrace metric but has no built-in
    # Davis coverage — this is the convertible auto-adaptive case.
    res = translate_health_rule(_baseline_rule(
        "Application Infrastructure Performance|web|JVM|GC|GC Time Spent Per Min (ms)",
        sigmas=3))
    assert res.classification == "converted"
    assert len(res.spec.detectors) == 1
    det = res.spec.detectors[0]
    assert det.analyzer == AUTO_ADAPTIVE_ANALYZER
    assert det.signal_fluctuations == "3"
    assert det.threshold == ""
    assert det.metric_key == "dt.runtime.jvm.gc.collection_time"
    assert lint_dql(det.query) == []
    assert any("auto-adaptive" in n and "7 days" in n for n in res.report.format_deduped())


def test_baseline_below_maps_to_below_condition():
    rule = _baseline_rule("Application Infrastructure Performance|web|JVM|GC|GC Time Spent Per Min (ms)")
    cond = rule["evalCriterias"]["criticalCriteria"]["conditions"][0]
    cond["evalDetail"]["metricEvalDetail"]["baselineCondition"] = "LESS_THAN_BASELINE"
    res = translate_health_rule(rule)
    assert res.spec.detectors[0].alert_condition == "BELOW"


def test_auto_adaptive_settings_body_shape():
    res = translate_health_rule(_baseline_rule(
        "Application Infrastructure Performance|web|JVM|GC|GC Time Spent Per Min (ms)"))
    det = res.spec.detectors[0]
    body = detector_settings_value(res.spec.name, det)
    inputs = {i["key"]: i["value"] for i in body["analyzer"]["input"]}
    assert body["analyzer"]["name"] == AUTO_ADAPTIVE_ANALYZER
    assert inputs["numberOfSignalFluctuations"] == "3"
    assert "threshold" not in inputs
    assert inputs["violatingSamples"] == "3"
    assert inputs["slidingWindow"] == "5"
    assert body["enabled"] is True


def test_static_settings_body_unchanged():
    from e2d.alerts.model import Detector
    det = Detector(title="t", query="timeseries x = avg(dt.host.cpu.usage), interval:1m",
                   alert_condition="ABOVE", threshold="90")
    body = detector_settings_value("rule", det)
    inputs = {i["key"]: i["value"] for i in body["analyzer"]["input"]}
    assert body["analyzer"]["name"] == STATIC_ANALYZER
    assert inputs["threshold"] == "90"
    assert "numberOfSignalFluctuations" not in inputs


def test_auto_adaptive_terraform_resource():
    from e2d.terraform.resources import detector_resource
    res = translate_health_rule(_baseline_rule(
        "Application Infrastructure Performance|web|JVM|GC|GC Time Spent Per Min (ms)"))
    det = res.spec.detectors[0]
    r = detector_resource(res.spec, det, 0)
    assert AUTO_ADAPTIVE_ANALYZER in r.body
    assert "numberOfSignalFluctuations" in r.body
    assert "threshold" not in r.body
    assert "enabled     = var.detectors_enabled" in r.body
