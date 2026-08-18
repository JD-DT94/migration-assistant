"""Migrate + live verify integration."""

import json
from unittest.mock import patch

import pytest

from e2d.api.client import VerifySweepResult, _iter_dql_artifacts, run_verify_sweep
from e2d.migrate import run_migration, render_report
from e2d.score import report_payload


def test_iter_dql_artifacts_pipeline_and_detectors(tmp_path):
    (tmp_path / "pipelines").mkdir()
    pipeline = [{
        "schemaId": "builtin:openpipeline.logs.pipelines",
        "value": {
            "processing": {
                "processors": [
                    {"id": "p001", "type": "dql", "dql": {"script": "fieldsAdd x = 1"}},
                    {"id": "p002", "type": "drop", "matcher": "true"},
                ]
            }
        },
    }]
    (tmp_path / "pipelines" / "syslog.pipeline.json").write_text(
        json.dumps(pipeline), encoding="utf-8")

    (tmp_path / "alerts").mkdir()
    detectors = [{
        "schemaId": "builtin:davis.anomaly-detectors",
        "value": {
            "analyzer": {
                "input": [{"key": "query", "value": "timeseries avg=avg(dt.service.request.count)"}],
            }
        },
    }]
    (tmp_path / "alerts" / "cpu.detectors.json").write_text(
        json.dumps(detectors), encoding="utf-8")

    labels = dict(_iter_dql_artifacts(str(tmp_path)))
    assert "pipelines/syslog.pipeline.json#proc:p001" in labels
    assert "alerts/cpu.detectors.json#detector:0" in labels
    assert "fieldsAdd x = 1" in labels["pipelines/syslog.pipeline.json#proc:p001"]


def test_iter_dql_artifacts_multi_section_dql(tmp_path):
    (tmp_path / "queries").mkdir()
    text = "# line one\nfetch logs | limit 1\n\n# line two\nfetch spans | limit 2\n"
    (tmp_path / "queries" / "multi.dql").write_text(text, encoding="utf-8")
    labels = dict(_iter_dql_artifacts(str(tmp_path)))
    assert "queries/multi.dql#section:line one" in labels
    assert "queries/multi.dql#section:line two" in labels


@patch("e2d.api.client.verify_dql")
def test_run_verify_sweep_records_invalid(mock_verify, tmp_path):
    from e2d.api.client import VerifyResult

    (tmp_path / "q.dql").write_text("fetch logs | limit 1", encoding="utf-8")
    mock_verify.return_value = VerifyResult("fetch logs | limit 1", False,
                                            errors=["Parse error"])

    results, counts = run_verify_sweep(str(tmp_path), "https://env", "token")
    assert counts["invalid"] == 1
    assert results[0].valid is False
    assert "Parse error" in results[0].errors


@patch("e2d.api.client.run_verify_sweep")
def test_migrate_verify_updates_report(mock_sweep, tmp_path):
    mock_sweep.return_value = (
        [VerifySweepResult("queries/q.dql", "fetch logs", False, ["bad syntax"])],
        {"total": 1, "ok": 0, "invalid": 1, "skipped": 0, "empty": 0},
    )
    indir = tmp_path / "in"
    outdir = tmp_path / "out"
    indir.mkdir()
    outdir.mkdir()
    (indir / "simple_query.json").write_text(
        json.dumps({"query": {"match_all": {}}}), encoding="utf-8")

    summary = run_migration(str(indir), str(outdir), verify=True,
                            env_url="https://env", token="tok")
    assert summary.verify_summary["invalid"] == 1
    report = render_report(summary)
    assert "Live DQL verification" in report
    assert "Invalid queries" in report

    payload = report_payload(summary)
    assert payload["verify_summary"]["invalid"] == 1
    assert payload["verify_results"][0]["valid"] is False


def test_migrate_heal_only(tmp_path):
    indir = tmp_path / "in"
    outdir = tmp_path / "out"
    indir.mkdir()
    (indir / "simple_query.json").write_text(
        json.dumps({"query": {"match_all": {}}}), encoding="utf-8")
    summary = run_migration(str(indir), str(outdir), heal=True)
    payload = json.loads((outdir / "migration_report.json").read_text())
    assert "healing_applied" in payload


def test_apply_verify_to_items_bumps_status():
    from e2d.migrate import MigrationSummary, Item, _apply_verify_to_items

    summary = MigrationSummary()
    summary.items.append(Item("dashboard", "dash.ndjson", "OK",
                              ["dashboards/my_dash.json"], []))
    summary.verify_results = [
        VerifySweepResult("dashboards/my_dash.json#tile:t1", "fetch logs | bad", False, ["err"]),
    ]
    _apply_verify_to_items(summary)
    assert summary.items[0].status == "REVIEW"
    assert any("Live DQL verify failed" in n for n in summary.items[0].notes)
