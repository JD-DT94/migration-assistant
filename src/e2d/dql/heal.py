"""Deterministic DQL auto-healers for known lint and verify failure patterns.

Each fixer is high-confidence and idempotent where possible. Healing runs on
written migration artifacts (dashboard tiles, .dql files, pipeline/detector
JSON) and records every change in ``HealAction`` for ``migration_report.json``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from e2d.dql.validate import _IDENT, _stages, _timeseries_aliases, lint_dql


@dataclass
class HealAction:
    code: str
    label: str
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "label": self.label, "message": self.message}


_FUNC_RENAMES = (
    ("toLowercase", "lower"),
    ("toUppercase", "upper"),
    ("length", "stringLength"),
)

# All healer codes in a stable order — used for --heal-rules filtering.
HEAL_RULES = (
    "array-arithmetic",
    "by-without-braces",
    "wrong-function-name",
    "static-list-brackets",
    "assignment-in-filter",
    "percentile-needs-rollup",
    "block-comment",
    "verify-error",
)


def heal_dql(dql: str, verify_errors: Optional[List[str]] = None,
             rules: Optional[Tuple[str, ...]] = None) -> Tuple[str, List[HealAction]]:
    """Apply known auto-fixes to one DQL string. Returns (healed, actions).

    ``rules`` limits which fixers run (default: all). ``verify_errors`` enables
    the verify-error fallback fixer.
    """
    enabled = set(rules) if rules else set(HEAL_RULES)
    current = dql
    actions: List[HealAction] = []
    fixers = (
        ("array-arithmetic", _heal_array_arithmetic),
        ("by-without-braces", _heal_by_without_braces),
        ("wrong-function-name", _heal_wrong_function_names),
        ("static-list-brackets", _heal_static_list_brackets),
        ("assignment-in-filter", _heal_assignment_in_filter),
        ("percentile-needs-rollup", _heal_percentile_rollup),
        ("block-comment", _heal_block_comments),
        ("block-comment", _heal_unterminated_block_comment),
    )
    for code, fixer in fixers:
        if code not in enabled:
            continue
        current, acts = fixer(current)
        actions.extend(acts)
    if verify_errors and "verify-error" in enabled:
        current, acts = _heal_from_verify_errors(current, verify_errors)
        actions.extend(acts)
    return current, actions


def _heal_array_arithmetic(dql: str) -> Tuple[str, List[HealAction]]:
    stages = _stages(dql)
    arrays = _timeseries_aliases(stages)
    if not arrays:
        return dql, []
    ts_match = re.search(r"(?:make)?[Tt]imeseries\b", dql)
    if not ts_match:
        return dql, []
    head, tail = dql[:ts_match.end()], dql[ts_match.end():]
    actions: List[HealAction] = []
    for alias in dict.fromkeys(arrays):
        bare = alias.strip("`")
        ref = re.escape(bare)
        # bare identifier form
        new_tail = tail
        before = rf"(?<![\w.`]){ref}(?![\w.`(\[])\s*(?=[-+*/])"
        after = rf"([-+*/])\s*(?<![\w.`]){ref}(?![\w.`(\[])"
        if re.search(before, new_tail):
            new_tail = re.sub(before, f"{bare}[]", new_tail)
        if re.search(after, new_tail):
            new_tail = re.sub(after, rf"\1 {bare}[]", new_tail)
        # backticked form
        ticked = f"`{bare}`"
        ref_t = re.escape(ticked)
        before_t = rf"(?<![\w.`]){ref_t}(?![\w.`(\[])\s*(?=[-+*/])"
        after_t = rf"([-+*/])\s*(?<![\w.`]){ref_t}(?![\w.`(\[])"
        if re.search(before_t, new_tail):
            new_tail = re.sub(before_t, f"{ticked}[]", new_tail)
        if re.search(after_t, new_tail):
            new_tail = re.sub(after_t, rf"\1 {ticked}[]", new_tail)
        if new_tail != tail:
            actions.append(HealAction(
                "array-arithmetic", "",
                f"Added `[]` to timeseries alias `{bare}` for element-wise arithmetic."))
            tail = new_tail
    return (head + tail, actions) if actions else (dql, [])


def _heal_by_without_braces(dql: str) -> Tuple[str, List[HealAction]]:
    pat = rf"\bby:\s*(?!\{{)({_IDENT}(?:\s*,\s*{_IDENT})*)"

    def repl(m: re.Match) -> str:
        return f"by: {{{m.group(1)}}}"

    new, n = re.subn(pat, repl, dql)
    if n:
        return new, [HealAction("by-without-braces", "",
                                f"Wrapped {n} `by:` field list(s) in braces.")]
    return dql, []


def _heal_wrong_function_names(dql: str) -> Tuple[str, List[HealAction]]:
    actions: List[HealAction] = []
    current = dql
    for wrong, right in _FUNC_RENAMES:
        if re.search(rf"\b{wrong}\s*\(", current):
            current = re.sub(rf"\b{wrong}\b", right, current)
            actions.append(HealAction(
                "wrong-function-name", "", f"Renamed `{wrong}()` to `{right}()`."))
    return current, actions


def _heal_static_list_brackets(dql: str) -> Tuple[str, List[HealAction]]:
    pat = rf"\bin\s*\(\s*({_IDENT})\s*,\s*\[([^\]]*)\]\s*\)"

    def repl(m: re.Match) -> str:
        return f"in({m.group(1)}, {{{m.group(2)}}})"

    new, n = re.subn(pat, repl, dql)
    if n:
        return new, [HealAction("static-list-brackets", "",
                                f"Changed {n} static `in(field, [..])` to brace list syntax.")]
    return dql, []


def _heal_assignment_in_filter(dql: str) -> Tuple[str, List[HealAction]]:
    stages = _stages(dql)
    changed = False
    out_stages: List[str] = []
    for st in stages:
        if re.match(r"filter(?:Out)?\b", st) and re.search(r"(?<![=!<>:])=(?![=])", st):
            fixed = re.sub(r"(?<![=!<>:])=(?![=])", "==", st)
            if fixed != st:
                changed = True
                st = fixed
        out_stages.append(st)
    if not changed:
        return dql, []
    sep = "\n| " if "\n| " in dql else " | "
    return sep.join(out_stages), [HealAction(
        "assignment-in-filter", "", "Replaced single `=` with `==` in filter stage(s).")]


def _heal_percentile_rollup(dql: str) -> Tuple[str, List[HealAction]]:
    if not re.search(r"(?<![A-Za-z])timeseries\b", dql):
        return dql, []
    if not re.search(r"\b(?:percentile|median|percentRank)\s*\(", dql):
        return dql, []
    if "rollup:" in dql:
        return dql, []
    new, n = re.subn(r"(\btimeseries\b[^|]*?)(\s*,\s*interval:)",
                     r"\1, rollup: avg\2", dql, count=1)
    if n:
        return new, [HealAction("percentile-needs-rollup", "",
                                "Inserted `rollup: avg` into metric timeseries with percentile.")]
    # no interval: — append rollup at end of timeseries command
    new, n = re.subn(r"(\btimeseries\b[^|]*?)(\s*(?:\||$))",
                     r"\1, rollup: avg\2", dql, count=1)
    if n:
        return new, [HealAction("percentile-needs-rollup", "",
                                "Inserted `rollup: avg` into metric timeseries with percentile.")]
    return dql, []


def _heal_block_comments(dql: str) -> Tuple[str, List[HealAction]]:
    if "/*" not in dql:
        return dql, []

    def repl(m: re.Match) -> str:
        inner = m.group(1).replace("\n", " ").strip()
        return f"// {inner}" if inner else "//"

    new = re.sub(r"/\*(.*?)\*/", repl, dql, flags=re.S)
    if new != dql:
        return new, [HealAction("block-comment", "", "Converted block comment(s) to `//` lines.")]
    return dql, []


def _heal_unterminated_block_comment(dql: str) -> Tuple[str, List[HealAction]]:
    """A `/*` without closing `*/` breaks parsing — truncate it to a line comment."""
    idx = dql.find("/*")
    if idx < 0 or "*/" in dql[idx:]:
        return dql, []
    inner = dql[idx + 2:].replace("\n", " ").strip()
    new = dql[:idx] + (f"// {inner}" if inner else "//")
    return new, [HealAction(
        "block-comment", "",
        "Unterminated `/*` comment truncated to `//` (DQL has no block comments).")]


def _heal_from_verify_errors(dql: str, errors: List[str]) -> Tuple[str, List[HealAction]]:
    """Map verify error patterns to deterministic fixes."""
    actions: List[HealAction] = []
    current = dql
    joined = " ".join(errors).lower()

    # unknown function -> rename
    for wrong, right in _FUNC_RENAMES:
        if wrong.lower() in joined and re.search(rf"\b{wrong}\s*\(", current):
            current = re.sub(rf"\b{wrong}\b", right, current)
            actions.append(HealAction(
                "verify-error", "", f"Verify error mentioned `{wrong}` — renamed to `{right}()`."))

    # "unknown field" / "no such field" on a dt.entity.* reference
    if re.search(r"unknown field|no such field|cannot resolve", joined):
        m = re.search(r"dt\.entity\.([\w.]+)", current)
        if m:
            field = m.group(1)
            # best-effort: strip dt.entity. prefix (real field is usually the same name)
            current = re.sub(rf"dt\.entity\.{re.escape(field)}", field, current)
            actions.append(HealAction(
                "verify-error", "",
                f"Verify flagged unknown field `dt.entity.{field}` — stripped deprecated prefix."))

    # parse error near "by:" without braces
    if "by:" in joined and re.search(r"\bby:\s*(?!\{)" + _IDENT, current):
        current, acts = _heal_by_without_braces(current)
        actions.extend(acts)

    return current, actions


def patch_artifact_dql(out_dir: Path, label: str, new_dql: str) -> None:
    """Write healed DQL back into a converted artifact identified by *label*."""
    base, _, frag = label.partition("#")
    path = out_dir / base
    if not frag:
        path.write_text(new_dql.rstrip() + "\n", encoding="utf-8")
        return

    if frag.startswith("section:"):
        section = frag[len("section:"):]
        text = path.read_text(encoding="utf-8")
        parts = re.split(r"(?m)^# (.+)$", text.strip())
        rebuilt: List[str] = []
        if parts[0].strip():
            rebuilt.append(parts[0].strip())
        found = False
        for i in range(1, len(parts), 2):
            title = parts[i].strip()
            body = parts[i + 1].strip() if i + 1 < len(parts) else ""
            if title == section:
                body = new_dql.strip()
                found = True
            block = f"# {title}\n{body}" if body else f"# {title}"
            rebuilt.append(block)
        if not found:
            rebuilt.append(f"# {section}\n{new_dql.strip()}")
        path.write_text("\n\n".join(rebuilt) + "\n", encoding="utf-8")
        return

    doc = json.loads(path.read_text(encoding="utf-8"))

    if frag.startswith("tile:"):
        key = frag[len("tile:"):]
        doc.setdefault("tiles", {})[key]["query"] = new_dql
    elif frag.startswith("var:"):
        key = frag[len("var:"):]
        for var in doc.get("variables") or []:
            if isinstance(var, dict) and var.get("key") == key:
                var["input"] = new_dql
                break
    elif frag.startswith("proc:"):
        pid = frag[len("proc:"):]
        entries = doc if isinstance(doc, list) else [doc]
        for entry in entries:
            procs = ((entry.get("value") or {}).get("processing") or {}).get("processors") or []
            for proc in procs:
                if isinstance(proc, dict) and proc.get("id") == pid:
                    proc.setdefault("dql", {})["script"] = new_dql
        doc = entries if isinstance(doc, list) else doc
    elif frag.startswith("detector:"):
        idx = int(frag[len("detector:"):])
        entries = doc if isinstance(doc, list) else [doc]
        entry = entries[idx]
        for inp in ((entry.get("value") or {}).get("analyzer") or {}).get("input") or []:
            if isinstance(inp, dict) and inp.get("key") == "query":
                inp["value"] = new_dql
        doc = entries if isinstance(doc, list) else doc
    else:
        raise ValueError(f"unknown artifact fragment: {frag}")

    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def heal_output_dir(out_dir: Path,
                    labels: Optional[List[str]] = None,
                    verify_errors: Optional[Dict[str, List[str]]] = None,
                    rules: Optional[Tuple[str, ...]] = None,
                    dry_run: bool = False) -> List[HealAction]:
    """Heal DQL artifacts under *out_dir*.

    When ``dry_run`` is True, compute fixes but do not write files.
    """
    from e2d.api.client import _iter_dql_artifacts

    label_filter = set(labels) if labels else None
    all_actions: List[HealAction] = []
    for label, dql in _iter_dql_artifacts(str(out_dir)):
        if label_filter is not None and label not in label_filter:
            continue
        errs = (verify_errors or {}).get(label)
        healed, actions = heal_dql(dql, verify_errors=errs, rules=rules)
        if healed == dql:
            continue
        if not dry_run:
            patch_artifact_dql(out_dir, label, healed)
        for act in actions:
            act.label = label
            all_actions.append(act)
    return all_actions
