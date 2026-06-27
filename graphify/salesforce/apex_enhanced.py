"""
graphify-sf: Enhanced Apex parser (classes, triggers, SOQL/DML, governor hints).

Regex-based parser for Apex ``*.cls`` / ``*.trigger`` files (ADR-019 — regex
first, tree-sitter deferred to Phase 3). It extracts:

    - The class / trigger definition node (full source kept for later passes).
    - Method signatures (``calls`` edge back to the owning class).
    - SOQL queries (``queries`` edge -> SObject, ``sf_in_loop`` tagged).
    - DML operations (``dml_operates_on`` edge -> SObject, ``sf_in_loop`` tagged).
    - ``implements`` of well-known interfaces (QCP, Database.Batchable hint).

CRITICAL (ADR-002): every SObject node ID is built via ``constants.sobject_nid()``
so the Apex, Flow and Object parsers converge on the same node in
``build_graph()``. Cross-file resolution depends on it.

The ``sf_in_loop`` flags feed the governor-limit analysis pass (Pass SF-4):
SOQL/DML detected inside a ``for``/``while`` body is the signal for
``governor_violation`` diagnostics.

Limitations (ADR-019, accepted): nested classes, generic-type element
resolution, and method overloads are not fully modelled; DML targets are
resolved from light variable-type tracking and fall back to an ``unknown``
SObject (``sf_ambiguous``) when the type cannot be inferred. No method-body AST
analysis — regex only.
"""

from __future__ import annotations

import re
from pathlib import Path

from graphify.salesforce.constants import (
    CPQ_QCP_INTERFACE,
    sobject_nid,
)

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

#: Class / trigger declaration. Anchored on the ``class``/``trigger`` keyword so
#: a trigger at file start (no leading modifier/whitespace) still matches — the
#: naive ``(?:public|private)?\s+`` form misses it.
_DEFINITION_RE = re.compile(r"\b(class|trigger)\s+(\w+)", re.IGNORECASE)

#: Method signature. A visibility modifier is REQUIRED to avoid matching control
#: flow (``else if (...)``, ``for (...)``) as methods. Captures return type,
#: name and the raw parameter list.
_METHOD_RE = re.compile(
    r"\b(public|private|protected|global)\s+"
    r"(?:static\s+)?(?:override\s+)?(?:virtual\s+)?"
    r"(\w+(?:<[^>]+>)?)\s+(\w+)\s*\((.*?)\)"
)

#: SOQL: ``[SELECT ... FROM <SObject> ...]``. Single-line (no DOTALL).
_SOQL_RE = re.compile(r"SELECT\s+.*?\s+FROM\s+(\w+)", re.IGNORECASE)

#: DML keyword followed by the target variable: ``update opps``.
_DML_RE = re.compile(r"\b(insert|update|delete)\s+(\w+)", re.IGNORECASE)

#: for / while loop header (used to bracket loop bodies for in-loop detection).
_LOOP_RE = re.compile(r"(?:for|while)\s*\([^)]*\)\s*\{")

#: Collection declaration: ``List<Opportunity> opps`` -> opps maps to Opportunity.
_COLLECTION_DECL_RE = re.compile(r"(?:List|Set)\s*<\s*(\w+)\s*>\s+(\w+)")

#: Simple declaration / parameter: ``Account acc`` (type must be capitalized).
_SIMPLE_DECL_RE = re.compile(r"\b([A-Z]\w*)\s+(\w+)\s*[;:=)]")

#: Apex / collection types that are not SObjects — excluded from DML resolution.
_NON_SOBJECT_TYPES = {
    "String", "Integer", "Long", "Decimal", "Double", "Boolean", "Id",
    "Date", "Datetime", "Time", "Blob", "Object", "List", "Set", "Map",
    "Trigger", "System", "Database", "void", "SObject",
}


def _strip_comments(source: str) -> str:
    """Blank out ``//`` line and ``/* */`` block comments (incl. ``/** */`` doc).

    Comment characters are replaced with spaces and newlines are preserved, so
    the result is the SAME length with the SAME line breaks as the input — line
    numbers and offsets used by loop / SOQL / DML detection stay accurate. String
    literals are skipped so a ``//`` or ``/*`` inside a string is not treated as
    a comment.

    This prevents comment text from polluting the parse — e.g. a doc comment
    "This class must be kept in sync" no longer makes ``_DEFINITION_RE`` match
    ``class must``, and commented-out SOQL/DML/methods produce no phantom nodes.
    """
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        # String literals — Apex strings are single-quoted; copy verbatim so an
        # embedded // or /* is not mistaken for a comment. \' is an escaped quote.
        if ch == "'":
            out.append(ch)
            i += 1
            while i < n:
                out.append(source[i])
                if source[i] == "\\" and i + 1 < n:
                    out.append(source[i + 1])
                    i += 2
                    continue
                if source[i] == "'":
                    i += 1
                    break
                i += 1
            continue
        # Line comment: blank to end of line, keep the newline.
        if ch == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
            continue
        # Block comment (covers /** doc */): blank through */, preserving newlines.
        if ch == "/" and nxt == "*":
            out.append("  ")
            i += 2
            while i < n and not (source[i] == "*" and i + 1 < n and source[i + 1] == "/"):
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            if i < n:  # consume the closing */
                out.append("  ")
                i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _find_loop_ranges(source: str) -> list[tuple[int, int]]:
    """Return ``(start_line, end_line)`` ranges for each for/while loop body.

    Uses simple brace matching from the loop header's opening ``{`` to its
    matching ``}``. Nested loops yield nested (overlapping) ranges, which is
    fine — a line is "in a loop" if it falls inside any range.
    """
    ranges: list[tuple[int, int]] = []
    for match in _LOOP_RE.finditer(source):
        start_line = source[: match.start()].count("\n") + 1
        depth = 1
        pos = match.end()
        while depth > 0 and pos < len(source):
            ch = source[pos]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            pos += 1
        end_line = source[:pos].count("\n") + 1
        ranges.append((start_line, end_line))
    return ranges


def _in_loop(line_no: int, loop_ranges: list[tuple[int, int]]) -> bool:
    return any(start <= line_no <= end for start, end in loop_ranges)


def _build_var_types(source: str) -> dict[str, str]:
    """Map local/param variable names to their (capitalized) declared type.

    Light, regex-only type tracking to resolve DML targets like ``update opps``
    to a concrete SObject. Collections (``List<Opportunity> opps``) resolve to
    the element type. Best-effort: unresolved variables fall back to ``unknown``.
    """
    var_types: dict[str, str] = {}
    for m in _COLLECTION_DECL_RE.finditer(source):
        var_types[m.group(2)] = m.group(1)
    for m in _SIMPLE_DECL_RE.finditer(source):
        type_name, var_name = m.group(1), m.group(2)
        if type_name in _NON_SOBJECT_TYPES:
            continue
        var_types.setdefault(var_name, type_name)
    return var_types


def _is_sobject_name(name: str) -> bool:
    """Reject tokens that a ``FROM`` clause can name but are not SObject roots.

    A child-relationship subquery (``SELECT Id, (SELECT Id FROM Contacts__r) ...``)
    puts a ``__r`` relationship name after ``FROM`` — that is a traversal, never a
    queryable SObject, so it must not become an ``sobject`` node (real-org label
    mis-extraction; ADR-019). Non-SObject keywords are filtered too.
    """
    return not name.lower().endswith("__r") and name not in _NON_SOBJECT_TYPES


def _detect_soql_objects(source: str) -> list[tuple[str, int, bool]]:
    """Detect SObjects queried via SOQL.

    Returns a list of ``(sobject_name, line_no, in_loop)`` tuples. Child-relationship
    (``__r``) subquery targets are skipped (see ``_is_sobject_name``).
    """
    loop_ranges = _find_loop_ranges(source)
    results: list[tuple[str, int, bool]] = []
    for match in _SOQL_RE.finditer(source):
        sobject = match.group(1)
        if not _is_sobject_name(sobject):
            continue
        line_no = source[: match.start()].count("\n") + 1
        results.append((sobject, line_no, _in_loop(line_no, loop_ranges)))
    return results


def _detect_dml_operations(
    source: str, var_types: dict[str, str]
) -> list[tuple[str, str, int, bool]]:
    """Detect DML operations.

    Returns ``(dml_type, sobject_name, line_no, in_loop)``. ``sobject_name`` is
    resolved from variable-type tracking, or ``"unknown"`` when not inferable.
    """
    loop_ranges = _find_loop_ranges(source)
    results: list[tuple[str, str, int, bool]] = []
    for match in _DML_RE.finditer(source):
        dml_type = match.group(1).upper()
        var_name = match.group(2)
        sobject = var_types.get(var_name, "unknown")
        line_no = source[: match.start()].count("\n") + 1
        results.append((dml_type, sobject, line_no, _in_loop(line_no, loop_ranges)))
    return results


def _ensure_sobject_node(
    nodes: list[dict], sobject_id: str, api_name: str, path: Path
) -> None:
    """Append a stub ``sobject`` node for *sobject_id* if not already present.

    Keeps cross-file edges (queries / dml_operates_on) non-dangling within this
    result; ``build_graph()`` merges the stub with the real object node via the
    shared ``sobject_nid`` (ADR-002, ADR-012).
    """
    if any(n["id"] == sobject_id for n in nodes):
        return
    node: dict = {
        "id": sobject_id,
        "label": api_name,
        "file_type": "sobject",
        "source_file": str(path),
    }
    if api_name == "unknown":
        node["sf_ambiguous"] = True
    nodes.append(node)


def extract_apex_enhanced(path: Path) -> dict:
    """Parse an Apex class / trigger file into graph nodes and edges.

    Returns:
        ``{"nodes": [...], "edges": [...]}``. If no class/trigger declaration is
        found, returns empty lists plus an ``"error"`` key (ADR-009 lenient: the
        caller skips the file and keeps analyzing).
    """
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()

    # Match against a comment-stripped copy so comment text (e.g. a doc comment
    # "This class must be kept in sync") and commented-out code do not pollute
    # the parse. ``scan`` preserves line numbers/offsets; the ORIGINAL ``source``
    # is still stored on the node for the CPQ / governor passes.
    scan = _strip_comments(source)

    nodes: list[dict] = []
    edges: list[dict] = []

    # 1. Class / trigger definition --------------------------------------
    definition = _DEFINITION_RE.search(scan)
    if not definition:
        return {"nodes": [], "edges": [], "error": "No class/trigger found"}

    code_type = definition.group(1).lower()  # "class" | "trigger"
    class_name = definition.group(2)
    class_id = f"apex_{path.stem.lower()}"

    nodes.append(
        {
            "id": class_id,
            "label": class_name,
            "file_type": "code",
            "source_file": str(path),
            "sf_code_type": "trigger" if code_type == "trigger" else "class",
            "source": source,  # kept for CPQ / governor passes
        }
    )

    # 2. Method signatures ------------------------------------------------
    for method in _METHOD_RE.finditer(scan):
        return_type, method_name, params = (
            method.group(2),
            method.group(3),
            method.group(4),
        )
        method_id = f"{class_id}_{method_name.lower()}"
        nodes.append(
            {
                "id": method_id,
                "label": f"{method_name}({params})",
                "file_type": "code",
                "source_file": str(path),
                "sf_return_type": return_type,
                "sf_method_type": "method",
            }
        )
        edges.append(
            {
                "source": method_id,
                "target": class_id,
                "relation": "calls",
                "confidence": "EXTRACTED",
                "source_file": str(path),
            }
        )

    # 3. SOQL queries (cross-file resolution) -----------------------------
    for sobject_name, line_no, in_loop in _detect_soql_objects(scan):
        target_id = sobject_nid(sobject_name)
        _ensure_sobject_node(nodes, target_id, sobject_name, path)
        edges.append(
            {
                "source": class_id,
                "target": target_id,
                "relation": "queries",
                "confidence": "EXTRACTED",
                "sf_in_loop": in_loop,
                "source_location": f"L{line_no}",
                "source_file": str(path),
            }
        )

    # 4. DML operations ---------------------------------------------------
    var_types = _build_var_types(scan)
    for dml_type, sobject_name, line_no, in_loop in _detect_dml_operations(
        scan, var_types
    ):
        target_id = sobject_nid(sobject_name)
        _ensure_sobject_node(nodes, target_id, sobject_name, path)
        edge: dict = {
            "source": class_id,
            "target": target_id,
            "relation": "dml_operates_on",
            "sf_dml_type": dml_type,
            "confidence": "INFERRED",
            "sf_in_loop": in_loop,
            "confidence_value": 0.85 if in_loop else 0.9,
            "source_location": f"L{line_no}",
            "source_file": str(path),
        }
        if sobject_name == "unknown":
            edge["sf_ambiguous"] = True
        edges.append(edge)

    # 5. Implements (QCP / Batchable hint) --------------------------------
    if (
        f"implements {CPQ_QCP_INTERFACE}" in scan
        or "implements QuoteCalculatorPlugin" in scan
    ):
        qcp_id = "sbqq_quotecalculatorplugin"
        if not any(n["id"] == qcp_id for n in nodes):
            nodes.append(
                {
                    "id": qcp_id,
                    "label": CPQ_QCP_INTERFACE,
                    "file_type": "code",
                    "source_file": str(path),
                    "sf_code_type": "interface",
                }
            )
        edges.append(
            {
                "source": class_id,
                "target": qcp_id,
                "relation": "implements",
                "confidence": "EXTRACTED",
                "source_file": str(path),
            }
        )

    if "implements Database.Batchable" in scan:
        nodes[0]["sf_async_pattern"] = "batchable"

    return {"nodes": nodes, "edges": edges}
