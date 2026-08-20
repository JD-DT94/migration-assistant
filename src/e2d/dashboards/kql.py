"""KQL (Kibana Query Language / kuery) -> filter IR -> DQL.

Used for dashboard panel queries (`searchSourceJSON.query`), SLO countIf
predicates, and the KQL inside `filters` aggregations. The parser produces the
same filter IR as Lucene and Query DSL; ``FilterEmitter`` is the only place that
knows DQL operator syntax.

Supported: `field : value`, `field : (a or b)` -> In(), `field : *` -> Exists,
wildcards (`*`) -> Wildcard, ranges (`>`,`>=`,`<`,`<=`) including ES date math,
AND/OR/NOT, parentheses, and bare full-text terms -> Phrase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional, Union

from e2d.config import MappingConfig
from e2d.core.filter_ir import (
    TIME_FIELDS, And, Compare, Exists, In, Node, Not, Or, Phrase, TimeRange,
    Wildcard, emit_filter, strip_keyword,
)
from e2d.report import Report

_DATE_MATH = re.compile(r"^now([-+/]|$)")


# --------------------------------------------------------------------------- #
# tokenizer
# --------------------------------------------------------------------------- #

T_WORD = "word"
T_STRING = "string"
T_COLON = "colon"
T_OP = "op"
T_LP = "lparen"
T_RP = "rparen"


@dataclass
class K:
    type: str
    value: str


def _tokenize(s: str) -> List[K]:
    toks: List[K] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < n:
                if s[j] == "\\" and j + 1 < n:
                    buf.append(s[j + 1])
                    j += 2
                    continue
                if s[j] == '"':
                    j += 1
                    break
                buf.append(s[j])
                j += 1
            toks.append(K(T_STRING, "".join(buf)))
            i = j
            continue
        if c == "(":
            toks.append(K(T_LP, c)); i += 1; continue
        if c == ")":
            toks.append(K(T_RP, c)); i += 1; continue
        if c == ":":
            toks.append(K(T_COLON, c)); i += 1; continue
        if c in "<>":
            if i + 1 < n and s[i + 1] == "=":
                toks.append(K(T_OP, c + "=")); i += 2; continue
            toks.append(K(T_OP, c)); i += 1; continue
        j = i
        while j < n and not s[j].isspace() and s[j] not in '():<>"':
            j += 1
        toks.append(K(T_WORD, s[i:j]))
        i = j
    return toks


# --------------------------------------------------------------------------- #
# parser (KQL -> filter IR)
# --------------------------------------------------------------------------- #

class KqlParser:
    def __init__(self, tokens: List[K], config: MappingConfig,
                 data_object: Optional[str], report: Report):
        self.toks = tokens
        self.pos = 0
        self.config = config
        self.data_object = data_object
        self.report = report

    def _peek(self) -> Optional[K]:
        return self.toks[self.pos] if self.pos < len(self.toks) else None

    def _next(self) -> Optional[K]:
        t = self._peek()
        if t:
            self.pos += 1
        return t

    def _is_kw(self, word: str) -> bool:
        t = self._peek()
        return t is not None and t.type == T_WORD and t.value.lower() == word

    def parse(self) -> Optional[Node]:
        if not self.toks:
            return None
        return self._or()

    def _skip_dup_ops(self) -> None:
        # tolerate authoring typos like `a and AND b` — redundant operators
        while True:
            t = self._peek()
            if t is not None and t.type == T_WORD and t.value.lower() in ("and", "or"):
                self.report.info(f"Skipped redundant KQL operator `{t.value}`.")
                self._next()
            else:
                break

    def _or(self) -> Optional[Node]:
        nodes: List[Node] = []
        left = self._and()
        if left:
            nodes.append(left)
        while self._is_kw("or"):
            self._next()
            self._skip_dup_ops()
            right = self._and()
            if right:
                nodes.append(right)
        if not nodes:
            return None
        return nodes[0] if len(nodes) == 1 else Or(nodes)

    def _and(self) -> Optional[Node]:
        nodes: List[Node] = []
        left = self._not()
        if left:
            nodes.append(left)
        while True:
            if self._is_kw("and"):
                self._next()
                self._skip_dup_ops()
                right = self._not()
            elif self._implicit_and():
                right = self._not()
            else:
                break
            if right:
                nodes.append(right)
        if not nodes:
            return None
        return nodes[0] if len(nodes) == 1 else And(nodes)

    def _implicit_and(self) -> bool:
        t = self._peek()
        if t is None:
            return False
        if t.type in (T_RP,):
            return False
        if t.type == T_WORD and t.value.lower() in ("and", "or"):
            return False
        return t.type in (T_WORD, T_STRING, T_LP)

    def _not(self) -> Optional[Node]:
        if self._is_kw("not"):
            self._next()
            inner = self._primary()
            return Not(inner) if inner is not None else None
        return self._primary()

    def _primary(self) -> Optional[Node]:
        t = self._peek()
        if t is None:
            return None
        if t.type == T_LP:
            self._next()
            inner = self._or()
            if self._peek() and self._peek().type == T_RP:
                self._next()
            return inner
        if t.type in (T_STRING, T_WORD):
            nxt = self.toks[self.pos + 1] if self.pos + 1 < len(self.toks) else None
            if nxt and nxt.type == T_COLON:
                return self._field_match()
            if t.type == T_WORD and nxt and nxt.type == T_OP:
                return self._range()
            self._next()
            return Phrase(t.value)
        self._next()
        return None

    def _field_match(self) -> Optional[Node]:
        field_tok = self._next()
        self._next()  # colon
        field = field_tok.value
        nxt = self._peek()
        if nxt and nxt.type == T_LP:
            return self._value_list(field)
        val = self._next()
        if val is None:
            return None
        if val.type == T_WORD and val.value == "*":
            return Exists(field)
        return self._match_node(field, val.value, val.type == T_STRING)

    def _value_list(self, field: str) -> Optional[Node]:
        self._next()  # '('
        values: List[tuple] = []
        op = "or"
        while True:
            t = self._peek()
            if t is None or t.type == T_RP:
                break
            if t.type == T_WORD and t.value.lower() in ("or", "and"):
                op = t.value.lower()
                self._next()
                continue
            tok = self._next()
            values.append((tok.value, tok.type == T_STRING))
        if self._peek() and self._peek().type == T_RP:
            self._next()
        if not values:
            return None
        if op == "or" and all("*" not in v and "?" not in v for v, _ in values):
            return In(field, [_typed(v, q) for v, q in values])
        nodes = [n for n in (self._match_node(field, v, q) for v, q in values) if n]
        if not nodes:
            return None
        if len(nodes) == 1:
            return nodes[0]
        return Or(nodes) if op == "or" else And(nodes)

    def _range(self) -> Optional[Node]:
        field_tok = self._next()
        op_tok = self._next()
        val = self._next()
        field = field_tok.value
        raw = val.value if val else ""
        quoted = bool(val and val.type == T_STRING)
        if _is_time_range(field, raw):
            bound = {">=": "gte", ">": "gt", "<=": "lte", "<": "lt"}.get(op_tok.value)
            if bound:
                return TimeRange(field=field, **{bound: raw})
        return Compare(field, op_tok.value, _typed(raw, quoted))

    def _match_node(self, field: str, value: str, quoted: bool) -> Node:
        if "*" in value or "?" in value:
            return Wildcard(field, value)
        mapped = self.config.resolve_field(strip_keyword(field), self.data_object)
        if mapped == "content":
            self.report.info("Match on the log body mapped to matchesPhrase(content, ...); "
                             "`==` would require the entire log line to equal the value.")
            return Phrase(value, field=field)
        return Compare(field, "==", _typed(value, quoted))


def _typed(value: str, quoted: bool) -> Union[str, int, float, bool]:
    if quoted:
        return value
    low = value.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return value


def _is_time_range(field: str, raw: str) -> bool:
    if strip_keyword(field) in TIME_FIELDS:
        return True
    return bool(isinstance(raw, str) and _DATE_MATH.match(raw.strip()))


def parse_kql(query: str, config: MappingConfig, data_object: Optional[str],
              report: Report) -> Optional[Node]:
    """Parse a KQL string into filter IR. Empty -> None."""
    if not query or not str(query).strip():
        return None
    tokens = _tokenize(query)
    parser = KqlParser(tokens, config, data_object, report)
    node = parser.parse()
    if parser.pos < len(tokens):
        rest = " ".join(t.value for t in tokens[parser.pos:])
        report.warn(f"KQL query only partially translated; unparsed trailing input `{rest[:60]}` "
                    "was dropped — review.", source=query[:80])
    return node


def translate_kql(query: str, config: MappingConfig, data_object: Optional[str],
                  report: Report) -> str:
    """Translate a KQL string into a DQL boolean expression. Empty -> ''."""
    node = parse_kql(query, config, data_object, report)
    return emit_filter(node, config, data_object, report) if node is not None else ""


def translate_query_string(query: Any, language: Optional[str], config: MappingConfig,
                           data_object: Optional[str], report: Report) -> str:
    """Translate a Kibana query in either language (kuery or lucene) into a DQL
    boolean expression string. Empty/untranslatable -> ''."""
    if not query or not str(query).strip():
        return ""
    if language == "lucene":
        from e2d.core.lucene import translate_lucene
        node = translate_lucene(str(query), config, data_object, report)
        return emit_filter(node, config, data_object, report) if node is not None else ""
    return translate_kql(str(query), config, data_object, report)
