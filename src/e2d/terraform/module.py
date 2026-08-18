"""Emit one Terraform child module you can drop into an existing repository.

The previous output wrote a self-contained root module per artifact — each with
its own `terraform {}` block and, for pipelines, its own `provider "dynatrace" {}`.
That is fine in isolation and wrong the moment you copy it into a real
repository: Terraform allows exactly one `required_providers` per module
directory, so several of those folders merged into one config fail to init, and
a `provider` block inside a module is incompatible with `count`, `for_each` and
`depends_on` on the module itself.

So this emits what the Terraform module guidance actually asks for:

* **`versions.tf`** declares `required_providers` — child modules do not inherit
  provider *requirements* from the root, so this must be present.
* **no `provider` block anywhere** — the default provider configuration is
  inherited from whatever root calls the module.
* one directory, one set of files, resources grouped by kind.
* `variables.tf` for the two things a migration actually needs to control:
  a name prefix, and whether detectors are created switched on.
* `outputs.tf` so the caller can reference what was created.
* an `example-root/` that *does* configure the provider, for anyone who just
  wants to apply it standalone.

Detectors default to **disabled**. A migration that turns 300 alerts on the
moment it applies pages people about a system they have not validated yet; the
sequencing guide says keep them off until the wave is signed off, and the
default should agree with the advice.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROVIDER_SOURCE = "dynatrace-oss/dynatrace"
PROVIDER_VERSION = ">= 1.70.0"
REQUIRED_TERRAFORM = ">= 1.3.0"


def hcl_str(value: str) -> str:
    """Quote a value as an HCL string, guarding interpolation sequences."""
    if value is None:
        return '""'
    text = str(value)
    if "\n" in text:
        # heredocs do not interpolate when the marker is quoted
        return "<<-'EOT'\n" + text + "\nEOT"
    return json.dumps(text).replace("${", "$${").replace("%{", "%%{")


def _replace_hcl_dql(body: str, old: str, new: str) -> str:
    """Swap one DQL string inside an already-rendered resource body."""
    old_hcl, new_hcl = hcl_str(old), hcl_str(new)
    if old_hcl in body:
        return body.replace(old_hcl, new_hcl, 1)
    return body


_MODULE_GITIGNORE = """.terraform/
*.tfstate
*.tfstate.*
.terraform.lock.hcl
terraform.tfvars
"""

_TFVARS_EXAMPLE = """# Copy to terraform.tfvars (gitignored) or pass -var on the CLI.
# name_prefix       = "[migrated] "
# detectors_enabled = false
"""


def ident(name: str, fallback: str = "resource") -> str:
    """A valid, readable Terraform identifier."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", str(name or "")).strip("_").lower()
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        slug = fallback
    if slug[0].isdigit():
        slug = f"r_{slug}"
    return slug[:60]


IDENT_TOKEN = "__IDENT__"


@dataclass
class Resource:
    """One Terraform resource, rendered by its owning converter."""
    type: str                    # e.g. dynatrace_davis_anomaly_detectors
    name: str                    # local identifier, uniquified on add
    body: str                    # the block body, already indented two spaces
    group: str = "main"          # which .tf file it lands in
    comment: str = ""
    # Extra files written next to the .tf (e.g. dashboard JSON). Keys may use
    # IDENT_TOKEN; add() rewrites them to the final resource name.
    files: Dict[str, str] = field(default_factory=dict)
    # Path relative to the migration output dir; after heal, copy that file
    # into every sidecar listed in ``files``.
    refresh_from: str = ""
    # (original_dql, artifact_label) so HCL can be patched from healed files.
    # Labels match ``_iter_dql_artifacts`` (e.g. alerts/x.detectors.json#detector:0).
    dql_slots: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class TerraformModule:
    """Accumulates resources, then writes a valid child module."""
    resources: List[Resource] = field(default_factory=list)
    _used: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def add(self, resource: Resource) -> str:
        """Add a resource, returning the identifier it actually got.

        Two AppD objects can easily reduce to the same slug ("Checkout — slow"
        and "checkout slow"), and duplicate identifiers are a hard Terraform
        error, so collisions get a numeric suffix rather than silently
        overwriting.
        """
        base = ident(resource.name)
        key = f"{resource.type}.{base}"
        if key in self._used:
            self._used[key] += 1
            base = f"{base}_{self._used[key]}"
        else:
            self._used[key] = 1
        resource.name = base
        if IDENT_TOKEN in resource.body:
            resource.body = resource.body.replace(IDENT_TOKEN, base)
        resource.files = {k.replace(IDENT_TOKEN, base): v for k, v in resource.files.items()}
        self.resources.append(resource)
        return base

    def refresh_from_healed(self, out_dir: str) -> None:
        """Copy healed sidecar JSON and patched DQL into resource bodies/files.

        Heal mutates ``dashboards/*.json``, ``*.detectors.json`` and
        ``*.pipeline.json`` on disk. Terraform is generated from in-memory
        bodies, so without this step ``terraform apply`` would ship pre-heal
        queries.
        """
        root = Path(out_dir)
        for r in self.resources:
            if r.refresh_from:
                src = root / r.refresh_from
                if src.is_file():
                    text = src.read_text(encoding="utf-8")
                    if not text.endswith("\n"):
                        text += "\n"
                    r.files = {k: text for k in r.files} if r.files else r.files
        labels = {label for r in self.resources for _, label in r.dql_slots if label}
        if not labels:
            return
        try:
            from e2d.api.client import _iter_dql_artifacts
        except Exception:
            return
        healed = {lab: dql for lab, dql in _iter_dql_artifacts(str(root)) if lab in labels}
        if not healed:
            return
        for r in self.resources:
            if not r.dql_slots:
                continue
            new_slots: List[Tuple[str, str]] = []
            for old, label in r.dql_slots:
                new = healed.get(label, old)
                if new != old:
                    r.body = _replace_hcl_dql(r.body, old, new)
                new_slots.append((new, label))
            r.dql_slots = new_slots

    @property
    def groups(self) -> Dict[str, List[Resource]]:
        out: Dict[str, List[Resource]] = {}
        for r in self.resources:
            out.setdefault(r.group, []).append(r)
        return out

    # -- file rendering ---------------------------------------------------- #

    def versions_tf(self) -> str:
        return (
            "# Provider *requirements* live in the child module; provider\n"
            "# *configuration* does not — it is inherited from the root module that\n"
            "# calls this one. See example-root/ for a working root configuration.\n"
            "terraform {\n"
            f"  required_version = {hcl_str(REQUIRED_TERRAFORM)}\n"
            "  required_providers {\n"
            "    dynatrace = {\n"
            f"      source  = {hcl_str(PROVIDER_SOURCE)}\n"
            f"      version = {hcl_str(PROVIDER_VERSION)}\n"
            "    }\n"
            "  }\n"
            "}\n")

    def variables_tf(self) -> str:
        return '''variable "name_prefix" {
  type        = string
  description = <<-EOT
    Prefixed to every migrated object's title so it is obvious where it came
    from and so it cannot collide with something already in the tenant.
    Set to "" to keep the original names.
  EOT
  default     = "[migrated] "
}

variable "detectors_enabled" {
  type        = bool
  description = <<-EOT
    Whether migrated anomaly detectors are created switched ON.

    Defaults to false on purpose. Applying a migration that immediately enables
    hundreds of detectors pages people about a system nobody has validated yet,
    and Davis needs 7-14 days of data before its baselines are trustworthy.
    Validate a wave, then set this true for it.
  EOT
  default     = false
}
'''

    def outputs_tf(self) -> str:
        lines = ["# Identifiers of everything this module created, for wiring into",
                 "# alerting profiles, ownership config or a downstream module.", ""]
        for group, resources in sorted(self.groups.items()):
            if not resources:
                continue
            width = max(len(r.name) for r in resources)
            entries = "\n".join(
                f"    {r.name.ljust(width)} = {r.type}.{r.name}.id" for r in resources)
            lines += [f'output "{group}_ids" {{',
                      f'  description = "Created {group.replace("_", " ")}, by local name."',
                      "  value = {",
                      entries,
                      "  }",
                      "}", ""]
        return "\n".join(lines)

    def group_tf(self, group: str) -> str:
        blocks = []
        for r in self.groups.get(group, []):
            head = f"# {r.comment}\n" if r.comment else ""
            blocks.append(f'{head}resource "{r.type}" "{r.name}" {{\n{r.body}\n}}')
        return "\n\n".join(blocks) + "\n"

    def example_root_tf(self) -> str:
        return '''# A minimal root configuration for applying the module standalone.
# Copy this next to the module (or point `source` at wherever you put it) and
# run terraform from HERE, not from inside the module directory.
#
#   export DYNATRACE_ENV_URL="https://<env-id>.apps.dynatrace.com"
#   export DYNATRACE_API_TOKEN="dt0c01.XXXX"   # settings, documents, detectors
#   # OpenPipeline, Workflows and platform SLOs also need OAuth or a platform token:
#   #   DT_CLIENT_ID / DT_CLIENT_SECRET / DT_ACCOUNT_ID
#   terraform init && terraform plan

terraform {
  required_version = ">= 1.3.0"
  required_providers {
    dynatrace = {
      source  = "dynatrace-oss/dynatrace"
      version = ">= 1.70.0"
    }
  }
}

# Provider configuration belongs in the root module only.
# DYNATRACE_ENV_URL and DYNATRACE_API_TOKEN are read from the environment.
provider "dynatrace" {}

module "migrated" {
  source = "../"

  name_prefix       = "[migrated] "
  detectors_enabled = false # flip to true per wave, once validated
}

output "migrated" {
  value = module.migrated
}
'''

    def readme(self) -> str:
        groups = ", ".join(f"`{g}.tf`" for g in sorted(self.groups)) or "none"
        n = len(self.resources)
        return f'''# Migrated Dynatrace configuration

This directory **is** the export: a Terraform **child module** with {n} resource(s).
It declares which provider it needs but does not configure one, so it drops into
an existing repository and inherits your provider setup.

## Using it from an existing repository

Copy this directory in (say to `modules/migrated/`) and call it:

```hcl
module "migrated" {{
  source = "./modules/migrated"

  name_prefix       = "[migrated] "
  detectors_enabled = false
}}
```

Then `terraform init` picks up the new module and `terraform plan` shows exactly
what would be created. Nothing here configures a provider, an alias or a
backend, so it will not fight your existing configuration.

If you use several Dynatrace provider configurations (multiple tenants), pass
the one you want explicitly:

```hcl
module "migrated" {{
  source    = "./modules/migrated"
  providers = {{ dynatrace = dynatrace.production }}
}}
```

## Applying it standalone

If you have no Terraform repository yet, `example-root/` is a working root
configuration. Run terraform from inside `example-root/`, not from here — a
child module has no provider configuration and cannot be applied directly.

```bash
export DYNATRACE_ENV_URL="https://<env-id>.apps.dynatrace.com"
export DYNATRACE_API_TOKEN="dt0c01.XXXX"
cd example-root
terraform init && terraform plan
```

## What is in it

Resources are grouped by kind: {groups}.

Dashboard JSON lives in `documents/` and is referenced with `file()` so the
HCL stays readable. After a re-run of the converter with `--heal`, regenerate
this folder so queries stay in sync.

## Two variables worth knowing

- **`name_prefix`** is prepended to every title, so migrated objects are
  identifiable and cannot collide with something already in the tenant.
- **`detectors_enabled`** defaults to **false**. Applying a migration that
  switches on hundreds of detectors at once pages people about a system nobody
  has validated, and Davis needs 7-14 days of data before its baselines can be
  trusted. Validate a wave, then enable it.

## Before you apply

Run `terraform plan` and read it. Thresholds, evaluation windows and entity
scoping are best-effort translations — the migration report lists every one that
needs a human decision, and the plan is the last place to catch them cheaply.
'''

    def write(self, out_dir: str) -> Dict[str, object]:
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        written: List[str] = []

        def put(rel: str, content: str) -> None:
            target = d / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(rel)

        put("versions.tf", self.versions_tf())
        put("variables.tf", self.variables_tf())
        for group in sorted(self.groups):
            put(f"{group}.tf", self.group_tf(group))
        put("outputs.tf", self.outputs_tf())
        put("README.md", self.readme())
        put(".gitignore", _MODULE_GITIGNORE)
        put("example-root/main.tf", self.example_root_tf())
        put("example-root/terraform.tfvars.example", _TFVARS_EXAMPLE)
        for r in self.resources:
            for rel, content in r.files.items():
                put(rel, content)
        return {"dir": str(d), "files": written, "resources": len(self.resources)}
