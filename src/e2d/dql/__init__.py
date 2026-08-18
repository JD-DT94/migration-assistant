"""DQL validation for e2d.

`validate.lint_dql` is an **offline**, rule-based checker: it does not guarantee
a query runs (only the real engine can), but it catches the high-frequency
classes of invalid DQL that a mechanical Elastic->DQL translation produces —
scalar arithmetic on timeseries arrays, deprecated `dt.entity.*` fields, static
lists written with `[]`, missing `by:{}` braces, and so on.

An optional **online** verifier (see `e2d.api.client.verify_dql` and
`run_verify_sweep`) submits queries to the Dynatrace `query:verify` endpoint.
Use `e2d verify out/` after migration, or `e2d migrate --verify` to validate
inline. Use `e2d migrate --heal` to auto-fix known lint patterns on disk.
Results land in `migration_report.json` under `verify_results` and
`healing_applied`.
"""

from e2d.dql.validate import Finding, lint_dql, lint_into_report
from e2d.dql.heal import HEAL_RULES, HealAction, heal_dql, heal_output_dir

__all__ = ["Finding", "lint_dql", "lint_into_report", "HEAL_RULES",
           "HealAction", "heal_dql", "heal_output_dir"]
