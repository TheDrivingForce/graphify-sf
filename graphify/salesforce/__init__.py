"""
graphify-sf: Salesforce-specific knowledge graph extraction.

Main API:
- register(): Register SF parsers with graphify's core _DISPATCH table.
- extract_sf(): Extract a Salesforce repository and run the SF analysis passes.

``extract_sf`` is the pipeline wrapper described in ARCHITECTURE.md ("서브패키지
구조" / "분석 패스 실행 순서"): it dispatches each Salesforce file to its parser,
merges the per-file results into one node/edge list (cross-file resolution via
``sobject_nid`` — ADR-002), then runs the graph-wide analysis passes in the fixed
order CPQ -> LWC merge -> OoE -> Governor (ADR-011). Errors degrade gracefully
(ADR-009 lenient): an unreadable / unparseable file is skipped, never aborting
the whole analysis.
"""

from __future__ import annotations

from pathlib import Path


def register():
    """Register Salesforce parsers with the main graphify pipeline.

    Patches ``extract._DISPATCH`` so the core pipeline routes Salesforce
    metadata suffixes to the SF parsers. ``extract_sf`` does NOT depend on this
    (it dispatches internally), but ``import graphify.salesforce; register()``
    enables SF parsing through the plain ``graphify`` entry points (ADR-014).
    """
    from graphify.extract import _DISPATCH

    from .apex_enhanced import extract_apex_enhanced
    from .flow import extract_flow
    from .lwc import extract_lwc_html, extract_lwc_js
    from .metadata import (
        extract_custom_labels,
        extract_custom_metadata_record,
        extract_permission_set_group,
        extract_record_type,
        extract_sharing_rules,
        extract_workflow,
    )
    from .objects import (
        extract_custom_field,
        extract_custom_object,
        extract_validation_rule,
    )
    from .profiles import extract_permission_set, extract_profile

    _DISPATCH[".cls"] = extract_apex_enhanced
    _DISPATCH[".trigger"] = extract_apex_enhanced
    _DISPATCH[".flow-meta.xml"] = extract_flow
    _DISPATCH[".object-meta.xml"] = extract_custom_object
    _DISPATCH[".field-meta.xml"] = extract_custom_field
    _DISPATCH[".validationRule-meta.xml"] = extract_validation_rule
    _DISPATCH[".recordType-meta.xml"] = extract_record_type
    _DISPATCH[".workflow-meta.xml"] = extract_workflow
    _DISPATCH[".permissionsetgroup-meta.xml"] = extract_permission_set_group
    _DISPATCH[".md-meta.xml"] = extract_custom_metadata_record
    _DISPATCH[".sharingRules-meta.xml"] = extract_sharing_rules
    _DISPATCH[".labels-meta.xml"] = extract_custom_labels
    _DISPATCH[".profile-meta.xml"] = extract_profile
    _DISPATCH[".permissionset-meta.xml"] = extract_permission_set
    _DISPATCH[".html"] = extract_lwc_html
    _DISPATCH[".js"] = extract_lwc_js


def _parser_for(path: Path):
    """Return the SF parser callable for a file, or ``None`` if unsupported.

    Suffix matching mirrors ``extract._get_extractor`` (ADR-003): multi-part
    ``*.meta.xml`` names are matched on ``name``, while ``.cls`` / ``.trigger``
    match on the plain suffix. ``.html`` / ``.js`` are LWC-only — limited to
    files living under an ``lwc/`` directory so ordinary web assets are ignored.
    """
    from .apex_enhanced import extract_apex_enhanced
    from .flow import extract_flow
    from .lwc import extract_lwc_html, extract_lwc_js
    from .metadata import (
        extract_custom_labels,
        extract_custom_metadata_record,
        extract_permission_set_group,
        extract_record_type,
        extract_sharing_rules,
        extract_workflow,
    )
    from .objects import (
        extract_custom_field,
        extract_custom_object,
        extract_validation_rule,
    )
    from .profiles import extract_permission_set, extract_profile

    name = path.name
    if name.endswith(".object-meta.xml"):
        return extract_custom_object
    if name.endswith(".validationRule-meta.xml"):
        return extract_validation_rule
    if name.endswith(".recordType-meta.xml"):
        return extract_record_type
    if name.endswith(".workflow-meta.xml"):
        return extract_workflow
    if name.endswith(".permissionsetgroup-meta.xml"):
        return extract_permission_set_group
    if name.endswith(".md-meta.xml"):
        return extract_custom_metadata_record
    if name.endswith(".sharingRules-meta.xml"):
        return extract_sharing_rules
    if name.endswith(".labels-meta.xml"):
        return extract_custom_labels
    if name.endswith(".field-meta.xml"):
        return extract_custom_field
    if name.endswith(".flow-meta.xml"):
        return extract_flow
    if name.endswith(".profile-meta.xml"):
        return extract_profile
    if name.endswith(".permissionset-meta.xml"):
        return extract_permission_set

    suffix = path.suffix
    if suffix in (".cls", ".trigger"):
        return extract_apex_enhanced

    # LWC HTML/JS only — guard on an lwc/ directory in the path.
    is_lwc = "lwc" in {p.lower() for p in path.parts[:-1]}
    if is_lwc and suffix == ".html":
        return extract_lwc_html
    if is_lwc and suffix == ".js":
        return extract_lwc_js

    return None


def _merge_into(all_nodes, node_by_id, node):
    """Merge ``node`` into the accumulated node list, deduping by ``id``.

    First occurrence wins for ``id`` / ``label`` (ADR-012); subsequent nodes
    with the same ID only fill in attributes the existing node is missing. This
    is what lets a full SObject node (from the Object parser) and a stub SObject
    node (from an Apex SOQL ``FROM`` clause) collapse into a single node.

    Special case: ``file_type`` is upgradeable from ``"concept"`` — a stub or
    error node that lands first with the generic fallback type is overwritten by
    any later node carrying a specific type (e.g. ``"sobject"``).
    """
    existing = node_by_id.get(node["id"])
    if existing is None:
        node_by_id[node["id"]] = node
        all_nodes.append(node)
        return
    for key, value in node.items():
        if key in ("id", "label"):
            continue
        existing_val = existing.get(key)
        # Treat "concept" as an unknown/fallback type: a more specific type wins.
        if key == "file_type" and existing_val == "concept" and value not in (None, "", "concept"):
            existing[key] = value
            continue
        if existing_val in (None, "", []) and value not in (None, "", []):
            existing[key] = value


def _merge_lwc_components(all_nodes, all_edges):
    """LWC merge pass (Pass SF-2 / ADR-008): fold each ``*.html`` template node
    into its sibling ``*.js`` controller node so one ``lwc_component`` node
    remains per component.

    The HTML parser emits ``lwc_<stem>_html``; the JS parser emits ``lwc_<stem>``.
    When both exist we keep the JS node (it carries the ``@wire`` / ``@api``
    signal) and drop the HTML node, tagging the survivor ``sf_has_template``.
    A template-only component is promoted to the base ID so it is still a single
    ``lwc_component`` node.
    """
    node_by_id = {n["id"]: n for n in all_nodes}
    html_nodes = [n for n in all_nodes if n.get("sf_lwc_file_type") == "html"]
    for html in html_nodes:
        html_id = html["id"]
        base_id = html_id[: -len("_html")] if html_id.endswith("_html") else html_id
        base = node_by_id.get(base_id)
        if base is None or base is html:
            # Template-only component: promote to the base ID.
            all_nodes.remove(html)
            html["id"] = base_id
            html.pop("sf_lwc_file_type", None)
            html["sf_has_template"] = True
            node_by_id[base_id] = html
            all_nodes.append(html)
            continue
        base["sf_has_template"] = True
        all_nodes.remove(html)
        node_by_id.pop(html_id, None)


def _strip_fields(all_nodes: list[dict], all_edges: list[dict]) -> None:
    """Remove all ``field`` nodes and any edges that reference them (in place)."""
    field_ids = {n["id"] for n in all_nodes if n.get("file_type") == "field"}
    all_nodes[:] = [n for n in all_nodes if n["id"] not in field_ids]
    all_edges[:] = [
        e for e in all_edges
        if e.get("source") not in field_ids and e.get("target") not in field_ids
    ]


def extract_sf(
    path,
    *,
    cpq_data_dir=None,
    ooe: bool = True,
    fields: bool = True,
    no_same_class_calls: bool = False,
    **kwargs,
):
    """Extract a Salesforce repository into a knowledge graph.

    Dispatches every supported Salesforce file under ``path`` to its parser,
    merges the results (deduping nodes by ID for cross-file resolution), then
    merges any CPQ configuration data (``cpq_data_dir``) before running the SF
    analysis passes in the fixed ADR-011 order:

        1. ``cpq_analysis_pass``       — reclassify SBQQ__ nodes, detect QCP.
        2. ``_merge_lwc_components``   — fold LWC HTML + JS into one node.
        2b. ``mdt_mapping_pass``       — turn field-mapping ``__mdt`` records into
           traversable ``maps_to`` field->field edges (e.g. Opp->Quote).
        3. ``ooe_analysis_pass``       — Order of Execution chains.
        4. ``governor_limit_analysis_pass`` — diagnostic ``governor_violation``
           edges (appended to the edge list).
        5. ``detect_recursive_triggers`` — recursive ``calls`` cycles as
           ``governor_violation`` edges, guarded recursion downgraded (ADR-027).
        6. ``permission_analysis_pass`` — Profile/FLS constraints on CPQ
           (``gov_permission_violation`` edges, ADR-028). Runs after CPQ so
           ``file_type == "cpq_rule"`` / ``sf_cpq_object`` are already set.
        7. ``detect_flow_cpq_loops``   — Flow ↔ CPQ Type A loops
           (``infinite_loop_risk`` self-edges, ADR-029).
        8. ``validation_cpq_analysis_pass`` — CPQ Rule ↔ Validation Rule field
           overlap (``cpq_validation_risk`` edges, ADR-030).

    Passes 4–8 are pure functions returning diagnostic edges that the caller
    appends; passes 1–3 mutate the node/edge lists in place.

    Args:
        path: Path to a Salesforce repository / metadata directory (or a single
            metadata file).
        cpq_data_dir: Optional directory of SFDX JSON exports of SBQQ CPQ records
            (Price/Product Rules, Conditions, Actions). When given, the real CPQ
            rule logic is merged in before the analysis passes so ``validation_cpq``
            / impact see the fields rules actually read/write (Phase 2).
        ooe: If ``False``, skip Order of Execution chain generation (smaller graph).
        fields: If ``False``, remove all ``field`` nodes and their edges after all
            analysis passes complete. Useful when field-level detail is not needed
            and a smaller graph is preferred. Passes still run with fields present
            so CPQ/validation overlap analysis is unaffected.
        no_same_class_calls: If ``True``, drop Apex ``calls`` edges whose caller and
            callee belong to the same class, keeping only inter-class call links.
            Applied after cross-file call resolution so downstream passes
            (recursion detection) see the filtered set.
        **kwargs: Reserved for future options (neo4j-uri, …); currently ignored.

    Returns:
        ``{"nodes": [...], "edges": [...]}`` — the merged, analyzed graph.
    """
    from .apex_calls import drop_same_class_calls, resolve_apex_calls
    from .cpq import cpq_analysis_pass
    from .flow_cpq_loops import detect_flow_cpq_loops
    from .mdt_mapping import mdt_mapping_pass
    from .governor_limits import (
        detect_recursive_triggers,
        governor_limit_analysis_pass,
    )
    from .order_of_execution import ooe_analysis_pass
    from .permission_analysis import permission_analysis_pass
    from .validation_cpq import validation_cpq_analysis_pass

    root = Path(path)
    if root.is_file():
        files = [root]
    else:
        from graphify.detect import _is_ignored, _load_graphifyignore

        ignore_patterns = _load_graphifyignore(root)
        ignore_cache: dict = {}
        files = sorted(
            p
            for p in root.rglob("*")
            if p.is_file()
            and not _is_ignored(p, root, ignore_patterns, _cache=ignore_cache)
        )

    all_nodes: list[dict] = []
    all_edges: list[dict] = []
    node_by_id: dict[str, dict] = {}

    for file_path in files:
        parser = _parser_for(file_path)
        if parser is None:
            continue
        try:
            result = parser(file_path)
        except Exception:
            # Lenient mode (ADR-009): skip a file the parser could not handle.
            continue
        for node in result.get("nodes", []):
            _merge_into(all_nodes, node_by_id, node)
        all_edges.extend(result.get("edges", []))

    # CPQ configuration data (SFDX JSON exports) — the rule LOGIC that lives as
    # SBQQ data records, not metadata files. Merged before the analysis passes so
    # validation_cpq / impact see the real fields the rules read/write (Phase 2).
    if cpq_data_dir is not None:
        from .cpq_data import extract_cpq_data

        cpq_result = extract_cpq_data(cpq_data_dir)
        for node in cpq_result.get("nodes", []):
            _merge_into(all_nodes, node_by_id, node)
        all_edges.extend(cpq_result.get("edges", []))

    # Analysis passes — fixed order (ADR-011). Passes 1-3 mutate in place;
    # passes 4-7 are pure functions whose diagnostic edges are appended here.
    cpq_analysis_pass(all_nodes, all_edges)
    _merge_lwc_components(all_nodes, all_edges)
    mdt_mapping_pass(all_nodes, all_edges)
    if ooe:
        ooe_analysis_pass(all_nodes, all_edges)
    # Resolve cross-file Apex->Apex calls BEFORE recursion detection so the new
    # `calls` edges feed cycle enumeration (ADR-027). Intra-file calls were
    # already emitted by the parser.
    all_edges.extend(resolve_apex_calls(all_nodes, all_edges))
    # Optionally keep only inter-class call links (--no-same-class-calls): drop
    # intra-class method->method calls before downstream passes consume them.
    if no_same_class_calls:
        drop_same_class_calls(all_nodes, all_edges)
    all_edges.extend(governor_limit_analysis_pass(all_nodes, all_edges))
    all_edges.extend(detect_recursive_triggers(all_nodes, all_edges))
    all_edges.extend(permission_analysis_pass(all_nodes, all_edges))
    all_edges.extend(detect_flow_cpq_loops(all_nodes, all_edges))
    all_edges.extend(validation_cpq_analysis_pass(all_nodes, all_edges))

    if not fields:
        _strip_fields(all_nodes, all_edges)

    return {"nodes": all_nodes, "edges": all_edges}
