"""
graphify-sf: Tree-sitter Apex parser (classes, triggers, SOQL/DML, governor hints).

AST-based parser for Apex ``*.cls`` / ``*.trigger`` files, built on the vendored
``tree_sitter_apex`` grammar (from ``aheber/tree-sitter-sfapex``). It replaces the
former regex parser (``apex_enhanced``), producing the SAME node/edge shapes so
the downstream CPQ / governor / OoE passes and the LWC / Flow / profile parsers
(which hand-build ``apex_<class>`` ids) are unaffected — see ADR-002, ADR-019.

It extracts:

    - The class / trigger / interface definition node (full source kept for later passes).
    - Method signatures (``method_of`` membership edge back to the owning class).
    - Apex -> Apex method calls (``calls`` edges; intra-file resolved here,
      cross-file deferred to ``apex_calls.resolve_apex_calls``).
    - SOQL queries (``queries`` edge -> SObject, ``sf_in_loop`` tagged).
    - DML operations (``dml_operates_on`` edge -> SObject, ``sf_in_loop`` tagged).
    - ``implements`` of well-known interfaces (QCP, Database.Batchable hint).

CRITICAL (ADR-002): every SObject node ID is built via ``constants.sobject_nid()``
so the Apex, Flow and Object parsers converge on the same node in ``build_graph()``.

The ``sf_in_loop`` flags feed the governor-limit analysis pass (Pass SF-4):
SOQL/DML detected inside a ``for``/``while`` body is the signal for
``governor_violation`` diagnostics. Loop membership is an ancestor walk over the
AST (any ``for_statement`` / ``enhanced_for_statement`` / ``while_statement`` /
``do_statement`` ancestor) rather than the former brace-counting heuristic.

Unlike the regex parser this needs no comment stripping: tree-sitter parses
comments as ``comment`` / ``*_comment`` nodes that the named-node walk never
visits, so doc comments and commented-out code cannot pollute the parse.

The grammar is an optional dependency (``graphify-sfdx[apex]``). When it is not
installed, ``extract_apex(... )`` returns an ``error`` result so the caller skips
the file (ADR-009 lenient), exactly like the core ``_extract_generic`` path.
"""

from __future__ import annotations

from pathlib import Path

from graphify.salesforce.constants import (
    CPQ_QCP_INTERFACE,
    sobject_nid,
)

# ---------------------------------------------------------------------------
# Grammar loading (optional dependency)
# ---------------------------------------------------------------------------

#: Cached (Language, ok) so we build the parser language only once per process.
_LANGUAGE = None
_LOAD_ERROR: str | None = None


def _get_language():
    """Load and cache the tree-sitter Apex ``Language``; return ``None`` on failure.

    Failure (grammar not installed, version mismatch) is non-fatal: the caller
    degrades to an ``error`` result and skips the file (ADR-009).
    """
    global _LANGUAGE, _LOAD_ERROR
    if _LANGUAGE is not None or _LOAD_ERROR is not None:
        return _LANGUAGE
    try:
        import tree_sitter_apex
        from tree_sitter import Language

        _LANGUAGE = Language(tree_sitter_apex.language())
    except ImportError:
        _LOAD_ERROR = "tree_sitter_apex not installed (pip install graphify-sfdx[apex])"
    except Exception as e:  # pragma: no cover - defensive (version mismatch etc.)
        _LOAD_ERROR = f"tree_sitter_apex failed to load: {e}"
    return _LANGUAGE


# ---------------------------------------------------------------------------
# Node-type constants (confirmed against the sfapex grammar; see
# docs/salesforce/apex-treesitter-node-types.md)
# ---------------------------------------------------------------------------

_LOOP_TYPES = frozenset(
    {"for_statement", "enhanced_for_statement", "while_statement", "do_statement"}
)

#: Apex / collection types that are not SObjects — excluded from DML resolution.
_NON_SOBJECT_TYPES = frozenset(
    {
        "String", "Integer", "Long", "Decimal", "Double", "Boolean", "Id",
        "Date", "Datetime", "Time", "Blob", "Object", "List", "Set", "Map",
        "Trigger", "System", "Database", "void", "SObject",
    }
)

#: DML keywords matched for parity with the regex parser (insert/update/delete).
_DML_KEYWORDS = frozenset({"insert", "update", "delete"})


# ---------------------------------------------------------------------------
# Small AST helpers
# ---------------------------------------------------------------------------

def _text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _line(node) -> int:
    return node.start_point[0] + 1


def _named_child_of_type(node, type_name: str):
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _first_identifier(node, source: bytes) -> str | None:
    """Return the text of the first direct ``identifier`` child, if any."""
    for c in node.children:
        if c.type == "identifier":
            return _text(c, source)
    return None


def _in_loop(node) -> bool:
    """True if any ancestor of *node* is a for/while/do loop."""
    parent = node.parent
    while parent is not None:
        if parent.type in _LOOP_TYPES:
            return True
        parent = parent.parent
    return False


def _walk(node):
    """Yield every node in the subtree (pre-order)."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        # reversed so children are visited left-to-right
        stack.extend(reversed(n.children))


# ---------------------------------------------------------------------------
# Type resolution (for DML targets) — light, AST-based
# ---------------------------------------------------------------------------

def _type_element_name(type_node, source: bytes) -> str | None:
    """Resolve a type node to its (SObject) element name.

    ``Account`` -> ``Account``; ``List<Opportunity>`` / ``Set<Opportunity>`` ->
    ``Opportunity`` (the generic element). Returns ``None`` when the node is not
    a simple/collection type we can resolve.
    """
    if type_node is None:
        return None
    t = type_node.type
    if t in ("type_identifier", "scoped_type_identifier"):
        return _text(type_node, source)
    if t == "generic_type":
        # First type_identifier is the container (List/Set/Map); the element type
        # lives in the type_arguments child.
        args = _named_child_of_type(type_node, "type_arguments")
        if args is not None:
            for c in args.children:
                if c.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
                    # For Map<K,V> this takes the first arg; DML on a Map is not
                    # meaningful, so first-arg is an acceptable best-effort.
                    return _type_element_name(c, source)
    return None


def _build_var_types(root, source: bytes) -> dict[str, str]:
    """Map local/parameter variable names to their declared (SObject) type.

    Mirrors the regex parser's light type tracking, but reads declared types from
    the AST: ``local_variable_declaration`` and ``formal_parameter`` nodes.
    Collections resolve to their element type. Non-SObject scalar/collection
    types are skipped. Best-effort: unresolved variables fall back to ``unknown``
    at the DML site.
    """
    var_types: dict[str, str] = {}
    for n in _walk(root):
        if n.type == "local_variable_declaration":
            type_node = n.children[0] if n.children else None
            resolved = _type_element_name(type_node, source)
            if resolved is None or resolved in _NON_SOBJECT_TYPES:
                continue
            for c in n.children:
                if c.type == "variable_declarator":
                    name = _first_identifier(c, source)
                    if name:
                        var_types.setdefault(name, resolved)
        elif n.type == "formal_parameter":
            # formal_parameter: <type> <identifier>
            type_node = n.children[0] if n.children else None
            resolved = _type_element_name(type_node, source)
            if resolved is None or resolved in _NON_SOBJECT_TYPES:
                continue
            name = _first_identifier(n, source)
            if name:
                var_types.setdefault(name, resolved)
    return var_types


# ---------------------------------------------------------------------------
# SObject name filtering (shared semantics with the former regex parser)
# ---------------------------------------------------------------------------

def _is_sobject_name(name: str) -> bool:
    """Reject ``__r`` relationship names and non-SObject keywords as FROM targets.

    A child-relationship subquery (``SELECT Id, (SELECT Id FROM Contacts__r) ...``)
    names a relationship after ``FROM``; that is a traversal, never a queryable
    SObject (ADR-019). We additionally detect subqueries structurally, but keep
    this guard so a relationship name never becomes an ``sobject`` node.
    """
    return not name.lower().endswith("__r") and name not in _NON_SOBJECT_TYPES


def _soql_from_sobject(query_body, source: bytes) -> str | None:
    """Read the root SObject name from a ``soql_query_body``'s ``from_clause``."""
    from_clause = _named_child_of_type(query_body, "from_clause")
    if from_clause is None:
        return None
    storage = _named_child_of_type(from_clause, "storage_identifier")
    if storage is not None:
        ident = _first_identifier(storage, source)
        if ident:
            return ident
    # Fallback: some FROM targets are a bare identifier / scoped name.
    for c in from_clause.children:
        if c.type in ("identifier", "scoped_type_identifier", "type_identifier"):
            return _text(c, source)
    return None


# ---------------------------------------------------------------------------
# SObject stub nodes
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Definition node helpers (class / interface / trigger)
# ---------------------------------------------------------------------------

def _find_definition(root):
    """Return the first class/interface/trigger declaration node, or ``None``.

    Walks pre-order so the outermost (file-level) declaration is found first,
    matching the former parser's "first declaration wins" behavior.
    """
    for n in _walk(root):
        if n.type in ("class_declaration", "interface_declaration", "trigger_declaration"):
            return n
    return None


def _implements_text(class_node, source: bytes) -> str:
    """Concatenated text of the ``interfaces`` clause (for QCP/Batchable hints)."""
    parts: list[str] = []
    interfaces = _named_child_of_type(class_node, "interfaces")
    if interfaces is not None:
        parts.append(_text(interfaces, source))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_apex(path: Path) -> dict:
    """Parse an Apex class / trigger file into graph nodes and edges.

    Returns:
        ``{"nodes": [...], "edges": [...]}``. On a missing grammar or no
        class/trigger declaration, returns empty lists plus an ``"error"`` key
        (ADR-009 lenient: the caller skips the file and keeps analyzing).
    """
    path = Path(path)

    language = _get_language()
    if language is None:
        return {"nodes": [], "edges": [], "error": _LOAD_ERROR or "apex grammar unavailable"}

    from tree_sitter import Parser

    with open(path, "rb") as f:
        source = f.read()

    parser = Parser(language)
    tree = parser.parse(source)
    root = tree.root_node

    nodes: list[dict] = []
    edges: list[dict] = []

    # 1. Class / trigger / interface definition --------------------------
    definition = _find_definition(root)
    if definition is None:
        return {"nodes": [], "edges": [], "error": "No class/trigger found"}

    decl_type = definition.type
    if decl_type == "trigger_declaration":
        code_type = "trigger"
    elif decl_type == "interface_declaration":
        code_type = "interface"
    else:
        code_type = "class"

    class_name = _first_identifier(definition, source) or path.stem
    class_id = f"apex_{path.stem.lower()}"

    nodes.append(
        {
            "id": class_id,
            "label": class_name,
            "file_type": "code",
            "source_file": str(path),
            "sf_code_type": code_type,
            # ORIGINAL source kept verbatim for CPQ / governor passes.
            "source": source.decode("utf-8", errors="replace"),
        }
    )

    # 2. Method signatures (overload-aware) -------------------------------
    # First collect every method_declaration, grouped by lowercased name, so we
    # can detect overload sets and only disambiguate ids when a name repeats
    # (preserving the apex_<class>_<method> contract lwc/flow/profiles rely on).
    method_decls: list[tuple[object, str]] = []  # (node, method_name)
    name_counts: dict[str, int] = {}
    for n in _walk(root):
        if n.type != "method_declaration":
            continue
        method_name = _method_name(n, source)
        if not method_name:
            continue
        method_decls.append((n, method_name))
        name_counts[method_name.lower()] = name_counts.get(method_name.lower(), 0) + 1

    # Map a (decl node) -> method_id, and build the per-class name -> [method_id]
    # index used for intra-file call resolution. ``method_node_by_decl`` lets the
    # call pass find the caller method node for a given call site.
    method_id_by_decl: dict[int, str] = {}
    methods_by_name: dict[str, list[tuple[str, int]]] = {}  # name -> [(id, arity)]
    for n, method_name in method_decls:
        params = _method_param_text(n, source)
        return_type = _method_return_type(n, source)
        base_id = f"{class_id}_{method_name.lower()}"
        # Disambiguate ONLY when the name is overloaded (>1 in this class). A lone
        # method keeps the bare id so cross-parser links (lwc/flow/profiles) resolve.
        if name_counts[method_name.lower()] > 1:
            sig = _param_type_signature(n, source)
            method_id = f"{base_id}_{sig}" if sig else f"{base_id}_0"
        else:
            method_id = base_id
        arity = _formal_param_count(n)
        method_id_by_decl[n.id] = method_id
        methods_by_name.setdefault(method_name.lower(), []).append((method_id, arity))
        nodes.append(
            {
                "id": method_id,
                "label": f"{method_name}({params})",
                "file_type": "code",
                "source_file": str(path),
                "sf_return_type": return_type,
                "sf_method_type": "method",
                # Method body kept so the recursion-guard scan (governor SF-4,
                # ADR-027) can detect guard patterns on method-level call cycles.
                "source": _text(n, source),
            }
        )
        edges.append(
            {
                # Membership edge: this method belongs to its class (mirrors
                # field_of). NOT a call — real method->method calls use `calls`.
                "source": method_id,
                "target": class_id,
                "relation": "method_of",
                "confidence": "EXTRACTED",
                "source_file": str(path),
            }
        )

    # 3. SOQL queries (cross-file resolution) -----------------------------
    for n in _walk(root):
        if n.type != "soql_query_body":
            continue
        # Skip subquery bodies — their FROM target is a child relationship, not a
        # queryable SObject. A subquery's soql_query_body sits under a `subquery`.
        if n.parent is not None and n.parent.type == "subquery":
            continue
        sobject_name = _soql_from_sobject(n, source)
        if not sobject_name or not _is_sobject_name(sobject_name):
            continue
        target_id = sobject_nid(sobject_name)
        _ensure_sobject_node(nodes, target_id, sobject_name, path)
        edges.append(
            {
                "source": class_id,
                "target": target_id,
                "relation": "queries",
                "confidence": "EXTRACTED",
                "sf_in_loop": _in_loop(n),
                "source_location": f"L{_line(n)}",
                "source_file": str(path),
            }
        )

    # 4. DML operations ---------------------------------------------------
    var_types = _build_var_types(root, source)
    for n in _walk(root):
        if n.type != "dml_expression":
            continue
        dml = _dml_type_and_target(n, source)
        if dml is None:
            continue
        dml_type, var_name = dml
        sobject_name = var_types.get(var_name, "unknown")
        target_id = sobject_nid(sobject_name)
        _ensure_sobject_node(nodes, target_id, sobject_name, path)
        in_loop = _in_loop(n)
        edge: dict = {
            "source": class_id,
            "target": target_id,
            "relation": "dml_operates_on",
            "sf_dml_type": dml_type,
            "confidence": "INFERRED",
            "sf_in_loop": in_loop,
            "confidence_value": 0.85 if in_loop else 0.9,
            "source_location": f"L{_line(n)}",
            "source_file": str(path),
        }
        if sobject_name == "unknown":
            edge["sf_ambiguous"] = True
        edges.append(edge)

    # 4.5 Apex -> Apex method calls (Phase B) -----------------------------
    # Intra-file calls resolve to a concrete method node now; calls whose target
    # is in another class are exported as unresolved metadata for the SF-wide
    # `resolve_apex_calls` pass (the SF pipeline does not run the core resolver).
    all_var_types = _build_all_var_types(root, source)  # var -> declared type (any)
    unresolved_calls: list[dict] = []
    # Dedup by (caller, callee) pair so two call sites of the same method don't
    # produce duplicate edges — matches the core call pass convention.
    seen_call_pairs: set[tuple[str, str]] = set()
    seen_deferred: set[tuple[str, str | None, str, int]] = set()
    for n in _walk(root):
        if n.type != "method_invocation":
            continue
        name_node = n.child_by_field_name("name")
        if name_node is None:
            continue
        callee = _text(name_node, source)
        arity = _argument_count(n)
        caller_decl = _enclosing_method_decl(n)
        if caller_decl is None:
            continue  # call at field-initializer / static-init level — skip
        caller_id = method_id_by_decl.get(caller_decl.id)
        if caller_id is None:
            continue

        obj_node = n.child_by_field_name("object")
        # Classify the receiver.
        same_class = obj_node is None or obj_node.type == "this"
        receiver_type: str | None = None
        if not same_class and obj_node is not None and obj_node.type == "identifier":
            recv = _text(obj_node, source)
            # `recv` is either a local/param var (resolve its type) or a class name.
            receiver_type = all_var_types.get(recv, recv)

        if same_class:
            targets = methods_by_name.get(callee.lower(), [])
            target_id = _pick_overload(targets, arity, methods_by_name, callee, n)
            if target_id and target_id != caller_id:
                pair = (caller_id, target_id)
                if pair not in seen_call_pairs:
                    seen_call_pairs.add(pair)
                    edges.append(_call_edge(caller_id, target_id, n, path, "EXTRACTED"))
            elif not targets:
                # Unqualified call to a method not declared here — defer (could be
                # an inherited/implicit-this method on a superclass).
                key = (caller_id, None, callee.lower(), arity)
                if key not in seen_deferred:
                    seen_deferred.add(key)
                    unresolved_calls.append(
                        _unresolved(caller_id, None, callee, arity, n, path)
                    )
        else:
            # Qualified call: receiver_type names a class (or a var's class type).
            key = (caller_id, (receiver_type or "").lower() or None, callee.lower(), arity)
            if key not in seen_deferred:
                seen_deferred.add(key)
                unresolved_calls.append(
                    _unresolved(caller_id, receiver_type, callee, arity, n, path)
                )

    # 5. Implements (QCP / Batchable hint) --------------------------------
    implements = _implements_text(definition, source)
    if (
        CPQ_QCP_INTERFACE in implements
        or "QuoteCalculatorPlugin" in implements
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

    if "Database.Batchable" in implements:
        nodes[0]["sf_async_pattern"] = "batchable"

    # Stash cross-file call sites on the class node so they survive the per-file
    # node/edge merge in extract_sf; `resolve_apex_calls` consumes and clears them.
    if unresolved_calls:
        nodes[0]["sf_unresolved_calls"] = unresolved_calls

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# Method-node detail helpers
# ---------------------------------------------------------------------------

def _method_name(method_node, source: bytes) -> str | None:
    """The method's own name: the ``identifier`` that directly precedes
    ``formal_parameters`` (skips type-identifiers in the return type)."""
    params = _named_child_of_type(method_node, "formal_parameters")
    name_node = None
    for c in method_node.children:
        if c == params:
            break
        if c.type == "identifier":
            name_node = c
    if name_node is not None:
        return _text(name_node, source)
    # Fallback: last identifier before params (or any identifier).
    return _first_identifier(method_node, source)


def _method_param_text(method_node, source: bytes) -> str:
    """Reproduce the regex parser's raw parameter-list text (without the parens).

    The regex captured ``(.*?)`` between the parens; we rebuild a comma-joined
    ``Type name`` list from ``formal_parameter`` children so labels stay
    human-readable (e.g. ``getAccounts(String name)``).
    """
    params = _named_child_of_type(method_node, "formal_parameters")
    if params is None:
        return ""
    parts: list[str] = []
    for c in params.children:
        if c.type == "formal_parameter":
            parts.append(" ".join(_text(c, source).split()))
    return ", ".join(parts)


def _method_return_type(method_node, source: bytes) -> str:
    """Return-type text: the node(s) before the method name. ``void`` for
    ``void_type``; the type text otherwise; empty for constructors."""
    params = _named_child_of_type(method_node, "formal_parameters")
    name = _method_name(method_node, source)
    ret_node = None
    for c in method_node.children:
        if c == params:
            break
        if c.type in (
            "void_type",
            "type_identifier",
            "scoped_type_identifier",
            "generic_type",
            "array_type",
        ):
            ret_node = c
        elif c.type == "identifier" and name is not None and _text(c, source) == name:
            # reached the method name — stop
            break
    if ret_node is None:
        return ""
    if ret_node.type == "void_type":
        return "void"
    return " ".join(_text(ret_node, source).split())


def _build_all_var_types(root, source: bytes) -> dict[str, str]:
    """Map every local/parameter variable name to its declared type (any type).

    Unlike ``_build_var_types`` (which keeps only SObject types for DML), this
    retains class types too, so a call ``svc.run()`` can resolve ``svc``'s class.
    Best-effort, file-scoped (no block scoping): a name declared twice keeps the
    first declared type.
    """
    out: dict[str, str] = {}
    for n in _walk(root):
        if n.type == "local_variable_declaration":
            type_node = n.children[0] if n.children else None
            resolved = _type_element_name(type_node, source)
            if not resolved:
                continue
            for c in n.children:
                if c.type == "variable_declarator":
                    name = _first_identifier(c, source)
                    if name:
                        out.setdefault(name, resolved)
        elif n.type == "formal_parameter":
            type_node = n.children[0] if n.children else None
            resolved = _type_element_name(type_node, source)
            name = _first_identifier(n, source)
            if resolved and name:
                out.setdefault(name, resolved)
    return out


def _call_edge(caller_id: str, target_id: str, node, path: Path, confidence: str) -> dict:
    """Build an Apex ``calls`` edge from one method node to another."""
    return {
        "source": caller_id,
        "target": target_id,
        "relation": "calls",
        "context": "call",
        "confidence": confidence,
        "source_location": f"L{_line(node)}",
        "source_file": str(path),
    }


def _unresolved(
    caller_id: str, receiver_type: str | None, callee: str, arity: int, node, path: Path
) -> dict:
    """A cross-file / deferred call site, resolved later by ``resolve_apex_calls``."""
    return {
        "caller_id": caller_id,
        "receiver_type": receiver_type,  # class name (or var's class), or None for bare
        "callee": callee,
        "arity": arity,
        "source_location": f"L{_line(node)}",
        "source_file": str(path),
    }


def _formal_param_count(method_node) -> int:
    """Number of ``formal_parameter`` children on a method declaration."""
    params = _named_child_of_type(method_node, "formal_parameters")
    if params is None:
        return 0
    return sum(1 for c in params.children if c.type == "formal_parameter")


def _pick_overload(
    targets: list[tuple[str, int]], arity: int, methods_by_name=None, callee=None, node=None
) -> str | None:
    """Choose a method-node id from same-name candidates by argument count.

    ``targets`` is a list of ``(method_id, declared_arity)``. With one candidate,
    return it. With several (overloads), return the one whose declared arity
    matches the call's argument count iff that is unique; otherwise return
    ``None`` — the conservative choice (don't guess which overload).
    """
    if not targets:
        return None
    if len(targets) == 1:
        return targets[0][0]
    matches = [tid for tid, ar in targets if ar == arity]
    return matches[0] if len(matches) == 1 else None


def _param_type_signature(method_node, source: bytes) -> str:
    """Normalized param-type signature for overload disambiguation.

    ``foo(Id id)`` -> ``id``; ``foo(List<Id> ids)`` -> ``list_id``;
    ``foo(String a, Integer b)`` -> ``string_integer``; ``foo()`` -> ``""``.
    Lowercased, generic ``<>`` flattened to ``_`` joins, parts joined by ``_``.
    Used only as an id suffix when a class has more than one method of a name.
    """
    params = _named_child_of_type(method_node, "formal_parameters")
    if params is None:
        return ""
    parts: list[str] = []
    for c in params.children:
        if c.type != "formal_parameter":
            continue
        type_node = c.children[0] if c.children else None
        if type_node is None:
            continue
        # Flatten the type text: strip the var name, lowercase, collapse generics.
        raw = _text(type_node, source)
        flat = (
            raw.replace("<", "_").replace(">", "").replace(",", "_")
            .replace("[", "_").replace("]", "").replace(".", "_")
        )
        token = "_".join(flat.split()).strip("_").lower()
        if token:
            parts.append(token)
    return "_".join(parts)


def _argument_count(invocation_node) -> int:
    """Number of arguments in a ``method_invocation``'s ``argument_list``."""
    args = invocation_node.child_by_field_name("arguments")
    if args is None:
        args = _named_child_of_type(invocation_node, "argument_list")
    if args is None:
        return 0
    return sum(1 for c in args.children if c.is_named)


def _enclosing_method_decl(node):
    """The nearest ancestor ``method_declaration`` of *node*, or ``None``."""
    parent = node.parent
    while parent is not None:
        if parent.type == "method_declaration":
            return parent
        parent = parent.parent
    return None


def _dml_type_and_target(dml_node, source: bytes) -> tuple[str, str] | None:
    """Return ``(DML_TYPE, target_var_name)`` for an ``insert``/``update``/``delete``.

    ``dml_expression`` -> ``dml_type`` (keyword child) + an operand expression.
    The operand variable is the first ``identifier`` in the operand (covers
    ``update opps`` and ``update acc``); more complex operands fall back to that
    first identifier, matching the regex parser's ``\\w+`` capture.
    """
    dml_type_node = _named_child_of_type(dml_node, "dml_type")
    if dml_type_node is None:
        return None
    keyword = None
    for c in dml_type_node.children:
        if c.type in _DML_KEYWORDS:
            keyword = c.type
            break
    if keyword is None:
        # dml_type may wrap the keyword as raw text
        kw_text = _text(dml_type_node, source).strip().split()
        keyword = kw_text[0] if kw_text and kw_text[0] in _DML_KEYWORDS else None
    if keyword is None:
        return None

    # Operand: the first identifier appearing after the dml_type node.
    target_var = None
    for c in dml_node.children:
        if c == dml_type_node:
            continue
        if c.type == "identifier":
            target_var = _text(c, source)
            break
        # operand may be an expression wrapping an identifier
        ident = _first_identifier(c, source)
        if ident:
            target_var = ident
            break
    if target_var is None:
        return None
    return keyword.upper(), target_var
