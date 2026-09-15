"""Minimal read-only .xlsx reader (stdlib only).

An xlsx is a zip of XML parts. Everything the tracker needs is a rectangular
grid of strings, so this reads just enough of the format for that: the sheet
index, the shared-string table, and each sheet's cell values. Keeping it
stdlib-only matters twice over — the package declares no dependencies, and the
web GUI runs the same code under Pyodide where a wheel may not be installable.
"""

from __future__ import annotations

import re
import zipfile
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKGREL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

_CELL_REF = re.compile(r"([A-Z]+)(\d+)")


def _col_index(ref: str) -> int:
    """'A' -> 0, 'Z' -> 25, 'AA' -> 26."""
    m = _CELL_REF.match(ref)
    letters = m.group(1) if m else ref
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _text_of(node: ET.Element) -> str:
    """Concatenate every <t> under a node — shared strings may be rich text
    split across several <r> runs."""
    return "".join(t.text or "" for t in node.iter(f"{_MAIN}t"))


def _shared_strings(z: zipfile.ZipFile) -> List[str]:
    try:
        raw = z.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    return [_text_of(si) for si in root.findall(f"{_MAIN}si")]


def _sheet_paths(z: zipfile.ZipFile) -> "Dict[str, str]":
    """Ordered {sheet name: zip path}. The workbook lists names against rIds;
    the rels part maps each rId to the worksheet part."""
    rels: Dict[str, str] = {}
    try:
        rroot = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        for rel in rroot.findall(f"{_PKGREL}Relationship"):
            target = rel.get("Target", "")
            target = target[1:] if target.startswith("/") else target
            if not target.startswith("xl/"):
                target = "xl/" + target.lstrip("./")
            rels[rel.get("Id", "")] = target
    except KeyError:
        pass

    out: Dict[str, str] = {}
    root = ET.fromstring(z.read("xl/workbook.xml"))
    for i, sh in enumerate(root.iter(f"{_MAIN}sheet"), start=1):
        name = sh.get("name") or f"Sheet{i}"
        path = rels.get(sh.get(f"{_REL}id", ""), f"xl/worksheets/sheet{i}.xml")
        out[name] = path
    return out


def _cell_value(c: ET.Element, shared: List[str]) -> str:
    kind = c.get("t")
    if kind == "inlineStr":
        node = c.find(f"{_MAIN}is")
        return _text_of(node) if node is not None else ""
    v = c.find(f"{_MAIN}v")
    if v is None or v.text is None:
        return ""
    if kind == "s":
        try:
            return shared[int(v.text)]
        except (ValueError, IndexError):
            return ""
    return v.text


def _rows(z: zipfile.ZipFile, path: str, shared: List[str]) -> List[List[str]]:
    try:
        raw = z.read(path)
    except KeyError:
        return []
    root = ET.fromstring(raw)
    grid: List[List[str]] = []
    for row in root.iter(f"{_MAIN}row"):
        cells: List[str] = []
        for c in row.findall(f"{_MAIN}c"):
            # cells are sparse: an empty cell is simply absent, so pad to its
            # column index rather than trusting document order alone
            ref = c.get("r")
            idx = _col_index(ref) if ref else len(cells)
            while len(cells) < idx:
                cells.append("")
            cells.append(_cell_value(c, shared))
        grid.append(cells)
    return grid


class Workbook:
    """A whole xlsx read into memory as {sheet name: list of row-lists}."""

    def __init__(self, sheets: "Dict[str, List[List[str]]]"):
        self.sheets = sheets

    @property
    def sheet_names(self) -> List[str]:
        return list(self.sheets)

    def rows(self, name: str) -> List[List[str]]:
        return self.sheets.get(name, [])

    @classmethod
    def load(cls, path: str) -> "Workbook":
        with zipfile.ZipFile(path) as z:
            return cls._from_zip(z)

    @classmethod
    def load_bytes(cls, data: bytes) -> "Workbook":
        import io
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return cls._from_zip(z)

    @classmethod
    def _from_zip(cls, z: zipfile.ZipFile) -> "Workbook":
        shared = _shared_strings(z)
        return cls({name: _rows(z, path, shared)
                    for name, path in _sheet_paths(z).items()})


def header_index(header: List[str], *names: str) -> Optional[int]:
    """Index of the first column whose header matches any of `names`
    (case/whitespace-insensitive). None when absent."""
    norm = [re.sub(r"\s+", " ", (h or "").strip().lower()) for h in header]
    for want in names:
        w = re.sub(r"\s+", " ", want.strip().lower())
        if w in norm:
            return norm.index(w)
    return None
