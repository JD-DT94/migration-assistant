"""DQL auto-healer rules and artifact write-back."""

import json

from e2d.dql.heal import heal_dql, heal_output_dir, patch_artifact_dql
from e2d.dql.validate import lint_dql


def test_heal_by_without_braces():
    dql = "fetch logs | summarize count(), by: service.name"
    healed, acts = heal_dql(dql)
    assert "by: {service.name}" in healed
    assert any(a.code == "by-without-braces" for a in acts)
    assert lint_dql(healed) == []


def test_heal_array_arithmetic():
    dql = ('fetch logs\n| makeTimeseries {total = count(), errors = countIf(loglevel == "ERROR")}, '
           'interval: 30m\n| fieldsAdd error_rate = errors / total')
    healed, acts = heal_dql(dql)
    assert "errors[]" in healed and "total[]" in healed
    assert any(a.code == "array-arithmetic" for a in acts)
    assert not any(f.code == "array-arithmetic" for f in lint_dql(healed))


def test_heal_wrong_function_names():
    dql = "fetch logs | fieldsAdd x = toLowercase(content), n = length(content)"
    healed, acts = heal_dql(dql)
    assert "lower(" in healed and "stringLength(" in healed
    assert not any(f.code == "wrong-function-name" for f in lint_dql(healed))


def test_heal_static_list_brackets():
    dql = 'fetch logs | filter in(host.name, ["a", "b"])'
    healed, acts = heal_dql(dql)
    assert 'in(host.name, {"a", "b"})' in healed
    assert not any(f.code == "static-list-brackets" for f in lint_dql(healed))


def test_heal_assignment_in_filter():
    dql = 'fetch logs | filter host.name = "A"'
    healed, acts = heal_dql(dql)
    assert 'host.name == "A"' in healed
    assert not any(f.code == "assignment-in-filter" for f in lint_dql(healed))


def test_heal_output_dir_writes_dql(tmp_path):
    (tmp_path / "queries").mkdir()
    bad = "fetch logs | summarize count(), by: service.name\n"
    (tmp_path / "queries" / "q.dql").write_text(bad, encoding="utf-8")
    acts = heal_output_dir(tmp_path)
    assert acts
    fixed = (tmp_path / "queries" / "q.dql").read_text(encoding="utf-8")
    assert "by: {service.name}" in fixed


def test_heal_block_comments_preserve_content():
    dql = "fetch logs | fieldsAdd x = /* important note */ 0"
    healed, acts = heal_dql(dql)
    assert "// important note" in healed
    assert any(a.code == "block-comment" for a in acts)


def test_heal_block_comment_multiline():
    dql = "fetch logs\n| fieldsAdd x = /* line1\nline2 */ 0"
    healed, acts = heal_dql(dql)
    assert "// line1 line2" in healed


def test_heal_by_multiple_fields():
    dql = "fetch logs | summarize count(), by: service.name, host.name"
    healed, acts = heal_dql(dql)
    assert "by: {service.name, host.name}" in healed
    assert lint_dql(healed) == []


def test_heal_percentile_no_interval():
    dql = "timeseries p95 = percentile(my.metric, 95)"
    healed, acts = heal_dql(dql)
    assert "rollup: avg" in healed


def test_heal_array_arithmetic_spacing():
    dql = ('fetch logs\n| makeTimeseries {total = count()}, interval: 30m\n'
           '| fieldsAdd x = total / 2')
    healed, acts = heal_dql(dql)
    assert "total[]" in healed
    assert "total[] /" in healed or "total[]/" in healed


def test_heal_verify_error_function_rename():
    dql = "fetch logs | fieldsAdd x = toLowercase(content)"
    healed, acts = heal_dql(dql, verify_errors=["Unknown function: toLowercase"])
    assert "lower(" in healed
    # lint-based fixer catches it first; verify-error is a fallback
    assert any(a.code in ("wrong-function-name", "verify-error") for a in acts)


def test_heal_idempotent():
    dql = "fetch logs | summarize count(), by: service.name"
    once, _ = heal_dql(dql)
    twice, acts2 = heal_dql(once)
    assert once == twice
    assert acts2 == []


def test_patch_dashboard_tile(tmp_path):
    dash = {"tiles": {"t1": {"query": "fetch logs | summarize count(), by: service.name"}}}
    path = tmp_path / "dashboards" / "d.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dash), encoding="utf-8")
    patch_artifact_dql(tmp_path, "dashboards/d.json#tile:t1",
                       "fetch logs | summarize count(), by: {service.name}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert "by: {service.name}" in doc["tiles"]["t1"]["query"]
