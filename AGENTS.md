# AGENTS.md

## Cursor Cloud specific instructions

`e2d` (a.k.a. `migration-assistant`) is a **stdlib-only Python CLI** that converts
Elastic/Kibana and AppDynamics artifacts into Dynatrace equivalents (Terraform
module, dashboards, DQL, detectors, pipelines, reports). It also ships a local
web GUI and a browser (WebAssembly/Pyodide) build. Standard commands live in
`README.md` (user-facing) and `pyproject.toml` (`[project.scripts]`, pytest
config); prefer those as the source of truth.

### Environment / running (non-obvious)

- The console scripts (`e2d`, `pytest`) install to `~/.local/bin` via
  `pip install -e .[dev]`. That directory is **not** on the default `PATH` on a
  fresh shell; it is added to `~/.bashrc`, so a normal login shell has it. In a
  non-login/non-interactive shell, either run `export PATH="$HOME/.local/bin:$PATH"`
  or invoke via module form: `python3 -m e2d.cli ...` / `python3 -m pytest`.
- Core is dependency-free stdlib (Python >= 3.9; VM has 3.12). The only pip deps
  are `pytest` (dev) and `requests` (the optional `push` extra, only needed for
  live upload to a real Dynatrace tenant).

### Lint / test / build / run

- **Test:** `pytest tests` (or `PYTHONPATH=src pytest tests -q`, as CI runs it).
- **Lint:** there is no linter configured (no ruff/flake8/black config). Tests
  are the gate; CI (`.github/workflows/pages.yml`) runs the suite before deploy.
- **Build:** no build step for the CLI — it runs from source (editable install).
  The GitHub Pages build bundles `site/index.html` + a zip of `src/e2d` + Pyodide;
  that only happens in CI, not locally.
- **Run CLI:** `e2d migrate samples -o out/` converts the bundled samples into a
  full output tree (`out/terraform/`, dashboards, alerts, pipelines, reports).
- **Run web GUI:** `e2d web --no-browser` serves on `127.0.0.1:8765` (localhost
  only, by design). Flow: `POST /session` -> `POST /upload` (raw body, filenames
  in `X-Filename`/`X-Session` headers) -> `POST /migrate` -> `GET /download/<sid>`.
  The GUI also has a "Paste a query" box that hits `POST /query` for one-off
  ES|QL/DSL/KQL/Lucene -> DQL conversions.

### Test skips (expected)

- ~34 tests skip with "company-data fixtures not present" — they need private
  Elastic/AppD exports deliberately kept out of the repo. This is normal.
- The 2 Terraform tests in `tests/test_terraform_module.py` skip only when the
  `terraform` CLI is absent. Install Terraform (CI pins `1.9.0`) to run them;
  they perform a real `terraform init`/`validate`/`fmt` on generated HCL.
  Terraform is a system dependency, so it is **not** in the startup update
  script — install it manually if you need those two tests.
