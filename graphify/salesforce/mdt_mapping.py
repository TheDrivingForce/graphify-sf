"""
graphify-sf: Custom Metadata field-mapping analysis pass.

Some ``__mdt`` records are field-mapping tables: their ``<values>`` name a source
object/field and a target object/field (e.g. ``Opportunity_To_Quote_Mapping__mdt``
copies Opportunity fields onto ``SBQQ__Quote__c`` during quote creation). The
parser (``metadata.extract_custom_metadata_record``) keeps the raw pairs on the
node as ``sf_values``; this pass turns recognized mappings into traversable
``maps_to`` edges (source field -> target field) so ``sf_impact`` can follow a
field change across the Opp->Quote boundary.

Detection is heuristic and best-effort (ADR-009). A value is treated as a field
reference when it is either:

    1. **Dotted** — ``Object.Field`` (e.g. ``Opportunity.Amount``); or
    2. **Separated** — a ``*Object*`` key (holding the object API name) paired by
       shared prefix with a ``*Field*`` key (holding the field API name), as in
       ``Main_Object__c`` + ``Main_Object_Api_Field__c``.

Direction (source vs target) is read from role hints in the key / pairing prefix
(``main``/``source``/``from``/``first`` vs ``second``/``target``/``to``/``dest``).
A mapping whose direction cannot be classified still emits an edge in record order
but is flagged ``sf_ambiguous`` with ``AMBIGUOUS`` confidence (ADR-020).

This is an analysis pass (CLAUDE.md rule 4): it runs over the whole node/edge list
and mutates it in place, appending field stub nodes (no dangling edges, ADR-012)
and the ``maps_to`` edges. Records that are not field mappings produce no edges,
so the graph stays token-economical.
"""

from __future__ import annotations

import re

#: A value shaped like ``Object.Field`` (single dot, identifier on each side).
_DOTTED_RE = re.compile(r"^[A-Za-z][\w]*\.[A-Za-z][\w]*$")

#: Role hint tokens (matched as whole ``_``-separated tokens, not substrings).
_SOURCE_HINTS = frozenset({"main", "source", "from", "first", "primary", "origin", "src"})
_TARGET_HINTS = frozenset({"second", "target", "to", "dest", "destination", "result"})


def _field_nid(object_api: str | None, field_api: str) -> str:
    """Object-scoped field node ID, delegating to ``objects._field_nid``.

    The owning object is required for a field to merge with the same field
    parsed from object metadata (ADR-002). When a mapping record omits the
    object, fall back to ``"unknown"`` so the ID is still stable (the field
    simply won't merge with a parsed node).
    """
    from graphify.salesforce.objects import _field_nid as _scoped_field_nid

    return _scoped_field_nid(object_api or "unknown", field_api)


def _norm_key(key: str) -> str:
    return key.lower().replace("__c", "").replace("__", "_").strip("_")


def _classify(hint: str) -> str | None:
    """Map a key / prefix to ``"source"`` / ``"target"`` / ``None`` by role token."""
    tokens = set(re.split(r"[_\s]+", hint.lower()))
    if tokens & _SOURCE_HINTS:
        return "source"
    if tokens & _TARGET_HINTS:
        return "target"
    return None


def _refs_from_values(values: dict) -> list[tuple[str | None, str | None, str]]:
    """Return ``(role, object_api_or_None, field_api)`` field references in a record."""
    refs: list[tuple[str | None, str | None, str]] = []
    used: set[str] = set()

    # 1. Dotted Object.Field values — role read from the key name.
    for key, val in values.items():
        if isinstance(val, str) and _DOTTED_RE.match(val.strip()):
            obj, fld = val.strip().split(".", 1)
            refs.append((_classify(key), obj, fld))
            used.add(key)

    # 2. Separated *Object* + *Field* key pairs sharing a prefix.
    obj_keys: dict[str, str] = {}   # prefix -> object api value
    fld_keys: list[tuple[str, str]] = []  # (normalized key, field api value)
    for key, val in values.items():
        if key in used or not isinstance(val, str) or not val.strip():
            continue
        nk = _norm_key(key)
        if nk.endswith("object"):
            obj_keys[nk[: -len("object")].strip("_")] = val.strip()
        elif nk.endswith("field"):
            fld_keys.append((nk, val.strip()))

    for nk, fval in fld_keys:
        # Pair with the longest object prefix this field key starts with.
        match = None
        for prefix in obj_keys:
            if prefix and nk.startswith(prefix) and (match is None or len(prefix) > len(match)):
                match = prefix
        obj_api = obj_keys.get(match) if match else None
        refs.append((_classify(match or nk), obj_api, fval))

    return refs


def _ensure_field(nodes, by_id, fid, api, obj, record) -> None:
    if fid in by_id:
        node = by_id[fid]
        if obj and not node.get("sf_object"):
            node["sf_object"] = obj
        return
    node = {
        "id": fid, "label": api, "file_type": "field",
        "source_file": record.get("source_file", ""), "sf_api_name": api,
    }
    if obj:
        node["sf_object"] = obj
    nodes.append(node)
    by_id[fid] = node


def mdt_mapping_pass(nodes: list[dict], edges: list[dict]) -> list[dict]:
    """Emit ``maps_to`` field->field edges for field-mapping ``cmt_record`` nodes.

    Mutates ``nodes`` / ``edges`` in place (appends field stubs + edges) and also
    returns the new edges for callers that prefer the functional form.
    """
    by_id = {n["id"]: n for n in nodes}
    new_edges: list[dict] = []

    for record in [n for n in nodes if n.get("file_type") == "cmt_record"]:
        values = record.get("sf_values") or {}
        if not values:
            continue
        refs = _refs_from_values(values)
        sources = [r for r in refs if r[0] == "source"]
        targets = [r for r in refs if r[0] == "target"]

        pairs: list[tuple] = []
        ambiguous = False
        if sources and targets:
            for s in sources:
                for t in targets:
                    pairs.append((s, t, "INFERRED"))
        elif len(refs) >= 2:
            # Direction unknown — assume the first ref feeds the rest.
            ambiguous = True
            for t in refs[1:]:
                pairs.append((refs[0], t, "AMBIGUOUS"))

        for (_, sobj, sfld), (_, tobj, tfld), conf in pairs:
            sid, tid = _field_nid(sobj, sfld), _field_nid(tobj, tfld)
            if sid == tid:
                continue
            _ensure_field(nodes, by_id, sid, sfld, sobj, record)
            _ensure_field(nodes, by_id, tid, tfld, tobj, record)
            edge = {
                "source": sid, "target": tid, "relation": "maps_to",
                "confidence": conf, "source_file": record.get("source_file", ""),
                "sf_via": record["id"], "sf_mapping_type": record.get("sf_mdt_type"),
            }
            if sobj:
                edge["sf_source_object"] = sobj
            if tobj:
                edge["sf_target_object"] = tobj
            if ambiguous:
                edge["sf_ambiguous"] = True
            new_edges.append(edge)

    edges.extend(new_edges)
    return new_edges
