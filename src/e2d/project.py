"""Project layout: one inbox, one Terraform repo, rebuilt on every convert.

A migration is a *project*, not a one-shot temp folder. Exports accumulate in
``sources/``; Convert rebuilds ``out/`` (and ``out/terraform/``) from that
whole inbox. We never merge HCL across independent runs — leftover resources
would orphan or duplicate. The durable takeaway is the child module at
``out/terraform/``.

This tool never ``git push``es. It prints the path and the copy / init / apply
commands so a human puts the module in *their* repo.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SOURCES = "sources"
OUT = "out"
TERRAFORM = "terraform"
DOT_E2D = ".e2d"


def project_dir() -> Path:
    """Working project: ``E2D_PROJECT_DIR`` if set, otherwise the cwd."""
    override = os.environ.get("E2D_PROJECT_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.cwd()


def sources_dir(root: Optional[Path] = None) -> Path:
    return (root or project_dir()) / SOURCES


def out_dir(root: Optional[Path] = None) -> Path:
    return (root or project_dir()) / OUT


def terraform_dir(out: Optional[Path] = None, root: Optional[Path] = None) -> Path:
    return (out if out is not None else out_dir(root)) / TERRAFORM


def ensure_layout(root: Optional[Path] = None) -> Tuple[Path, Path]:
    """Create ``sources/``, ``out/``, and ``.e2d/`` under the project root."""
    root = Path(root) if root is not None else project_dir()
    src, dest = sources_dir(root), out_dir(root)
    src.mkdir(parents=True, exist_ok=True)
    dest.mkdir(parents=True, exist_ok=True)
    (root / DOT_E2D).mkdir(parents=True, exist_ok=True)
    return src, dest


def list_files(root: Path) -> List[str]:
    """Relative POSIX paths of every file under ``root``, sorted."""
    if not root.is_dir():
        return []
    files = sorted(p for p in root.rglob("*") if p.is_file())
    return [str(p.relative_to(root)).replace("\\", "/") for p in files]


def module_ready(tf: Path) -> bool:
    return tf.is_dir() and any(tf.glob("*.tf"))


def tree_lines(root: Path, *, limit: int = 80) -> List[str]:
    """A compact file listing for the report / GUI (not a full unicode tree)."""
    names = list_files(root)
    if not names:
        return []
    extra = len(names) - limit
    shown = names[:limit]
    lines = [f"{root.name}/"] + [f"  {n}" for n in shown]
    if extra > 0:
        lines.append(f"  … {extra} more")
    return lines


def describe_export(out: Path) -> Dict[str, object]:
    """Facts the CLI, report, and GUI all lead with."""
    tf = terraform_dir(out)
    ready = module_ready(tf)
    path = str(tf.resolve()) if tf.exists() else str(tf)
    files = list_files(tf) if ready else []
    return {
        "ready": ready,
        "path": path if ready else "",
        "files": files,
        "tree": tree_lines(tf) if ready else [],
        "count": len(files),
    }


def describe_project(sources: Path, out: Path,
                     persist: Optional[Path] = None) -> Dict[str, object]:
    """Session payload: inbox + last Terraform export."""
    export = describe_export(out)
    src_files = list_files(sources)
    return {
        "project_dir": str(persist.resolve()) if persist else "",
        "sources_dir": str(sources.resolve()) if sources.exists() else str(sources),
        "sources": src_files,
        "sources_count": len(src_files),
        "terraform_path": export["path"],
        "terraform_files": export["files"],
        "terraform_tree": export["tree"],
        "terraform_ready": export["ready"],
    }


def render_handoff_md(out: Path) -> str:
    """Markdown section that leads ``MIGRATION_REPORT.md``.

    Always names the folder, lists it, and gives copy / git / apply — never a
    push from this tool.
    """
    info = describe_export(out)
    lines: List[str] = ["## Your Terraform repo", ""]
    if not info["ready"]:
        lines += [
            "This run did not write a Terraform child module (JSON-only export, "
            "or nothing convertible). Re-run with Terraform in the export "
            "selector (`--emit both` / `--emit tf`) to get an applyable repo.",
            "",
        ]
        return "\n".join(lines)
    path = info["path"]
    lines += [
        "This folder **is** the complete export — one child module, rebuilt "
        "from every file in this run. Copy it, zip it, or `git init` it; "
        "this tool never pushes to a remote for you.",
        "",
        f"**Path:** `{path}`",
        "",
        "```",
        *info["tree"],
        "```",
        "",
        "### Take it with you",
        "",
        "**Already have a Terraform repo?** Copy the folder in and call it:",
        "",
        "```bash",
        f"cp -R {path} ./modules/migrated",
        "```",
        "",
        "```hcl",
        'module "migrated" {',
        '  source            = "./modules/migrated"',
        '  name_prefix       = "[migrated] "',
        "  detectors_enabled = false",
        "}",
        "```",
        "",
        "Then `terraform init && terraform plan`.",
        "",
        "**No repo yet?** `example-root/` is a working root. Apply from there "
        "(a child module cannot be applied directly):",
        "",
        "```bash",
        f"cd {path}/example-root",
        "terraform init && terraform plan",
        "```",
        "",
        "**Push it somewhere else?** You own the git remote:",
        "",
        "```bash",
        f"cd {path}",
        "git init",
        "git add .",
        'git commit -m "Migrated Dynatrace configuration"',
        "git remote add origin <your-repo>",
        "git push -u origin main",
        "```",
        "",
    ]
    return "\n".join(lines)


def render_handoff_banner(out: Path) -> str:
    """Short stderr banner after `e2d migrate`."""
    info = describe_export(out)
    if not info["ready"]:
        return f"Terraform module -> (not written; see {Path(out) / 'MIGRATION_REPORT.md'})"
    path = info["path"]
    n = info["count"]
    return (
        f"\nTerraform repo  ->  {path}  ({n} file(s))\n"
        f"  copy into an existing repo, or:\n"
        f"    cd {path}/example-root && terraform init && terraform plan\n"
        f"  to push elsewhere: cd {path} && git init && git add . && git commit\n"
        f"  (e2d never git-pushes for you)\n"
    )
