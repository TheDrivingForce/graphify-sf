"""graphify-sf: Neo4j export with Salesforce-aware labels and relationship types.

Two delivery modes (ADR-007, ADR-013):

* ``to_cypher_sf(G, path)`` — write a ``.cypher`` file of ``MERGE`` statements
  for offline / version-controlled import via Neo4j Browser or ``cypher-shell``.
* ``push_to_neo4j_sf(G, uri, user, password)`` — upsert the graph directly into
  a live Neo4j instance through the official Python driver.

Both paths reuse the *base* graphify Cypher helpers (`_cypher_escape`,
`_cypher_label`) from `graphify.export` so the injection hardening stays in one
place — this module only adds the SF-specific node-label / relation-type
vocabulary (``docs/how-it-works.md`` "Neo4j export", ARCHITECTURE.md schema).

No schema/constraint/index DDL is emitted here — labels are created implicitly
by MERGE, and uniqueness constraints are a separate concern.
"""

from __future__ import annotations

import networkx as nx

from graphify.export import _cypher_escape, _cypher_label

# ---------------------------------------------------------------------------
# SF vocabulary → Neo4j vocabulary
# ---------------------------------------------------------------------------

#: graphify ``file_type`` -> Neo4j node label. Unknown types fall back to a
#: sanitised, capitalised form of the file_type (see `_neo4j_label`).
SF_NODE_TYPE_TO_NEO4J_LABEL: dict[str, str] = {
    "code": "ApexClass",
    "sobject": "SObject",
    "flow": "Flow",
    "lwc_component": "LWCComponent",
    "cpq_rule": "CPQRule",
    "cpq_qcp_method": "CPQQCPMethod",
    "profile": "Profile",
    "permission_set": "PermissionSet",
    "aura_component": "AuraComponent",
    "concept": "Concept",
    "validation_rule": "ValidationRule",
}

#: graphify edge ``relation`` -> Neo4j relationship type. Unknown relations fall
#: back to a sanitised, upper-cased form of the relation (see `_neo4j_rel_type`).
SF_RELATION_TO_NEO4J_TYPE: dict[str, str] = {
    # Apex
    "triggers_on": "TRIGGERS_ON",
    "queries": "QUERIES",
    "dml_operates_on": "DML_OPERATES_ON",
    "method_of": "METHOD_OF",
    # Flow
    "flow_invokes": "FLOW_INVOKES",
    # LWC
    "wire_to": "WIRE_TO",
    "embeds": "EMBEDS",
    # Permission
    "grants_access_to": "GRANTS_ACCESS_TO",
    "field_of": "FIELD_OF",
    # CPQ
    "cpq_applies_to": "CPQ_APPLIES_TO",
    # Order of Execution
    "order_of_execution": "ORDER_OF_EXECUTION",
    # Diagnostics
    "governor_violation": "GOVERNOR_VIOLATION",
    "gov_permission_violation": "GOV_PERMISSION_VIOLATION",
    "validates": "VALIDATES",
    # Relationship analysis (ADR-028~030)
    "publishes_event": "PUBLISHES_EVENT",
    "cpq_validation_risk": "CPQ_VALIDATION_RISK",
    "infinite_loop_risk": "INFINITE_LOOP_RISK",
    # Base graphify relation carried through SF graphs
    "calls": "CALLS",
}

#: Node/edge attributes that are structural and never re-emitted as properties.
_NODE_SKIP_KEYS = frozenset({"id", "label", "file_type"})
_EDGE_SKIP_KEYS = frozenset({"source", "target", "relation"})


def _neo4j_label(file_type: str | None) -> str:
    """Map a graphify ``file_type`` to a safe Neo4j node label."""
    mapped = SF_NODE_TYPE_TO_NEO4J_LABEL.get(file_type or "")
    if mapped:
        return mapped
    return _cypher_label((file_type or "node").capitalize(), "Node")


def _neo4j_rel_type(relation: str | None) -> str:
    """Map a graphify edge ``relation`` to a safe Neo4j relationship type."""
    mapped = SF_RELATION_TO_NEO4J_TYPE.get(relation or "")
    if mapped:
        return mapped
    return _cypher_label((relation or "related_to").upper(), "RELATED_TO")


def _cypher_literal(value) -> str:
    """Render a scalar Python value as a Cypher literal for the .cypher file."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return f"'{_cypher_escape(str(value))}'"


def _scalar_props(data: dict, skip: frozenset[str]) -> dict:
    """Keep only JSON-scalar attributes safe to materialise as properties."""
    return {
        k: v
        for k, v in data.items()
        if k not in skip
        and not k.startswith("_")
        and isinstance(v, (str, int, float, bool))
    }


def _props_clause(props: dict) -> str:
    """Render ``{k: v, ...}`` for a Cypher inline property map (keys sorted)."""
    if not props:
        return ""
    body = ", ".join(
        f"{_cypher_label(k, 'attr')}: {_cypher_literal(v)}"
        for k, v in sorted(props.items())
    )
    return f" {{{body}}}"


def to_cypher_sf(G: nx.DiGraph, output_path: str) -> None:
    """Export ``G`` to a Neo4j-import-ready ``.cypher`` file of MERGE statements.

    Node statements use SF-aware labels (``:ApexClass``, ``:SObject``, …) and
    carry ``id`` + ``label`` + any scalar attributes. Relationship statements
    use SF-aware types (``:TRIGGERS_ON``, ``:QUERIES``, …), matching endpoints
    by ``id`` so re-running is idempotent (MERGE upserts).

    Args:
        G: Assembled Salesforce knowledge graph.
        output_path: Destination ``.cypher`` file path.
    """
    lines = ["// Neo4j Cypher import - generated by graphify-sf", ""]

    for node_id, data in G.nodes(data=True):
        label = _neo4j_label(data.get("file_type"))
        props = {"id": node_id, "label": data.get("label", node_id)}
        props.update(_scalar_props(data, _NODE_SKIP_KEYS))
        lines.append(f"MERGE (n:{label}{_props_clause(props)});")

    lines.append("")

    for u, v, data in G.edges(data=True):
        rel = _neo4j_rel_type(data.get("relation"))
        props = _scalar_props(data, _EDGE_SKIP_KEYS)
        lines.append(
            f"MATCH (a {{id: '{_cypher_escape(u)}'}}), "
            f"(b {{id: '{_cypher_escape(v)}'}}) "
            f"MERGE (a)-[:{rel}{_props_clause(props)}]->(b);"
        )

    with open(output_path, "w", encoding="utf-8") as f:  # nosec
        f.write("\n".join(lines))


def push_to_neo4j_sf(
    G: nx.DiGraph,
    uri: str,
    user: str,
    password: str,
) -> dict:
    """Push ``G`` directly to a running Neo4j instance via the Python driver.

    Requires: ``pip install neo4j`` (graphify ``[neo4j]`` extra). Uses MERGE so
    re-running is safe — nodes and relationships are upserted, not duplicated.
    Endpoints are parameterised; labels / relationship types are validated by
    the allowlist mappers (they cannot be Cypher-parameterised).

    Args:
        G: Assembled Salesforce knowledge graph.
        uri: Bolt URI, e.g. ``bolt://localhost:7687`` or ``neo4j+s://…``.
        user: Neo4j username.
        password: Neo4j password.

    Returns:
        ``{"success": bool, "nodes_created": int, "relationships_created": int}``
    """
    try:
        from neo4j import GraphDatabase
    except ImportError as e:  # pragma: no cover - exercised via mock in tests
        raise ImportError(
            "neo4j driver not installed. Run: pip install neo4j"
        ) from e

    results = {"success": False, "nodes_created": 0, "relationships_created": 0}
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            for node_id, data in G.nodes(data=True):
                label = _neo4j_label(data.get("file_type"))
                props = {"label": data.get("label", node_id)}
                props.update(_scalar_props(data, _NODE_SKIP_KEYS))
                session.run(
                    f"MERGE (n:{label} {{id: $id}}) SET n += $props",
                    id=node_id,
                    props=props,
                )
                results["nodes_created"] += 1

            for u, v, data in G.edges(data=True):
                rel = _neo4j_rel_type(data.get("relation"))
                props = _scalar_props(data, _EDGE_SKIP_KEYS)
                session.run(
                    f"MATCH (a {{id: $src}}), (b {{id: $tgt}}) "
                    f"MERGE (a)-[r:{rel}]->(b) SET r += $props",
                    src=u,
                    tgt=v,
                    props=props,
                )
                results["relationships_created"] += 1

        results["success"] = True
    finally:
        driver.close()

    return results
