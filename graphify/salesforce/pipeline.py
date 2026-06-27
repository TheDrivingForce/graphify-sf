"""
graphify-sf: consumption-layer pipeline (enrichment + persistence).

``extract_sf()`` returns a raw ``{"nodes", "edges"}`` extraction. To make that
queryable with the token-aware base graphify consumption stack (the MCP
``serve`` server, ``god_nodes``, ``shortest_path``, community navigation), the
graph must first be (1) built into a NetworkX DiGraph and (2) enriched with
Leiden community labels. This module wires the SF extraction through the base
``build`` / ``cluster`` machinery instead of dumping it raw (the previous
``__main__`` behaviour, which bypassed every enrichment).

Design notes:
    - **Directed** graph: SF impact analysis needs source→target direction (a
      trigger acts ON an SObject; a Flow updates a Quote). An undirected graph
      would lose "what does changing X break?".
    - **Direct construction, NOT ``build.build_from_json``**: the base builder
      hard-codes a file_type whitelist (``code/document/paper/image/rationale/
      concept``) and collapses every other type to ``concept`` (build.py:187),
      which would erase the SF semantics (``sobject``/``flow``/``cpq_rule``/
      ``validation_rule``/…); it also renames a node's ``source`` attribute to
      ``source_file``, clobbering the Apex source. So we build the DiGraph
      ourselves, preserving every attribute. SF IDs are already canonical /
      deduped via ``sobject_nid`` + ``_merge_into`` in ``extract_sf``, so no
      entity dedup is needed.
    - **Community label persistence**: ``serve`` reconstructs communities from a
      ``community`` attribute on each node (``serve._communities_from_graph``);
      we write exactly that attribute so ``graph_stats`` / ``get_community`` work.
"""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
from networkx.readwrite import json_graph

from graphify.cluster import cluster


def build_sf_graph(extraction: dict, *, resolution: float = 1.0) -> nx.DiGraph:
    """Build + enrich an SF extraction into a queryable directed graph.

    Args:
        extraction: ``{"nodes": [...], "edges": [...]}`` from ``extract_sf``.
        resolution: Leiden resolution (>1 = more, smaller communities).

    Returns:
        A NetworkX ``DiGraph`` preserving all SF node/edge attributes, with a
        ``community`` attribute on every node that landed in a community
        (matching ``serve._communities_from_graph``).
    """
    G = nx.DiGraph()
    for node in extraction.get("nodes", []):
        attrs = {k: v for k, v in node.items() if k not in ("id", "source")}
        G.add_node(node["id"], **attrs)
    for edge in extraction.get("edges", []):
        # Skip dangling edges so a self-consistent graph is serialized.
        if edge["source"] not in G or edge["target"] not in G:
            continue
        attrs = {k: v for k, v in edge.items() if k not in ("source", "target")}
        G.add_edge(edge["source"], edge["target"], **attrs)

    # Leiden community detection (graspologic, Louvain fallback) — accepts a
    # DiGraph and converts to undirected internally.
    communities = cluster(G, resolution=resolution)
    for community_id, node_ids in communities.items():
        for node_id in node_ids:
            if node_id in G:
                G.nodes[node_id]["community"] = community_id

    return G


def write_sf_graph(G: nx.DiGraph, out_path: Path | str) -> Path:
    """Serialize the enriched graph to node-link JSON at ``out_path``.

    Written with the ``links`` edge key (newer NetworkX) so the base
    ``serve._load_graph`` loads it without remapping; older NetworkX falls back
    to the default key, which ``serve`` also accepts.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json_graph.node_link_data(G, edges="links")
    except TypeError:  # NetworkX < 3.4 has no ``edges`` kwarg
        data = json_graph.node_link_data(G)
    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return out_path
