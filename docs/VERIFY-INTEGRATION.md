# Live DQL verify & auto-healing

Canonical reference for humans and AI tools working on validation, healing, CI
gates, and the web GUI verify path in **migration-assistant** (`e2d`).

## Pipeline overview

```
e2d migrate samples/ -o out/ [--heal] [--verify] [--env-url URL] [--data]
  │
  ├─ classify → translate → offline lint (dql/validate.py)
  ├─ write artifacts (dashboards/, queries/, alerts/, pipelines/, terraform/)
  ├─ [optional] heal_output_dir() — deterministic fixes on disk
  ├─ [optional] run_verify_sweep() → query:verify per DQL artifact
  ├─ [optional] re-heal failed queries → re-verify (max 3 rounds)
  └─ MIGRATION_REPORT.md + migration_report.json
```

| Phase | When | Module |
|-------|------|--------|
| Offline lint | During translation | `dql/validate.py` |
| Auto-heal | `--heal` after write (+ between verify rounds) | `dql/heal.py` |
| Live verify | `--verify` (needs tenant creds) | `api/client.py` |

## CLI usage

### Migrate with heal (offline fixes, no tenant needed)

```bash
e2d migrate samples/ -o /tmp/out --heal
```

Fixes known lint patterns on all DQL artifacts and records actions in the report.

### Migrate with heal + live verify (full loop)

```bash
export DYNATRACE_ENV_URL="https://<env-id>.apps.dynatrace.com"
export DT_API_TOKEN="<platform-token>"

e2d migrate samples/ -o /tmp/out --heal --verify
e2d migrate samples/ -o /tmp/out --heal --verify --data --strict
```

Loop (max 3 rounds):

1. Heal all artifacts (offline lint rules)
2. Verify against tenant
3. Heal failed queries using verify errors
4. Re-verify until clean or no progress

### Standalone verify

```bash
e2d verify /tmp/out --env-url "$DYNATRACE_ENV_URL"
```

### CI assess gate

```bash
e2d assess samples/ --heal --verify --json report.json
```

## Auto-healing rules (`dql/heal.py`)

| Code | Fix |
|------|-----|
| `array-arithmetic` | Insert `[]` on timeseries aliases in arithmetic |
| `by-without-braces` | Wrap `by: field` → `by: {field}` |
| `wrong-function-name` | `toLowercase`→`lower`, `toUppercase`→`upper`, `length`→`stringLength` |
| `static-list-brackets` | `in(f, ["a"])` → `in(f, {"a"})` |
| `assignment-in-filter` | Single `=` → `==` in filter stages |
| `percentile-needs-rollup` | Insert `rollup: avg` before `interval:` |
| `block-comment` | `/* … */` → `// …` |
| `verify-error` | Re-apply function renames when verify mentions unknown functions |

Healing **writes back** to converted artifacts:

- `.dql` files (including multi-section querytext output)
- Dashboard JSON tile/variable queries
- OpenPipeline `.pipeline.json` stage scripts
- Davis `.detectors.json` queries

## What gets verified

Same artifact set as healing — see `_iter_dql_artifacts()` in `api/client.py`.

Dashboard `$Variable` references are substituted with `""` before verify.

## API surface

### `run_migration(..., heal=False, verify=False, env_url=None, token=None, verify_data=False)`

Returns `MigrationSummary` with:

| Field | Description |
|-------|-------------|
| `healing_applied` | List of `HealAction` (code, label, message) |
| `verify_results` | List of `VerifySweepResult` |
| `verify_summary` | Counts: total, ok, invalid, skipped, empty |

### `heal_dql(dql, verify_errors=None) -> (str, List[HealAction])`

Pure function — heal one query string.

### `heal_output_dir(out_dir, labels=None, verify_errors=None) -> List[HealAction]`

Scan output directory, heal, write back.

### `run_verify_sweep(out_dir, env_url, token, check_data=False)`

Shared by CLI and migrate; never raises.

## `migration_report.json` schema

```json
{
  "healing_applied": [
    {"code": "by-without-braces", "label": "queries/q.dql", "message": "Wrapped 1 `by:` field list(s) in braces."}
  ],
  "verify_summary": {"total": 12, "ok": 11, "invalid": 0, "skipped": 1, "empty": 0},
  "verify_results": [
    {"label": "dashboards/foo.json#tile:t1", "valid": true, "errors": [], "warnings": []}
  ]
}
```

## Web GUI

The browser WASM build cannot call Dynatrace APIs. The local web server exposes:

| Endpoint | Purpose |
|----------|---------|
| `POST /migrate` | Body may include `heal`, `verify`, `env_url`, `token`, `data` |
| `POST /verify` | Verify converted output for a session (`env_url`, `token`, `data`) |

Example:

```json
POST /verify
X-Session: <session-id>
{"env_url": "https://….apps.dynatrace.com", "token": "…", "data": false}
```

## CI

| Workflow | Purpose |
|----------|---------|
| `.github/workflows/terraform-validate.yml` | `terraform init` + `validate` on generated modules |
| `e2d assess --heal --verify` | Migration quality gate (needs tenant secret in CI) |

## Exit codes

| Command | Non-zero when |
|---------|---------------|
| `migrate --strict` | MANUAL/ERROR items, or verify invalid/empty |
| `migrate --heal` (alone) | never (informational) |
| `verify` | invalid or empty queries |
| `assess --verify` | converter errors, verify failures, or manual work (exit 2) |

## Key files

| File | Role |
|------|------|
| `src/e2d/dql/heal.py` | Auto-fixers + artifact write-back |
| `src/e2d/dql/validate.py` | Offline lint rules |
| `src/e2d/api/client.py` | Live verify + artifact iteration |
| `src/e2d/migrate.py` | `_run_heal_verify_loop`, report sections |
| `src/e2d/score.py` | JSON report payload |
| `src/e2d/cli.py` | `--heal`, `--verify` flags |
| `src/e2d/web/server.py` | `/verify` endpoint |
| `tests/test_dql_heal.py` | Healer unit tests |
| `tests/test_migrate_verify.py` | Migrate+verify integration |

## Dependencies

```bash
pip install -e ".[push,dev]"
```

Live verify requires `[push]` (`requests`). Healing is stdlib-only.

## Not yet implemented

1. **Settings schema pre-validation** — dry-run before detector/pipeline push
2. **Browser UI button for /verify** — endpoint exists; page JS not wired yet
3. **Broader verify-error heal registry** — only function-name patterns today

## Testing

```bash
pytest tests/test_dql_heal.py tests/test_migrate_verify.py tests/test_verify.py -v
pytest tests/test_terraform_module.py -v   # needs terraform CLI
```
