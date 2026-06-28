"""
graphify-sf: focused subgraph visualization (self-contained HTML).

Renders a focused neighborhood of an enriched SF graph as a single standalone
HTML file with the graph data **embedded inline** (no ``fetch`` — so it opens
from ``file://`` with no CORS issue). This replaces the ad-hoc hand-written D3
scripts: ``graphify sf viz`` calls ``build_viz`` so the visualization is a
first-class library feature, reproducible without glue code.

Edges are styled by SF semantics: INSERT DML (record creation) is highlighted
green, other DML amber, ``validates`` purple, diagnostics (governor / loop /
risk) red. Nodes are colored by ``file_type`` and sized up for SObjects.
"""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx

from graphify.salesforce.query import resolve_node
from graphify.security import sanitize_label

_COLORS = {
    "sobject": "#e74c3c", "code": "#3498db", "lwc_component": "#16a085",
    "lwc_controller": "#1abc9c", "lwc_template": "#48c9b0",
    "validation_rule": "#9b59b6", "field": "#f39c12", "flow": "#2ecc71",
    "cpq_rule": "#e67e22", "cpq_condition": "#d35400", "cpq_action": "#c0392b",
    "cpq_qcp_method": "#1abc9c", "profile": "#95a5a6", "concept": "#7f8c8d",
}


def focus_subgraph(
    G: nx.DiGraph, seeds: list[str], *, max_nodes: int = 90, include_tests: bool = False
) -> tuple[list[str], list[tuple[str, str]]]:
    """Collect a focused node set around *seeds* (1-hop in+out) and inner edges.

    Test classes (id containing ``test``) are excluded unless ``include_tests``.
    Seeds are always kept; remaining nodes are ranked by degree and capped at
    ``max_nodes`` so the densest, most central context survives truncation.
    """
    keep = set(seeds)
    for seed in seeds:
        if seed not in G:
            continue
        for nbr in list(G.successors(seed)) + list(G.predecessors(seed)):
            if include_tests or "test" not in nbr.lower():
                keep.add(nbr)

    if len(keep) > max_nodes:
        ranked = sorted(keep - set(seeds), key=lambda n: G.degree(n), reverse=True)
        keep = set(seeds) | set(ranked[: max_nodes - len(seeds)])

    edges = [(u, v) for u, v in G.edges() if u in keep and v in keep]
    return list(keep), edges


_HTML = """<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><title>__TITLE__</title>
<script src="https://d3js.org/d3.v7.min.js"></script><style>
body{margin:0;font-family:-apple-system,sans-serif;background:#0d1117;color:#e6edf3}
#h{padding:10px 16px;background:#161b22;border-bottom:1px solid #30363d;font-size:13px}
#h b{color:#58a6ff}svg{width:100vw;height:calc(100vh - 70px)}
.lbl{font-size:9px;fill:#9aa;pointer-events:none}
line{stroke:#444}line.create{stroke:#3fb950;stroke-width:2.5}line.dml{stroke:#e3b341;stroke-width:2}
line.val{stroke:#a371f7}line.risk{stroke:#f85149;stroke-width:2}
#tip{position:fixed;background:#161b22;border:1px solid #30363d;padding:6px 10px;border-radius:6px;font-size:12px;pointer-events:none;opacity:0}
</style></head><body>
<div id="h"><b>__TITLE__</b><br><span style="color:#3fb950">━ INSERT(생성)</span>
<span style="color:#e3b341">━ DML</span> <span style="color:#a371f7">━ validates</span>
<span style="color:#f85149">━ risk</span> &nbsp;|&nbsp; hover=상세, 드래그/줌</div>
<svg></svg><div id="tip"></div><script>
const DATA=__DATA__,C=__COLORS__;
const svg=d3.select("svg"),W=innerWidth,H=innerHeight-70,g=svg.append("g");
svg.call(d3.zoom().on("zoom",e=>g.attr("transform",e.transform)));
const sim=d3.forceSimulation(DATA.nodes)
 .force("link",d3.forceLink(DATA.edges).id(d=>d.id).distance(70))
 .force("charge",d3.forceManyBody().strength(-220))
 .force("center",d3.forceCenter(W/2,H/2)).force("collide",d3.forceCollide(20));
const link=g.selectAll("line").data(DATA.edges).join("line").attr("class",d=>{
 if(d.relation==="dml_operates_on")return String(d.dml).toUpperCase()==="INSERT"?"create":"dml";
 if(d.relation==="validates")return "val";
 if(["governor_violation","infinite_loop_risk","cpq_validation_risk","gov_permission_violation"].includes(d.relation))return "risk";
 return "";});
const node=g.selectAll("circle").data(DATA.nodes).join("circle")
 .attr("r",d=>d.seed?13:6).attr("fill",d=>C[d.file_type]||"#7f8c8d")
 .attr("stroke",d=>d.seed?"#fff":"#222").attr("stroke-width",d=>d.seed?3:1)
 .call(d3.drag().on("start",(e,d)=>{if(!e.active)sim.alphaTarget(.3).restart();d.fx=d.x;d.fy=d.y;})
  .on("drag",(e,d)=>{d.fx=e.x;d.fy=e.y;}).on("end",(e,d)=>{if(!e.active)sim.alphaTarget(0);d.fx=null;d.fy=null;}));
const tip=d3.select("#tip");
node.on("mouseover",(e,d)=>tip.style("opacity",1).html(`<b>${d.label}</b><br>${d.file_type}<br>${d.src||""}`))
 .on("mousemove",e=>tip.style("left",(e.clientX+12)+"px").style("top",(e.clientY+12)+"px"))
 .on("mouseout",()=>tip.style("opacity",0));
const lbl=g.selectAll("text").data(DATA.nodes.filter(d=>d.seed||d.label.length<22)).join("text")
 .attr("class","lbl").text(d=>d.label);
sim.on("tick",()=>{link.attr("x1",d=>d.source.x).attr("y1",d=>d.source.y).attr("x2",d=>d.target.x).attr("y2",d=>d.target.y);
 node.attr("cx",d=>d.x).attr("cy",d=>d.y);lbl.attr("x",d=>d.x+9).attr("y",d=>d.y+3);});
</script></body></html>"""


def render_html(
    G: nx.DiGraph, node_ids: list[str], edges: list[tuple[str, str]],
    *, seeds: list[str], title: str,
) -> str:
    """Render the focused subgraph as a self-contained HTML string."""
    seed_set = set(seeds)
    nodes = [{
        "id": n, "label": sanitize_label(str(G.nodes[n].get("label", n)))[:40],
        "file_type": G.nodes[n].get("file_type", "concept"),
        "src": (G.nodes[n].get("source_file", "") or "").split("/")[-1],
        "seed": n in seed_set,
    } for n in node_ids]
    edge_data = [{
        "source": u, "target": v,
        "relation": G[u][v].get("relation", ""),
        "dml": G[u][v].get("sf_dml_type", ""),
    } for u, v in edges]
    data = json.dumps({"nodes": nodes, "edges": edge_data})
    return (_HTML.replace("__DATA__", data).replace("__COLORS__", json.dumps(_COLORS))
            .replace("__TITLE__", title))


def build_viz(
    G: nx.DiGraph, out_path: Path | str, *, focus: list[str] | None = None,
    max_nodes: int = 90, title: str = "graphify-sf 서브그래프",
) -> Path:
    """Build a focused HTML visualization of *G* around *focus* node names.

    ``focus`` names are resolved to node IDs via ``query.resolve_node``; if none
    resolve (or none given), the highest-degree nodes seed the view.
    """
    seeds = [r for name in (focus or []) if (r := resolve_node(G, name))]
    if not seeds:
        seeds = [n for n, _ in sorted(G.degree, key=lambda x: x[1], reverse=True)[:2]]
    node_ids, edges = focus_subgraph(G, seeds, max_nodes=max_nodes)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(G, node_ids, edges, seeds=seeds, title=title),
                        encoding="utf-8")
    return out_path
