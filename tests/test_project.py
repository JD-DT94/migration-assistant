"""Project layout: accumulate sources, rebuild terraform/, never git-push."""

from pathlib import Path

from e2d.project import (describe_export, ensure_layout, project_dir,
                         render_handoff_banner, render_handoff_md)


def test_project_dir_respects_env(tmp_path, monkeypatch):
    monkeypatch.setenv("E2D_PROJECT_DIR", str(tmp_path))
    assert project_dir() == tmp_path.resolve()


def test_ensure_layout_creates_inbox_and_out(tmp_path):
    src, out = ensure_layout(tmp_path)
    assert src == tmp_path / "sources"
    assert out == tmp_path / "out"
    assert src.is_dir() and out.is_dir()
    assert (tmp_path / ".e2d").is_dir()


def test_handoff_md_leads_with_path_and_never_pushes(tmp_path):
    tf = tmp_path / "terraform"
    tf.mkdir()
    (tf / "versions.tf").write_text("# stub\n", encoding="utf-8")
    (tf / "example-root").mkdir()
    (tf / "example-root" / "main.tf").write_text("# root\n", encoding="utf-8")
    md = render_handoff_md(tmp_path)
    assert "## Your Terraform repo" in md
    assert str(tf.resolve()) in md
    assert "never pushes" in md
    assert 'module "migrated"' in md
    assert "git init" in md
    assert "example-root" in md
    assert "terraform plan" in md
    banner = render_handoff_banner(tmp_path)
    assert str(tf.resolve()) in banner
    assert "never git-pushes" in banner


def test_handoff_when_no_module(tmp_path):
    md = render_handoff_md(tmp_path)
    assert "did not write a Terraform child module" in md
    info = describe_export(tmp_path)
    assert info["ready"] is False
    assert info["path"] == ""
