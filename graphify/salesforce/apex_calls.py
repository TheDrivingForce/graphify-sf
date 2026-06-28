"""
graphify-sf: Apex -> Apex cross-file call resolution (Pass SF, Phase B).

The Apex parser (``apex_ts``) resolves *intra-file* method calls during its walk
and emits ``calls`` edges directly. Calls whose target lives in another class
cannot be resolved per-file, so the parser stashes them on the class node as
``sf_unresolved_calls``. This pass runs after all files are parsed and merged,
builds a global method index, and resolves those deferred call sites.

It must run BEFORE ``detect_recursive_triggers`` so the new ``calls`` edges feed
recursion-cycle detection (ADR-027), and it deliberately mirrors the core
``extract()`` cross-file resolver's conservatism (#543): a call to a method name
that matches several classes is only emitted when exactly one candidate fits.

Resolution rules (conservative — see ADR-019 Phase B):

    - ``Class.method(...)`` / ``var.method(...)`` where the receiver class is
      known: resolve to that class's method (arity-matched on overloads).
      ``EXTRACTED``.
    - Bare ``method(...)`` not found in the caller's own class: resolve only if a
      single class anywhere declares it. ``INFERRED``.
    - Anything ambiguous (name matches multiple classes, or overload arity is not
      uniquely matched) is skipped — no edge, rather than a wrong edge.
"""

from __future__ import annotations


def _method_index(all_nodes: list[dict]) -> tuple[dict, dict]:
    """Build lookup indexes over Apex method nodes.

    Returns ``(by_class_name, by_method_name)`` where:
      - ``by_class_name``: lower class label -> {lower method name -> [(id, arity)]}
      - ``by_method_name``: lower method name -> [(id, arity, class_label_lower)]

    Arity is derived from the method node's label ``name(p1, p2)`` param list.
    """
    # Map class node id -> lower class label, for grouping methods by their class.
    class_label_by_id: dict[str, str] = {}
    for n in all_nodes:
        if n.get("file_type") == "code" and n.get("sf_code_type") in (
            "class",
            "trigger",
            "interface",
        ):
            class_label_by_id[n["id"]] = (n.get("label") or "").lower()

    by_class_name: dict[str, dict[str, list[tuple[str, int]]]] = {}
    by_method_name: dict[str, list[tuple[str, int, str]]] = {}

    for n in all_nodes:
        if n.get("sf_method_type") != "method":
            continue
        mid = n["id"]
        # The owning class id is the method id with the trailing _<name>[_<sig>]
        # stripped — but more robustly, the class is the longest class-id prefix.
        owner = _owner_class_id(mid, class_label_by_id)
        class_label = class_label_by_id.get(owner, "")
        mname = _method_name_from_label(n.get("label", ""))
        arity = _arity_from_label(n.get("label", ""))
        if not mname:
            continue
        by_class_name.setdefault(class_label, {}).setdefault(mname, []).append(
            (mid, arity)
        )
        by_method_name.setdefault(mname, []).append((mid, arity, class_label))
    return by_class_name, by_method_name


def _owner_class_id(method_id: str, class_label_by_id: dict[str, str]) -> str:
    """Return the class node id that owns *method_id* (longest matching prefix)."""
    best = ""
    for cid in class_label_by_id:
        if method_id.startswith(cid + "_") and len(cid) > len(best):
            best = cid
    return best


def _method_name_from_label(label: str) -> str:
    return label.split("(", 1)[0].strip().lower() if label else ""


def _arity_from_label(label: str) -> int:
    """Argument count from a ``name(Type a, Type b)`` label."""
    if "(" not in label:
        return 0
    inside = label[label.index("(") + 1 : label.rindex(")")] if ")" in label else ""
    inside = inside.strip()
    if not inside:
        return 0
    # Commas at top level only — method-param types here never contain top-level
    # commas except generics (List<Map<a,b>>). Strip <...> first to be safe.
    depth = 0
    cleaned = []
    for ch in inside:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            cleaned.append("|")
            continue
        cleaned.append(ch)
    return len("".join(cleaned).split("|"))


def _pick(candidates: list[tuple[str, int]], arity: int) -> str | None:
    """One candidate -> it; several -> the unique arity match, else None."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]
    matches = [mid for mid, ar in candidates if ar == arity]
    return matches[0] if len(matches) == 1 else None


def drop_same_class_calls(all_nodes: list[dict], all_edges: list[dict]) -> None:
    """Remove ``calls`` edges whose caller and callee live in the same Apex class.

    Implements the ``--no-same-class-calls`` option: when set, the graph keeps
    only *inter-class* call links, dropping intra-class method->method calls so
    the call graph shows the relationships *between* classes rather than each
    class's internal control flow. Mutates ``all_edges`` in place.

    Only Apex method->method ``calls`` edges are considered. An edge is dropped
    when both endpoints resolve to the same owning class node id; calls whose
    endpoints belong to different classes (or that we cannot attribute to a
    class) are kept.
    """
    class_label_by_id: dict[str, str] = {
        n["id"]: (n.get("label") or "").lower()
        for n in all_nodes
        if n.get("file_type") == "code"
        and n.get("sf_code_type") in ("class", "trigger", "interface")
    }
    # Owning class id for each method node, resolved once.
    owner_by_method: dict[str, str] = {
        n["id"]: _owner_class_id(n["id"], class_label_by_id)
        for n in all_nodes
        if n.get("sf_method_type") == "method"
    }

    def _same_class(edge: dict) -> bool:
        if edge.get("relation") != "calls":
            return False
        src_owner = owner_by_method.get(edge.get("source"))
        tgt_owner = owner_by_method.get(edge.get("target"))
        # Both endpoints must be attributable to the SAME, non-empty class.
        return bool(src_owner) and src_owner == tgt_owner

    all_edges[:] = [e for e in all_edges if not _same_class(e)]


def resolve_apex_calls(all_nodes: list[dict], all_edges: list[dict]) -> list[dict]:
    """Resolve deferred Apex->Apex call sites into ``calls`` edges.

    Reads ``sf_unresolved_calls`` off class nodes (set by ``apex_ts``), resolves
    each against the global method index, and returns new ``calls`` edges. The
    ``sf_unresolved_calls`` metadata is cleared from the nodes (it has served its
    purpose and should not leak into the exported graph). Caller does
    ``all_edges.extend(resolve_apex_calls(...))``.
    """
    by_class_name, by_method_name = _method_index(all_nodes)

    existing = {
        (e.get("source"), e.get("target"))
        for e in all_edges
        if e.get("relation") == "calls"
    }
    new_edges: list[dict] = []

    for node in all_nodes:
        pending = node.pop("sf_unresolved_calls", None)
        if not pending:
            continue
        for call in pending:
            caller_id = call["caller_id"]
            callee = (call.get("callee") or "").lower()
            arity = call.get("arity", 0)
            receiver = call.get("receiver_type")
            target_id: str | None = None
            confidence = "EXTRACTED"

            if receiver:
                # Qualified: Class.method / var.method (var's class type).
                methods = by_class_name.get(receiver.lower(), {}).get(callee, [])
                target_id = _pick(methods, arity)
            else:
                # Bare name not found in caller's own class: resolve only if a
                # single class declares it (conservative). INFERRED.
                candidates = [
                    (mid, ar) for mid, ar, _cls in by_method_name.get(callee, [])
                ]
                # If the name appears in exactly one class, _pick by arity there.
                classes = {cls for _mid, _ar, cls in by_method_name.get(callee, [])}
                if len(classes) == 1:
                    target_id = _pick(candidates, arity)
                    confidence = "INFERRED"

            if not target_id or target_id == caller_id:
                continue
            if (caller_id, target_id) in existing:
                continue
            existing.add((caller_id, target_id))
            new_edges.append(
                {
                    "source": caller_id,
                    "target": target_id,
                    "relation": "calls",
                    "context": "call",
                    "confidence": confidence,
                    "source_location": call.get("source_location", ""),
                    "source_file": call.get("source_file", ""),
                }
            )

    return new_edges
