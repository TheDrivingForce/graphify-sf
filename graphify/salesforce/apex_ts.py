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
    - Apex -> Apex type references from variable / parameter / return / field
      declarations (deferred to ``apex_calls.resolve_apex_type_refs``, which
      links only otherwise-orphaned classes — the DTO fallback).
    - Apex -> Flow launches via ``new Flow.Interview.<FlowApiName>(...)``
      (``calls`` edge -> ``flow_<name>``, ``context: "flow_interview"``).
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

#: Apex access modifiers, in narrowest->widest order. The narrowest one declared
#: on a member wins as its ``sf_scope`` (an Apex member has at most one of these).
_ACCESS_MODIFIERS = ("private", "protected", "public", "global")


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


def _scope(decl_node) -> str:
    """Return the declared access modifier for a class/method/interface node.

    Reads the ``modifiers`` child and returns the first of
    ``private``/``protected``/``public``/``global`` found. Apex defaults an
    un-annotated member to ``private``, so that is the fallback when no access
    modifier is present (matches the language's implicit visibility).
    """
    modifiers = _named_child_of_type(decl_node, "modifiers")
    if modifiers is not None:
        for m in _walk(modifiers):
            if m.type in _ACCESS_MODIFIERS:
                return m.type
    return "private"


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


def _type_ref_names(type_node, source: bytes) -> list[str]:
    """All user-class candidate names a type node mentions.

    Unlike ``_type_element_name`` (DML-oriented, first generic arg only), this
    collects EVERY class-candidate name so ``Map<Id, FlockEventResponseDto>``
    yields the value type too:

        ``Foo`` -> ``[Foo]``; ``List<Foo>`` -> ``[Foo]``;
        ``Map<Id, Foo>`` -> ``[Foo]``; ``Foo[]`` -> ``[Foo]``.

    Builtins (``_NON_SOBJECT_TYPES``) and dotted ``scoped_type_identifier`` types
    (inner classes — out of scope, matching ``_constructed_type_name``) yield
    nothing. Collection containers themselves are builtins; only their type
    arguments can name a class (Apex has no user-defined generics).
    """
    if type_node is None:
        return []
    t = type_node.type
    if t == "type_identifier":
        name = _text(type_node, source)
        return [name] if name and name not in _NON_SOBJECT_TYPES else []
    if t == "generic_type":
        names: list[str] = []
        args = _named_child_of_type(type_node, "type_arguments")
        if args is not None:
            for c in args.children:
                names.extend(_type_ref_names(c, source))
        return names
    if t == "array_type":
        for c in type_node.children:
            if c.type in ("type_identifier", "generic_type"):
                return _type_ref_names(c, source)
    return []


def _declared_type_node(decl_node):
    """The type node of a local/field/parameter declaration.

    Robust to leading ``modifiers`` (``final``, ``public static`` ...): returns
    the first child that is a type-shaped node rather than assuming position 0.
    """
    for c in decl_node.children:
        if c.type in ("type_identifier", "scoped_type_identifier", "generic_type", "array_type"):
            return c
    return None


def _flow_interview_name(creation_node, source: bytes) -> str | None:
    """Return the Flow API name of a ``new Flow.Interview.<Name>(...)`` construction.

    Apex launches an autolaunched flow / screen flow programmatically with
    ``new Flow.Interview.<FlowApiName>(inputs)``. The type node is a dotted
    ``scoped_type_identifier`` whose text is ``Flow.Interview.<FlowApiName>``;
    ``_constructed_type_name`` deliberately returns ``None`` for such scoped types
    (they are not Apex class links), so this handles the Flow case separately.

    Returns the last dotted segment (the flow API name) only for the exact
    ``Flow.Interview.<Name>`` shape; ``None`` for any other construction.
    """
    type_node = None
    for c in creation_node.children:
        if c.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
            type_node = c
            break
    if type_node is None or type_node.type != "scoped_type_identifier":
        return None
    parts = _text(type_node, source).split(".")
    if len(parts) == 3 and parts[0] == "Flow" and parts[1] == "Interview" and parts[2]:
        return parts[2]
    return None


def _constructed_type_name(creation_node, source: bytes) -> str | None:
    """Return the user-class type name of an ``object_creation_expression``.

    ``new TranslationResponse()`` -> ``TranslationResponse``. Returns ``None`` for
    constructions whose type is not a candidate Apex *class* link:

      - collection containers (``new List<X>()``, ``new Map<K,V>()``) — the
        ``generic_type`` container is a builtin; its element type, if a class, is
        picked up where the element itself is constructed/used, not here.
      - primitives / system builtins (``new String()``-like) via ``_NON_SOBJECT_TYPES``.
      - dotted types (``new Foo.Bar()``) — inner classes are out of scope here.

    The grammar shape is ``new <type> <arguments>`` where ``<type>`` is the first
    named child after the ``new`` keyword.
    """
    type_node = None
    for c in creation_node.children:
        if c.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
            type_node = c
            break
    if type_node is None:
        return None
    # Collection containers and scoped/dotted types are not direct class links.
    if type_node.type != "type_identifier":
        return None
    name = _text(type_node, source)
    if not name or name in _NON_SOBJECT_TYPES:
        return None
    return name


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


def _trigger_sobject(trigger_node, source: bytes) -> str | None:
    """Return the SObject a ``trigger_declaration`` fires on, or ``None``.

    Grammar is ``trigger <name> on <SObject> ( <events> ) { ... }`` — the target
    is the first ``identifier`` following the ``on`` keyword child.
    """
    seen_on = False
    for c in trigger_node.children:
        if c.type == "on":
            seen_on = True
            continue
        if seen_on and c.type == "identifier":
            return _text(c, source)
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
            # Access scope (public/private/protected/global) of the type itself.
            "sf_scope": _scope(definition),
            # ORIGINAL source kept verbatim for CPQ / governor passes.
            "source": source.decode("utf-8", errors="replace"),
        }
    )

    # 1.5 Trigger -> SObject it fires on ---------------------------------
    # ``trigger AccountTrigger on Account (...)`` names the SObject the trigger
    # runs against. Emit a `triggers_on` edge to that object so the graph shows
    # which trigger acts on which SObject (mirrors queries / dml_operates_on).
    if code_type == "trigger":
        sobject_name = _trigger_sobject(definition, source)
        if sobject_name and _is_sobject_name(sobject_name):
            target_id = sobject_nid(sobject_name)
            _ensure_sobject_node(nodes, target_id, sobject_name, path)
            edges.append(
                {
                    "source": class_id,
                    "target": target_id,
                    "relation": "triggers_on",
                    "confidence": "EXTRACTED",
                    "source_file": str(path),
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
                # Access scope (public/private/protected/global); defaults to
                # private when un-annotated, matching Apex implicit visibility.
                "sf_scope": _scope(n),
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

    # 4.6 Apex -> Apex type references via `new X()` (Phase B) ------------
    # A `new TypeName()` instantiation references a class even when no method is
    # called on it — pure DTO / wrapper classes (no methods, default ctor) would
    # otherwise be orphaned. We collect the constructed type name and defer
    # resolution to the SF-wide `resolve_apex_refs` pass (target class is almost
    # always in another file). Collection containers (List/Set/Map) and other
    # non-class builtins are skipped; the constructed element type is what links.
    # Also handles `new Flow.Interview.<FlowApiName>(inputs)` in the same walk: a
    # programmatic flow launch. This is an Apex class -> Flow link, emitted as a
    # `calls` edge (Apex invokes the flow like a method) to a flow node stub that
    # merges with the real `flow_<name>` node parsed from the .flow-meta.xml.
    unresolved_refs: list[dict] = []
    seen_refs: set[str] = set()
    seen_flow_calls: set[str] = set()
    for n in _walk(root):
        if n.type != "object_creation_expression":
            continue
        flow_name = _flow_interview_name(n, source)
        if flow_name:
            flow_id = f"flow_{flow_name.lower()}"
            if flow_id not in seen_flow_calls:
                seen_flow_calls.add(flow_id)
                if not any(nd["id"] == flow_id for nd in nodes):
                    # Stub only: no ``source_file`` — the real flow node owns that
                    # (its .flow-meta.xml). Merge fills our gaps, not the reverse,
                    # so claiming source_file here could shadow the real path if
                    # this file is merged first.
                    nodes.append(
                        {
                            "id": flow_id,
                            "label": flow_name,
                            "file_type": "flow",
                        }
                    )
                edges.append(
                    {
                        "source": class_id,
                        "target": flow_id,
                        "relation": "calls",
                        "context": "flow_interview",
                        "confidence": "EXTRACTED",
                        "source_location": f"L{_line(n)}",
                        "source_file": str(path),
                    }
                )
            continue
        type_name = _constructed_type_name(n, source)
        if not type_name:
            continue
        # Skip self-references and obvious builtins.
        if type_name.lower() == class_name.lower():
            continue
        key = type_name.lower()
        if key in seen_refs:
            continue
        seen_refs.add(key)
        unresolved_refs.append(
            {
                "caller_id": class_id,
                "type_name": type_name,
                "source_location": f"L{_line(n)}",
                "source_file": str(path),
            }
        )

    # 4.7 Apex -> Apex type references via declarations (orphan fallback) --
    # A class used ONLY as a variable / parameter / return / field type (a pure
    # DTO handed around but never `new`-ed and with no methods to call) produces
    # no `calls` or `instantiates` edge. Collect each declared type name once per
    # class and defer to `resolve_apex_type_refs`, which emits `references` edges
    # only toward classes that would otherwise be orphaned — so the thousands of
    # routine declarations across a codebase do not each become an edge.
    unresolved_type_refs: list[dict] = []
    seen_type_refs: set[str] = set()

    def _collect_type_refs(type_node, site) -> None:
        for type_name in _type_ref_names(type_node, source):
            key = type_name.lower()
            if key == class_name.lower() or key in seen_type_refs:
                continue
            seen_type_refs.add(key)
            unresolved_type_refs.append(
                {
                    "caller_id": class_id,
                    "type_name": type_name,
                    "source_location": f"L{_line(site)}",
                    "source_file": str(path),
                }
            )

    for n in _walk(root):
        if n.type in ("local_variable_declaration", "formal_parameter", "field_declaration"):
            _collect_type_refs(_declared_type_node(n), n)
        elif n.type == "method_declaration":
            ret_node = _return_type_node(n, source)
            if ret_node is not None and ret_node.type != "void_type":
                _collect_type_refs(ret_node, n)

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
    if unresolved_refs:
        nodes[0]["sf_unresolved_refs"] = unresolved_refs
    if unresolved_type_refs:
        nodes[0]["sf_unresolved_type_refs"] = unresolved_type_refs

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


def _return_type_node(method_node, source: bytes):
    """The return-type node preceding the method name (``None`` for constructors)."""
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
    return ret_node


def _method_return_type(method_node, source: bytes) -> str:
    """Return-type text: the node(s) before the method name. ``void`` for
    ``void_type``; the type text otherwise; empty for constructors."""
    ret_node = _return_type_node(method_node, source)
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
