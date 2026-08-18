# Live DQL verify integration

This document describes how **live schema validation** is wired into the e2d
migration pipeline. It is the canonical reference for humans and AI tools
working on verify, healing, or CI gates.

## Overview

```
e2d migrate samples/ -o out/ [--verify] [--env-url URL] [--data]
  │
  ├─ classify → translate → offline lint (dql/validate.py)
  ├─ write artifacts (dashboards/, queries/, alerts/, pipelines/, terraform/)
  ├─ [optional] run_verify_sweep() → query:verify per DQL artifact
  └─ MIGRATION_REPORT.md + migration_report.json (includes verify_results)
```

Offline lint runs **during** translation. Live verify runs **after** all
artifacts are written, when `--verify` is passed.

## CLI usage

### Migrate with inline verify

```bash
export DYNATRACE_ENV_URL="https://<env-id>.apps.dynatrace.com"
export DT_API_TOKEN="<platform-token>"

e2d migrate samples/ -o /tmp/out --verify
e2d migrate samples/ -o /tmp/out --verify --data      # also flag empty tiles
e2d migrate samples/ -o /tmp/out --verify --strict    # exit 1 on invalid/empty DQL
```

### Standalone verify (unchanged)

```bash
e2d verify /tmp/out --env-url "$DYNATRACE_ENV_URL"
e2d verify /tmp/out --data
```

### Assess with verify (CI)

```bash
e2d assess samples/ --verify --json report.json
# exit 1 if verify finds invalid or empty queries
```

## What gets verified

`api/client.py` → `_iter_dql_artifacts()` collects DQL from:

| Source | Label pattern |
|--------|---------------|
| `**/*.dql` | `queries/foo.dql` or `queries/foo.dql#section:<title>` |
| Dashboard JSON tiles | `dashboards/foo.json#tile:<key>` |
| Dashboard variables | `dashboards/foo.json#var:<key>` |
| OpenPipeline settings | `pipelines/foo.pipeline.json#proc:<id>` |
| Davis detectors | `alerts/foo.detectors.json#detector:<n>` |

Dashboard variable references (`$Var`) are substituted with `""` before
verify so queries still parse.

## API surface

### `run_migration(..., verify=False, env_url=None, token=None, verify_data=False)`

- **`verify`**: run live validation after conversion
- **`env_url` / `token`**: Dynatrace tenant credentials
- **`verify_data`**: execute valid queries and flag empty results

Returns `MigrationSummary` with:

- `verify_results: List[VerifySweepResult]`
- `verify_summary: Dict` — keys `total`, `ok`, `invalid`, `skipped`, `empty`

### `run_verify_sweep(out_dir, env_url, token, check_data=False)`

Shared by `e2d verify` and `e2d migrate --verify`. Never raises; missing
creds produce `valid=None` with `skipped_reason`.

### `migration_report.json` fields

```json
{
  "verify_summary": { "total": 12, "ok": 10, "invalid": 1, "skipped": 1, "empty": 0 },
  "verify_results": [
    {
      "label": "dashboards/foo.json#tile:t1",
      "valid": false,
      "errors": ["Parse error: ..."],
      "warnings": []
    }
  ]
}
```

## Item status feedback

When verify finds `valid=false`, matching migration items (by output path) get:

- Status bumped to **REVIEW** (via `_worst()`)
- Note appended: `[WARN] Live DQL verify failed (<label>): <errors>`

Unmatched failures appear as a synthetic `verify` category item.

## Exit codes

| Command | Non-zero when |
|---------|---------------|
| `migrate --strict` | MANUAL/ERROR items, or verify invalid/empty |
| `migrate` (default) | never (informational) |
| `verify` | invalid or empty queries |
| `assess --verify` | converter errors, verify invalid/empty, or manual work (exit 2) |

## Dependencies

Live verify requires the optional **`[push]`** extra:

```bash
pip install -e ".[push,dev]"
```

Uses `requests` to call `/platform/storage/query/v1/query:verify`.

## Key files

| File | Role |
|------|------|
| `src/e2d/api/client.py` | `verify_dql`, `_iter_dql_artifacts`, `run_verify_sweep` |
| `src/e2d/migrate.py` | `_run_post_migration_verify`, `_apply_verify_to_items`, report section |
| `src/e2d/score.py` | `verify_results` in `report_payload()` |
| `src/e2d/cli.py` | `--verify`, `--env-url`, `--token-env`, `--data` on migrate/assess |
| `src/e2d/dql/validate.py` | Offline lint (separate from live verify) |
| `tests/test_migrate_verify.py` | Integration tests |

## Not yet implemented (healing roadmap)

These are **out of scope** for the current verify integration but documented
for follow-up work:

1. **`dql/heal.py`** — auto-fix known lint rules (`array-arithmetic`, `by-without-braces`)
2. **Fix-and-revalidate loop** — `convert → lint → heal → verify → re-verify (max N)` behind `--heal`
3. **Web GUI `/verify` endpoint** — server-side verify for the browser UI (WASM is offline-only)
4. **Terraform validate in CI** — `terraform validate` on generated modules
5. **Settings schema pre-validation** — dry-run before detector/pipeline push

## Testing

```bash
pytest tests/test_migrate_verify.py tests/test_verify.py -v
```

Mock `run_verify_sweep` or `verify_dql` for unit tests; use a real tenant only
for manual smoke tests.
