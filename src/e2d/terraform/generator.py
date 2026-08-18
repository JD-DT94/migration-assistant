"""Emit a Terraform child module for converted dashboards.

`e2d dashboard --terraform` uses the same `TerraformModule` layout as `e2d migrate`
so the two paths do not disagree: no provider block in the child, documents as
sidecar JSON, `example-root/` as the standalone apply entry.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from e2d.terraform.module import TerraformModule
from e2d.terraform.resources import dashboard_resource


def generate_terraform(dashboards: List[Tuple[str, Dict[str, Any]]], out_dir: str) -> Dict[str, Any]:
    """dashboards: list of (display_name, dashboard_dict). Returns a summary."""
    module = TerraformModule()
    for display_name, dashboard in dashboards:
        content = dashboard.get("content", dashboard) if isinstance(dashboard, dict) else dashboard
        module.add(dashboard_resource(display_name, content))
    return module.write(out_dir)
