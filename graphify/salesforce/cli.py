"""
graphify-sf: ``graphify sf`` command-line interface.

Subcommands:
    extract <path> [--output-dir] [--cpq-data] [--no-ooe] [--no-fields]   build + enrich an SF graph.json
    cluster-only [path] [--graph]               re-cluster an existing SF graph.json (SF-safe)
    serve <graph.json>                           run the base MCP server
    impact <node> [--direction] [--depth]        impact traversal
    violations [--severity]                       diagnostic risks
    cpq-chain <object>                            CPQ Calc Engine order
    ooe <object>                                  Order-of-Execution chain
    sync-release-notes [--dry-run]               Governor Limits staleness sync

    --no-fields            supresses generation of the field nodes in the graph
    --no-ooe               supresses generation of the order of execution nodes in the graph

Use ``graphify-sfdx cluster-only`` instead of the base ``graphify cluster-only`` — the base
command runs in a separate Python environment that may not have SF file types whitelisted,
causing ``sobject`` / ``field`` / ``flow`` nodes to be downgraded to ``concept``.

The query subcommands load an enriched ``graph.json`` (produced by ``extract``)
and delegate to the pure functions in ``query.py`` — the same ones the MCP tools
use — so CLI and MCP answers stay consistent.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import networkx as nx
from networkx.readwrite import json_graph

_DEFAULT_GRAPH = Path("graphify-out") / "graph.json"


def _load_graph(path: Path) -> nx.DiGraph:
    """Load an enriched SF graph.json into a DiGraph (edges/links key tolerant)."""
    import json

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    edges_key = "links" if "links" in data else "edges"
    try:  # NetworkX >= 3.4 takes an ``edges`` kwarg; older versions default to "links"
        return json_graph.node_link_graph(data, directed=True, multigraph=False, edges=edges_key)
    except TypeError:
        return json_graph.node_link_graph(data, directed=True, multigraph=False)


def _cmd_extract(args: argparse.Namespace) -> int:
    from graphify.salesforce import extract_sf
    from graphify.salesforce.pipeline import build_sf_graph, write_sf_graph

    out_dir = Path(args.output_dir)
    print(f"🚀 Extracting {args.path}")
    extraction = extract_sf(args.path, cpq_data_dir=args.cpq_data, ooe=not args.no_ooe, fields=not args.no_fields)
    G = build_sf_graph(extraction)
    graph_file = write_sf_graph(G, out_dir / "graph.json")

    communities = {d.get("community") for _, d in G.nodes(data=True) if d.get("community") is not None}
    print(f"✅ nodes={G.number_of_nodes()} edges={G.number_of_edges()} communities={len(communities)}")
    print(f"💾 {graph_file}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    # Delegate to the base token-aware MCP server (stdio). Fail gracefully when
    # the optional ``mcp`` stack isn't installed.
    import importlib.util
    if importlib.util.find_spec("mcp") is None:
        print("MCP server requires the 'mcp' package: pip install mcp", file=sys.stderr)
        return 2
    return subprocess.call([sys.executable, "-m", "graphify.serve", str(args.graph)])


def _cmd_viz(args: argparse.Namespace) -> int:
    from graphify.salesforce.viz import build_viz

    G = _load_graph(args.graph)
    out = build_viz(G, args.out, focus=args.focus, max_nodes=args.max_nodes,
                    title=args.title or "graphify-sf 서브그래프")
    print(f"💾 {out}")
    return 0


def _cmd_impact(args: argparse.Namespace) -> int:
    from graphify.salesforce.query import sf_impact

    G = _load_graph(args.graph)
    result = sf_impact(G, args.node, direction=args.direction, depth=args.depth,
                       min_confidence=args.min_confidence)
    print(result["text"])
    return 0 if result["node"] else 1


def _cmd_violations(args: argparse.Namespace) -> int:
    from graphify.salesforce.query import sf_violations

    G = _load_graph(args.graph)
    print(sf_violations(G, severity=args.severity)["text"])
    return 0


def _cmd_cpq_chain(args: argparse.Namespace) -> int:
    from graphify.salesforce.query import sf_cpq_chain

    G = _load_graph(args.graph)
    print(sf_cpq_chain(G, args.object)["text"])
    return 0


def _cmd_ooe(args: argparse.Namespace) -> int:
    from graphify.salesforce.query import sf_ooe

    G = _load_graph(args.graph)
    print(sf_ooe(G, args.object)["text"])
    return 0


def _cmd_cluster_only(args: argparse.Namespace) -> int:
    """Re-cluster an existing SF graph.json using the SF-aware build path.

    Equivalent to ``graphify cluster-only`` but runs entirely within the
    graphify-sfdx package so SF file types (sobject, field, flow, …) are
    preserved.  The base ``graphify cluster-only`` command may run in a
    separate Python environment whose ``build_from_json`` does not know about
    SF types and silently downgrades them to ``concept``.
    """
    import json as _json

    from graphify.build import build_from_json
    from graphify.cluster import cluster, remap_communities_to_previous
    from graphify.export import to_json

    graph_path = Path(args.graph) if args.graph else Path(args.path) / "graphify-out" / "graph.json"
    if not graph_path.exists():
        print(f"error: graph not found at {graph_path} — run extract first", file=sys.stderr)
        return 1

    out_dir = graph_path.parent
    print(f"Loading {graph_path} …")
    raw = _json.loads(graph_path.read_text(encoding="utf-8"))
    _directed = bool(raw.get("directed", False))
    G = build_from_json(raw, directed=_directed)
    print(f"Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    print("Re-clustering …")
    communities = cluster(G, resolution=args.resolution)

    previous_node_community = {
        n["id"]: n["community"]
        for n in raw.get("nodes", [])
        if n.get("community") is not None and n.get("id") is not None
    }
    if previous_node_community:
        communities = remap_communities_to_previous(communities, previous_node_community)

    labels_path = out_dir / ".graphify_labels.json"
    if labels_path.exists():
        try:
            labels = {int(k): v for k, v in _json.loads(labels_path.read_text(encoding="utf-8")).items()}
        except Exception:
            labels = {cid: f"Community {cid}" for cid in communities}
    else:
        labels = {cid: f"Community {cid}" for cid in communities}

    to_json(G, communities, str(graph_path), community_labels=labels)
    print(f"✅ {len(communities)} communities. {graph_path} updated.")
    return 0


def _cmd_sync_release_notes(args: argparse.Namespace) -> int:
    from graphify.salesforce.release_sync import sync_release_notes

    result = sync_release_notes(args.output_dir, dry_run=args.dry_run)
    print(f"version={result['version']} changed={result['changed']} "
          f"status={result['status']} days_stale={result['days_stale']}"
          f"{' (dry-run)' if result['dry_run'] else ''}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="graphify sf", description="Salesforce knowledge-graph CLI")
    sub = p.add_subparsers(dest="command", required=True)

    pe = sub.add_parser("extract", help="build + enrich an SF graph.json")
    pe.add_argument("path")
    pe.add_argument("--output-dir", default="graphify-out")
    pe.add_argument("--cpq-data", default=None, help="dir of SFDX JSON SBQQ data exports")
    pe.add_argument("--no-ooe", action="store_true", default=False,
                    help="skip Order of Execution chain generation (smaller graph)")
    pe.add_argument("--no-fields", action="store_true", default=False,
                    help="strip field nodes and their edges from the graph (smaller graph)")
    pe.set_defaults(func=_cmd_extract)

    pco = sub.add_parser("cluster-only", help="re-cluster an existing SF graph.json (SF-safe, use instead of base graphify cluster-only)")
    pco.add_argument("path", nargs="?", default=".")
    pco.add_argument("--graph", default=None,
                     help="explicit path to graph.json (default: <path>/graphify-out/graph.json)")
    pco.add_argument("--resolution", type=float, default=1.0,
                     help="Leiden resolution (>1 = more, smaller communities)")
    pco.set_defaults(func=_cmd_cluster_only)

    ps = sub.add_parser("serve", help="run the base MCP server on a graph.json")
    ps.add_argument("graph", nargs="?", default=str(_DEFAULT_GRAPH))
    ps.set_defaults(func=_cmd_serve)

    pz = sub.add_parser("viz", help="render a focused self-contained HTML visualization")
    pz.add_argument("graph", nargs="?", default=str(_DEFAULT_GRAPH))
    pz.add_argument("--focus", action="append", default=None,
                    help="node name/ID to center on (repeatable)")
    pz.add_argument("--out", default="graphify-out/graph.html")
    pz.add_argument("--max-nodes", dest="max_nodes", type=int, default=90)
    pz.add_argument("--title", default=None)
    pz.set_defaults(func=_cmd_viz)

    pi = sub.add_parser("impact", help="impact traversal from a node")
    pi.add_argument("node")
    pi.add_argument("--graph", default=str(_DEFAULT_GRAPH))
    pi.add_argument("--direction", choices=["downstream", "upstream", "both"], default="downstream")
    pi.add_argument("--depth", type=int, default=3)
    pi.add_argument("--min-confidence", dest="min_confidence", type=float, default=0.0)
    pi.set_defaults(func=_cmd_impact)

    pv = sub.add_parser("violations", help="list diagnostic risks")
    pv.add_argument("--graph", default=str(_DEFAULT_GRAPH))
    pv.add_argument("--severity", default=None, help="CRITICAL|HIGH|MEDIUM|LOW|INFO")
    pv.set_defaults(func=_cmd_violations)

    pc = sub.add_parser("cpq-chain", help="CPQ Calc Engine order for an object")
    pc.add_argument("object")
    pc.add_argument("--graph", default=str(_DEFAULT_GRAPH))
    pc.set_defaults(func=_cmd_cpq_chain)

    po = sub.add_parser("ooe", help="Order-of-Execution chain for an SObject")
    po.add_argument("object")
    po.add_argument("--graph", default=str(_DEFAULT_GRAPH))
    po.set_defaults(func=_cmd_ooe)

    pr = sub.add_parser("sync-release-notes", help="sync Governor Limits version file")
    pr.add_argument("--output-dir", default="graphify-out")
    pr.add_argument("--dry-run", action="store_true")
    pr.set_defaults(func=_cmd_sync_release_notes)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)
