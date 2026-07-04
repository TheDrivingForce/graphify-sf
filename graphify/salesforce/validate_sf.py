"""
graphify-sf: Salesforce-specific node / edge schema definitions and validation.

This module is a *schema-only* layer. It declares which Salesforce ``file_type``
and ``relation`` values are legal, what edge attributes each relation may carry,
and a graph-level integrity check (`validate_sf_graph`). It does NOT create any
nodes or edges — the parsers (apex_enhanced, flow, lwc, objects, profiles, …)
and analysis passes (cpq, order_of_execution, governor_limits) own that.

Companion to `graphify/validate.py`: that file validates the *base* graphify
schema (shared across all languages); this file adds the Salesforce-specific
vocabulary. The SF file types are mirrored into `validate.VALID_FILE_TYPES`
(step 1) so base validation accepts SF nodes too.

Schema source of truth: docs/ARCHITECTURE.md "스키마" section.
Confidence rubric: ADR-020. Risk relations: ADR-028~030.
"""

from __future__ import annotations

import networkx as nx

# ---------------------------------------------------------------------------
# 1. SF node file_type values (ARCHITECTURE.md schema table)
# ---------------------------------------------------------------------------

SF_FILE_TYPES = {
    "sobject",          # Salesforce Object (Account, SBQQ__Quote__c, …)
    "flow",             # Flow / Process Builder
    "lwc_component",    # Lightning Web Component (bundle node, one per folder)
    "lwc_controller",   # One JS file within an LWC bundle (part_of the bundle)
    "lwc_template",     # One HTML template within an LWC bundle (part_of the bundle)
    "profile",          # Profile (user permissions)
    "permission_set",   # Permission Set
    "cpq_rule",         # CPQ Price/Product Rule object
    "cpq_qcp_method",   # QCP Calc Engine callback node (ADR-025)
    "cpq_condition",    # CPQ Price/Error Condition record (cpq_data)
    "cpq_action",       # CPQ Price/Product Action record (cpq_data)
    "aura_component",   # Aura Component / Application (legacy, ADR-102)
    "vf_page",          # Visualforce page (ADR-101)
    "vf_component",     # Visualforce component (ADR-101)
    "validation_rule",  # Validation Rule (ADR-030)
    "record_type",          # Record Type (objects/<Obj>/recordTypes)
    "permission_set_group", # Permission Set Group
    "workflow",             # Workflow rule set (per SObject)
    "field",                # Custom Field (objects.py)
    "cmt_record",           # Custom Metadata record (customMetadata/<Type>.<Record>)
    "sharing_rule",         # Sharing Rule (owner/criteria/guest/territory)
    "custom_label",         # Custom Label ($Label.X reference target)
}


# ---------------------------------------------------------------------------
# 2. SF edge relation values (ARCHITECTURE.md schema table)
# ---------------------------------------------------------------------------

SF_RELATIONS = {
    # Apex
    "triggers_on",          # Apex Trigger -> SObject
    "queries",              # SOQL site -> SObject
    "dml_operates_on",      # DML site -> SObject
    "method_of",            # Apex method -> its class (membership; mirrors field_of)
    "calls",                # Apex method -> Apex method (intra/cross-file call)

    # Flow
    "flow_invokes",         # Flow ApexAction -> Apex class

    # LWC
    "wire_to",              # LWC @wire -> Apex method
    "lwc_calls",            # LWC imperative @salesforce/apex import -> Apex method
    "part_of",              # LWC JS/HTML file -> its bundle component node
    "embeds",               # LWC HTML file -> embedded child LWC bundle (c- tag)
    "imports",              # LWC JS file -> imported c/ module bundle
    "member_of",            # Exported LWC function -> its bundle component
    "instantiates",         # Apex class -> class constructed via `new X()`

    # Permission
    "grants_access_to",     # Profile/PermSet -> Object/Field
    "field_of",             # Custom Field -> Custom Object
    "contains_permission_set",  # Permission Set Group -> Permission Set

    # Metadata coverage
    "record_type_of",       # Record Type -> SObject
    "cmt_record_of",        # Custom Metadata record -> <Type>__mdt SObject
    "shares",               # Sharing Rule -> SObject
    "maps_to",              # field -> field (Custom Metadata field-mapping record)

    # CPQ
    "cpq_applies_to",       # CPQ Rule/QCP -> target object
    "cpq_has_condition",    # CPQ Rule -> Price/Error Condition (cpq_data)
    "cpq_has_action",       # CPQ Rule -> Price/Product Action (cpq_data)
    "cpq_reads_field",      # CPQ Condition -> Field (cpq_data)
    "cpq_writes_field",     # CPQ Action -> Field (cpq_data)

    # Order of Execution
    "order_of_execution",   # OoE Step N -> Step N+1 (DAG)

    # Diagnostics
    "governor_violation",       # Method -> Governor Limit sentinel
    "gov_permission_violation", # CPQ Rule -> Profile (FLS-restricted field, ADR-028)
    "validates",                # Validation Rule -> SObject

    # Relationship analysis (ADR-028~030)
    "publishes_event",      # Apex -> Platform Event
    "cpq_validation_risk",  # CPQ Rule <-> Validation Rule conflict
    "infinite_loop_risk",   # Flow <-> CPQ infinite loop
}


# ---------------------------------------------------------------------------
# 3. Per-relation edge attribute schema
# ---------------------------------------------------------------------------

#: Allowed edge attributes per relation type. ``required`` attrs MUST be present;
#: ``optional`` attrs MAY be present. Relations not listed here carry no
#: SF-specific attributes beyond the base edge fields.
EDGE_ATTRIBUTES_BY_RELATION: dict[str, dict[str, list[str]]] = {
    "queries": {
        "optional": ["sf_in_loop", "sf_dynamic", "sf_ambiguous"],
    },
    "dml_operates_on": {
        "optional": ["sf_in_loop"],
    },
    "cpq_applies_to": {
        "required": ["execution_order"],
        "optional": ["sf_cpq_rule_type", "sf_in_mass_action"],
    },
    "governor_violation": {
        "required": ["sf_violation_type", "sf_severity"],
        "optional": [
            "sf_in_loop", "sf_reason", "sf_violation_count", "sf_limit",
            # recursive_trigger violations (ADR-027)
            "sf_safe_recursion", "sf_cycle",
        ],
    },
    "cpq_validation_risk": {
        "optional": ["sf_risk_level", "sf_overlapping_fields", "sf_note"],
    },
    "infinite_loop_risk": {
        "optional": ["sf_loop_type", "sf_severity", "sf_reason", "sf_loop_prevention"],
    },
}


# ---------------------------------------------------------------------------
# 4. Graph-level validation
# ---------------------------------------------------------------------------

def validate_sf_graph(G: nx.DiGraph) -> list[str]:
    """SF-specific graph integrity checks.

    Complements `validate.validate_extraction` (base schema) with structural
    invariants that only make sense for the Salesforce graph.

    Checks:
        1. SObject node IDs follow the ``sobject_`` prefix convention (ADR-002).
        2. The ``order_of_execution`` subgraph is acyclic (must be a DAG).
        3. ``cpq_applies_to`` edges carry the required ``execution_order`` attr.
        4. ``governor_violation`` edges carry the required diagnostic attrs.

    Args:
        G: Assembled NetworkX directed graph.

    Returns:
        List of validation error messages. Empty list means the graph is valid.
    """
    errors: list[str] = []

    # Check 1: SObject node ID prefix convention. Only nodes that ARE SObjects
    # (file_type == "sobject") must follow it — a substring match on "sobject"
    # would wrongly flag Apex named ``SObjectUtil`` / methods like
    # ``getSObjectName`` (real-org false positives).
    for node, data in G.nodes(data=True):
        if data.get("file_type") == "sobject" and not str(node).startswith("sobject_"):
            errors.append(f"Malformed SObject node ID: {node}")

    # Check 2: Order of Execution chain must be a DAG
    ooe_edges = [
        (u, v)
        for u, v, d in G.edges(data=True)
        if d.get("relation") == "order_of_execution"
    ]
    if ooe_edges:
        ooe_graph = nx.DiGraph(ooe_edges)
        if not nx.is_directed_acyclic_graph(ooe_graph):
            cycles = list(nx.simple_cycles(ooe_graph))
            first = " -> ".join(cycles[0]) if cycles else "?"
            errors.append(
                f"OoE chain contains cycles (must be DAG): {first}"
            )

    # Checks 3 & 4: required edge attributes by relation
    for u, v, d in G.edges(data=True):
        relation = d.get("relation")
        spec = EDGE_ATTRIBUTES_BY_RELATION.get(relation)
        if not spec:
            continue
        for attr in spec.get("required", []):
            if attr not in d:
                errors.append(
                    f"{relation} edge missing required '{attr}': {u} -> {v}"
                )

    return errors
