"""Direct upload of dashboards to Dynatrace via the Document Service API.

This is the imperative alternative to the Terraform path. Terraform is generally
preferable (declarative, diffable, idempotent), but `push` is handy for quick
one-off uploads.

API: POST {env}/platform/document/v1/documents  (multipart/form-data)
  form fields : name, type=dashboard
  file part   : content  (the dashboard content JSON)
Auth: Bearer platform token with scope `document:documents:write`.

Because uploads are outward-facing and not reversible in bulk, `push` defaults to
a dry run; pass --apply to actually create documents.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DOC_PATH = "/platform/document/v1/documents"
# Grail DQL validation endpoint — checks a query without executing it.
QUERY_VERIFY_PATH = "/platform/storage/query/v1/query:verify"


def _iter_dashboard_files(input_path: str) -> List[Path]:
    p = Path(input_path)
    if p.is_dir():
        return sorted(q for q in p.glob("*.json"))
    return [p]


def _content_payload(doc: Dict[str, Any],
                     fallback_name: str = "Imported dashboard") -> Tuple[str, Dict[str, Any]]:
    """Return (name, content_object) from either a full dashboard or a content doc.

    Converted files hold only the content document (importable straight into
    the Dashboards app); the display name travels in the filename.
    """
    if "content" in doc and "name" in doc:
        return doc["name"], doc["content"]
    # bare content payload — name comes from the caller (usually the filename)
    return doc.get("name") or fallback_name, doc


# --------------------------------------------------------------------------- #
# online DQL verification (authoritative — uses the real engine)
# --------------------------------------------------------------------------- #

@dataclass
class VerifyResult:
    dql: str
    valid: Optional[bool]            # None => could not check (skipped)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    skipped_reason: Optional[str] = None


@dataclass
class VerifySweepResult:
    """One DQL artifact checked during a verify sweep (migrate or `e2d verify`)."""
    label: str                       # relative path, e.g. dashboards/foo.json#tile:t1
    dql: str
    valid: Optional[bool]            # None => skipped (no creds, network, etc.)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    skipped_reason: Optional[str] = None
    empty: Optional[bool] = None     # True when --data and query returned no rows

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"label": self.label, "valid": self.valid,
                             "errors": self.errors, "warnings": self.warnings}
        if self.skipped_reason:
            d["skipped_reason"] = self.skipped_reason
        if self.empty is not None:
            d["empty"] = self.empty
        return d


def _parse_verify_response(dql: str, status: int, body: Dict[str, Any]) -> VerifyResult:
    """Fold the query:verify response into a VerifyResult.

    The endpoint returns a `valid` flag plus `notifications` carrying a
    `severity` (ERROR/WARNING/...) and `message`. We treat any ERROR notification
    (or an explicit `valid: false`, or a non-2xx status) as invalid.
    """
    notes = body.get("notifications") or []
    errors = [n.get("message", "") for n in notes if str(n.get("severity", "")).upper() == "ERROR"]
    warnings = [n.get("message", "") for n in notes
                if str(n.get("severity", "")).upper() in ("WARNING", "WARN")]
    valid = body.get("valid")
    if valid is None:
        valid = status < 400 and not errors
    valid = bool(valid) and not errors
    return VerifyResult(dql, valid, errors, warnings)


def verify_dql(env_url: str, token: Optional[str], dql: str, timeout: int = 30) -> VerifyResult:
    """Validate one DQL query against the tenant. Best-effort: returns a skipped
    result (valid=None) rather than raising when creds or `requests` are missing."""
    if not env_url or not token:
        return VerifyResult(dql, None, skipped_reason="no env-url/token")
    try:
        import requests
    except ImportError:
        return VerifyResult(dql, None, skipped_reason="requests not installed (pip install ...[push])")
    try:
        resp = requests.post(
            env_url.rstrip("/") + QUERY_VERIFY_PATH,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"query": dql},
            timeout=timeout,
        )
    except Exception as e:  # network failure shouldn't crash a verify sweep
        return VerifyResult(dql, None, skipped_reason=f"request failed: {e}")
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400 and not body:
        return VerifyResult(dql, False, errors=[f"HTTP {resp.status_code}: {resp.text[:200]}"])
    return _parse_verify_response(dql, resp.status_code, body)


_VAR_REF = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*(?::[a-z]+)?")


def _substitute_variables(dql: str) -> str:
    """query:verify cannot resolve dashboard variables — stand in a neutral
    string literal so the query still parses and type-checks."""
    return _VAR_REF.sub('""', dql)


def _strip_variable_filters(dql: str) -> str:
    """For DATA checks, a variable filter substituted with "" would match nothing
    and falsely report an empty tile — drop whole `filter …$Var…` stages instead,
    then neutralise any remaining variable references."""
    parts = dql.split("\n| ")
    kept = [parts[0]] + [p for p in parts[1:]
                         if not (p.lstrip().startswith("filter") and "$" in p)]
    return _substitute_variables("\n| ".join(kept))


QUERY_EXECUTE_PATH = "/platform/storage/query/v1/query:execute"
QUERY_POLL_PATH = "/platform/storage/query/v1/query:poll"


def execute_dql_count(env_url: str, token: str, dql: str,
                      timeout: int = 60) -> Tuple[Optional[int], Optional[str]]:
    """Run a query and return (record_count, error). record_count is capped at 1 —
    the caller only needs 'returns data' vs 'returns nothing'. (None, reason)
    when the check could not run."""
    try:
        import requests
    except ImportError:
        return None, "requests not installed (pip install ...[push])"
    import time as _time
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = requests.post(
            env_url.rstrip("/") + QUERY_EXECUTE_PATH, headers=headers,
            json={"query": dql, "maxResultRecords": 1,
                  "requestTimeoutMilliseconds": min(timeout, 55) * 1000},
            timeout=timeout)
        body = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            return None, f"HTTP {resp.status_code}: {str(body)[:160]}"
        deadline = _time.time() + timeout
        while body.get("state") in ("RUNNING", "NOT_STARTED") and _time.time() < deadline:
            _time.sleep(1)
            resp = requests.post(
                env_url.rstrip("/") + QUERY_POLL_PATH, headers=headers,
                params={"request-token": body.get("requestToken", "")}, timeout=timeout)
            body = resp.json() if resp.content else {}
        if body.get("state") not in (None, "SUCCEEDED"):
            return None, f"query state {body.get('state')}"
        records = (body.get("result") or {}).get("records")
        if records is None:
            return None, "no result in response"
        return len(records), None
    except Exception as e:
        return None, f"request failed: {e}"


def _rel_label(root: Path, f: Path, suffix: str = "") -> str:
    try:
        base = str(f.relative_to(root))
    except ValueError:
        base = f.name
    return f"{base}{suffix}" if suffix else base


def _split_dql_sections(text: str) -> List[Tuple[str, str]]:
    """Split a .dql file that may contain multiple `# source` sections."""
    parts = re.split(r"(?m)^# (.+)$", text.strip())
    if len(parts) <= 1:
        return [("", text.strip())] if text.strip() else []
    out: List[Tuple[str, str]] = []
    if parts[0].strip():
        out.append(("", parts[0].strip()))
    for i in range(1, len(parts), 2):
        title = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if body:
            out.append((title, body))
    return out


def _detector_query(value: Dict[str, Any]) -> Optional[str]:
    analyzer = value.get("analyzer") or {}
    for inp in analyzer.get("input") or []:
        if isinstance(inp, dict) and inp.get("key") == "query":
            q = inp.get("value")
            return q if isinstance(q, str) and q.strip() else None
    return None


def _pipeline_dql_scripts(doc: Any) -> List[Tuple[str, str]]:
    """Extract DQL scripts from OpenPipeline Settings JSON (list or single object)."""
    found: List[Tuple[str, str]] = []
    entries = doc if isinstance(doc, list) else [doc]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        value = entry.get("value") or {}
        procs = ((value.get("processing") or {}).get("processors")) or []
        for proc in procs:
            if not isinstance(proc, dict) or proc.get("type") != "dql":
                continue
            script = (proc.get("dql") or {}).get("script")
            pid = proc.get("id") or "proc"
            if isinstance(script, str) and script.strip():
                found.append((str(pid), script))
    return found


def _iter_dql_artifacts(input_path: str) -> List[Tuple[str, str]]:
    """Collect (label, dql) pairs from converted artifacts under a path.

    Sources: `.dql` files (incl. multi-section querytext output), dashboard tile
    and variable queries, OpenPipeline `.pipeline.json` stage scripts, and Davis
    detector queries in `.detectors.json`.
    """
    p = Path(input_path)
    root = p.parent if p.is_file() else p
    items: List[Tuple[str, str]] = []
    files = [p] if p.is_file() else sorted(root.rglob("*"))
    for f in files:
        if not f.is_file():
            continue
        name = f.name
        if f.suffix == ".dql":
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            for section, body in _split_dql_sections(text):
                suffix = f"#section:{section}" if section else ""
                items.append((_rel_label(root, f, suffix), body))
        elif name.endswith(".pipeline.json"):
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            for pid, script in _pipeline_dql_scripts(doc):
                items.append((_rel_label(root, f, f"#proc:{pid}"), script))
        elif name.endswith(".detectors.json"):
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            entries = doc if isinstance(doc, list) else [doc]
            for i, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                q = _detector_query(entry.get("value") or {})
                if q:
                    items.append((_rel_label(root, f, f"#detector:{i}"), q))
        elif f.suffix == ".json":
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if name.endswith(".pipeline.json") or name.endswith(".detectors.json"):
                continue
            content = doc.get("content", doc) if isinstance(doc, dict) else {}
            if not isinstance(content, dict) or "tiles" not in content:
                continue
            rel = _rel_label(root, f)
            for key, tile in (content.get("tiles") or {}).items():
                q = tile.get("query") if isinstance(tile, dict) else None
                if q:
                    items.append((f"{rel}#tile:{key}", _substitute_variables(q)))
            for var in content.get("variables") or []:
                q = var.get("input") if isinstance(var, dict) else None
                if q:
                    items.append((f"{rel}#var:{var.get('key', '?')}", q))
    return items


def run_verify_sweep(out_dir: str, env_url: Optional[str], token: Optional[str],
                     check_data: bool = False) -> Tuple[List[VerifySweepResult], Dict[str, int]]:
    """Validate every DQL artifact under *out_dir* against a Dynatrace tenant.

    Returns (results, summary_counts) where summary has keys ok, invalid, skipped,
    empty. Never raises — skipped results carry ``valid=None`` and a reason.
    """
    items = _iter_dql_artifacts(out_dir)
    results: List[VerifySweepResult] = []
    counts = {"total": len(items), "ok": 0, "invalid": 0, "skipped": 0, "empty": 0}
    if not items:
        return results, counts
    for label, dql in items:
        res = verify_dql(env_url or "", token, dql)
        if res.valid is None:
            results.append(VerifySweepResult(label, dql, None, res.errors, res.warnings,
                                             res.skipped_reason))
            counts["skipped"] += 1
            continue
        if not res.valid:
            results.append(VerifySweepResult(label, dql, False, res.errors, res.warnings))
            counts["invalid"] += 1
            continue
        empty: Optional[bool] = None
        if check_data and env_url and token:
            count, err = execute_dql_count(env_url, token, _strip_variable_filters(dql))
            if err is None and count == 0:
                empty = True
                counts["empty"] += 1
            else:
                counts["ok"] += 1
        else:
            counts["ok"] += 1
        results.append(VerifySweepResult(label, dql, True, res.errors, res.warnings, empty=empty))
    return results, counts


def verify_cli(args) -> int:
    env_url = getattr(args, "env_url", None) or os.environ.get("DYNATRACE_ENV_URL")
    token = os.environ.get(getattr(args, "token_env", "DT_API_TOKEN"))
    if not env_url or not token:
        print("error: online verify needs --env-url (or DYNATRACE_ENV_URL) and a token "
              f"in {getattr(args, 'token_env', 'DT_API_TOKEN')}", file=sys.stderr)
        return 2

    check_data = getattr(args, "data", False)
    results, counts = run_verify_sweep(args.input, env_url, token, check_data)
    if counts["total"] == 0:
        print(f"No DQL artifacts found at {args.input}", file=sys.stderr)
        return 1

    for vr in results:
        if vr.valid is None:
            print(f"[SKIP ] {vr.label}: {vr.skipped_reason}")
        elif not vr.valid:
            print(f"[BAD  ] {vr.label}: {'; '.join(vr.errors) or 'invalid'}", file=sys.stderr)
        elif vr.empty:
            print(f"[EMPTY] {vr.label}: query is valid but returned no data in the current "
                  "timeframe — the tile will render blank. Check the fields manifest "
                  "(.fields.md) for attributes that may need an OpenPipeline extraction.",
                  file=sys.stderr)
        else:
            extra = f"  ({len(vr.warnings)} warning(s))" if vr.warnings else ""
            if check_data and vr.valid:
                print(f"[OK   ] {vr.label}  (returns data){extra}")
            else:
                print(f"[OK   ] {vr.label}{extra}")

    tail = f", {counts['empty']} valid-but-empty" if check_data else ""
    print(f"\nverified {counts['total']} quer(ies): {counts['ok']} ok, "
          f"{counts['invalid']} invalid{tail}, {counts['skipped']} skipped",
          file=sys.stderr)
    return 1 if (counts["invalid"] or counts["empty"]) else 0


def push_cli(args) -> int:
    env_url = getattr(args, "env_url", None) or os.environ.get("DYNATRACE_ENV_URL")
    if not env_url:
        print("error: provide --env-url or set DYNATRACE_ENV_URL", file=sys.stderr)
        return 2
    env_url = env_url.rstrip("/")
    token = os.environ.get(getattr(args, "token_env", "DT_API_TOKEN"))
    apply = getattr(args, "apply", False)

    files = _iter_dashboard_files(args.input)
    if not files:
        print(f"No .json dashboards found at {args.input}", file=sys.stderr)
        return 1

    if apply and not token:
        print(f"error: token env var '{getattr(args, 'token_env', 'DT_API_TOKEN')}' is empty",
              file=sys.stderr)
        return 2

    requests = None
    if apply:
        try:
            import requests  # noqa: F401
        except ImportError:
            print("error: 'requests' is required for --apply: pip install migration-assistant[push]",
                  file=sys.stderr)
            return 2

    n_ok = n_err = 0
    for f in files:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"[SKIP] {f.name}: invalid JSON ({e})", file=sys.stderr)
            n_err += 1
            continue
        name, content = _content_payload(doc, fallback_name=f.stem)

        if not apply:
            tiles = len(content.get("tiles", {})) if isinstance(content, dict) else "?"
            print(f"[DRY ] would create dashboard '{name}' ({tiles} tiles) from {f.name}")
            n_ok += 1
            continue

        import requests
        url = env_url + DOC_PATH
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            data={"name": name, "type": "dashboard"},
            files={"content": (f.name, json.dumps(content).encode("utf-8"), "application/json")},
            timeout=60,
        )
        if resp.status_code in (200, 201):
            doc_id = ""
            try:
                doc_id = resp.json().get("documentMetadata", {}).get("id", "")
            except Exception:
                pass
            print(f"[OK  ] created '{name}'  id={doc_id}")
            n_ok += 1
        else:
            print(f"[ERR ] '{name}': HTTP {resp.status_code} {resp.text[:200]}", file=sys.stderr)
            n_err += 1

    mode = "applied" if apply else "dry-run"
    print(f"\n{mode}: {n_ok} ok, {n_err} errors", file=sys.stderr)
    return 1 if n_err else 0
