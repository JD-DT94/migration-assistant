---
name: migration-upgrade
description: Upgrade e2d so the primary export is a usable Terraform child module. Use when improving Terraform coverage, unifying generators, writing TF after heal, fixing Settings/HCL schema, or making the GUI/docs Terraform-first (visual, functional, conversion scenarios).
---

# Upgrade the Terraform export

The product converts Elastic/Kibana and AppDynamics config into Dynatrace. **Ship a usable, exportable Terraform repo** — one child module at `out/terraform/` — not a pile of JSON that someone must re-encode by hand.

JSON, Markdown guides, and live push stay as supporting paths.

## Before changing code

1. Read `.cursor/agents/migration-reviewer.md` (intent, checklist, constraints).
2. Trace the kind you are upgrading: translator → disk artifacts → `TerraformModule.add(...)` → `write()` after heal.
3. Confirm the Dynatrace provider resource and schema (do not invent attributes). Prefer resources already used in-repo:
   - `dynatrace_document` (platform dashboards)
   - `dynatrace_davis_anomaly_detectors`
   - `dynatrace_openpipeline_v2_logs_pipelines`
   - `dynatrace_automation_workflow`
   - `dynatrace_request_attribute`
   - `dynatrace_platform_slo` (Grail custom SLI)
   - `dynatrace_maintenance` (`builtin:alerting.maintenance-window`)

## Child-module rules (do not regress)

- `versions.tf` declares `required_providers` only — **no `provider` block**
- Unique identifiers via `ident()`; collisions get a numeric suffix
- Titles through `var.name_prefix`; detectors through `var.detectors_enabled` (default `false`)
- `example-root/main.tf` is the only place that configures `provider "dynatrace" {}`
- Extra files (dashboard JSON) live under the module (`documents/<final-id>.json`) and `file()` uses that **final** id (`__IDENT__` replaced in `TerraformModule.add`)
- Write the module **after** heal/verify so HCL matches healed DQL (`refresh_dql` / sidecar `refresh_from`)

## Upgrade recipe (per artifact kind)

1. **Can it apply?** If the provider has a resource and the translation is semantically real, emit it. If not, keep a guided `.md` and say so in the report.
2. **Add a body builder** in `src/e2d/terraform/resources.py` returning `Resource(group=...)`.
3. **Call it from the migrate handler** when `summary.tf_module is not None` and `emit in ("tf", "both")`. Append `"terraform/"` to the item's outputs.
4. **Keep Settings JSON in sync** with the same schema the resource wraps (e.g. TimeWindow needs `startTime` **and** `endTime`, not `durationMinutes`; weekly recurrence is **one day per object**).
5. **After heal**, refresh:
   - Sidecar JSON: `Resource.refresh_from` → copy into `Resource.files`
   - Inline DQL: `Resource.dql_slots = [(original, label)]` where `label` matches `_iter_dql_artifacts` (`#detector:N`, `#proc:id`, `#tile:key`)
6. **Tests** in `tests/test_terraform_module.py` (structure + `terraform validate` when CLI exists) and a focused kind test.
7. **UX**: if this kind was previously “upload JSON / paste in UI”, update `plan.py`, emitted README, and GUI/site deploy copy so Terraform is the first route.

## Visual / export upgrades

- Hero and “How to deploy” lead with `terraform/` (copy module or `example-root`)
- GUI: full `converted.zip` plus `terraform-module.zip` when the module exists
- Keep `site/index.html` and `src/e2d/web/server.py` copy in agreement (tests parametrize both)

## Conversion-coverage upgrades

Prioritize shapes that already translate but never become HCL, or that emit JSON the Settings API/provider will reject:

- Dashboards (Kibana + AppD) → `dynatrace_document`
- Kibana custom-KQL SLOs → `dynatrace_platform_slo` with `makeTimeseries` SLI
- AppD schedules → valid maintenance windows + Terraform
- Watcher/`doc_count` and AppD `BASELINE_TYPE` already have converters; do not re-invent OOTB vs `--baseline-detectors`

Do **not** invent entity filters, static thresholds for baselines, or lookalike metrics.

## Verify locally

```bash
pip install -e ".[dev]"
e2d migrate samples/ -o /tmp/e2d-out --heal
test -f /tmp/e2d-out/terraform/versions.tf
terraform -chdir=/tmp/e2d-out/terraform/example-root init -backend=false
terraform -chdir=/tmp/e2d-out/terraform/example-root validate
pytest tests/test_terraform_module.py tests/test_appd_schedules.py tests/test_gui_views.py -q
```
