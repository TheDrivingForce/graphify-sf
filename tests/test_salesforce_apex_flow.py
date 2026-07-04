"""Apex -> Flow launch tests (ADR-105).

Apex can launch a Flow programmatically with
``new Flow.Interview.<FlowApiName>(inputs)``. The tree-sitter parser detects that
construction and emits a ``calls`` edge (``context: "flow_interview"``) from the
Apex class node to the ``flow_<name>`` node, mirroring how a method call is an
Apex->Apex ``calls`` edge.
"""

from __future__ import annotations

from pathlib import Path

from graphify.salesforce import extract_sf
from graphify.salesforce.apex_enhanced import extract_apex_enhanced

FIXTURES = Path(__file__).parent / "fixtures"


def _flow_calls(edges: list[dict]) -> set[tuple[str, str]]:
    return {
        (e["source"], e["target"])
        for e in edges
        if e["relation"] == "calls" and e.get("context") == "flow_interview"
    }


def test_apex_launches_flow_emits_calls_edge(tmp_path: Path) -> None:
    """``new Flow.Interview.X(...)`` -> a ``calls`` edge to ``flow_x``."""
    cls = tmp_path / "FlockEventOnsiteController.cls"
    cls.write_text(
        "public class FlockEventOnsiteController {\n"
        "    public void send() {\n"
        "        Map<String, Object> flowInputs = new Map<String, Object>();\n"
        "        Flow.Interview.Send_Email_Using_Flow sendResponseFlow =\n"
        "            new Flow.Interview.Send_Email_Using_Flow(flowInputs);\n"
        "        sendResponseFlow.start();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    res = extract_apex_enhanced(cls)
    calls = _flow_calls(res["edges"])
    assert ("apex_flockeventonsitecontroller", "flow_send_email_using_flow") in calls
    edge = next(
        e for e in res["edges"]
        if e["relation"] == "calls" and e["target"] == "flow_send_email_using_flow"
    )
    assert edge["confidence"] == "EXTRACTED"
    # A stub flow node is emitted so the per-file edge is not dangling.
    stub = next(n for n in res["nodes"] if n["id"] == "flow_send_email_using_flow")
    assert stub["file_type"] == "flow"
    assert stub["label"] == "Send_Email_Using_Flow"


def test_apex_flow_edge_merges_with_real_flow_node(tmp_path: Path) -> None:
    """The Apex-side stub merges with the real ``flow_<name>`` node by shared id.

    The bundled ``sf_AccountFlow.flow-meta.xml`` fixture yields ``flow_sf_accountflow``
    (lowercased API name); Apex launching ``new Flow.Interview.sf_AccountFlow(...)``
    must resolve to that same node — no dangling, no duplicate.
    """
    import shutil

    shutil.copy(FIXTURES / "sf_AccountFlow.flow-meta.xml", tmp_path)
    (tmp_path / "Launcher.cls").write_text(
        "public class Launcher {\n"
        "    public void go() {\n"
        "        Flow.Interview.sf_AccountFlow f = new Flow.Interview.sf_AccountFlow(null);\n"
        "        f.start();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    result = extract_sf(tmp_path)
    flow_nodes = [n for n in result["nodes"] if n["id"] == "flow_sf_accountflow"]
    # Exactly one flow node — the stub merged into the real, richer one.
    assert len(flow_nodes) == 1
    assert flow_nodes[0].get("sf_trigger_object") == "Account"  # from the real parse
    assert ("apex_launcher", "flow_sf_accountflow") in _flow_calls(result["edges"])

    node_ids = {n["id"] for n in result["nodes"]}
    for e in result["edges"]:
        assert e["source"] in node_ids and e["target"] in node_ids


def test_ordinary_new_is_not_a_flow_edge(tmp_path: Path) -> None:
    """A regular ``new X()`` produces ``instantiates``, never a flow ``calls`` edge."""
    (tmp_path / "Dto.cls").write_text(
        "public class Dto { public String x; }\n", encoding="utf-8"
    )
    (tmp_path / "User.cls").write_text(
        "public class User {\n"
        "    public void run() { Dto d = new Dto(); }\n"
        "}\n",
        encoding="utf-8",
    )

    res = extract_apex_enhanced(tmp_path / "User.cls")
    assert not _flow_calls(res["edges"])


def test_multiple_flow_launches_dedupe_per_flow(tmp_path: Path) -> None:
    """Two launches of the same flow -> one edge; two distinct flows -> two edges."""
    cls = tmp_path / "Runner.cls"
    cls.write_text(
        "public class Runner {\n"
        "    public void a() {\n"
        "        Flow.Interview.Alpha_Flow x = new Flow.Interview.Alpha_Flow(null);\n"
        "    }\n"
        "    public void b() {\n"
        "        Flow.Interview.Alpha_Flow y = new Flow.Interview.Alpha_Flow(null);\n"
        "        Flow.Interview.Beta_Flow z = new Flow.Interview.Beta_Flow(null);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    res = extract_apex_enhanced(cls)
    calls = _flow_calls(res["edges"])
    assert calls == {
        ("apex_runner", "flow_alpha_flow"),
        ("apex_runner", "flow_beta_flow"),
    }
    # Alpha launched twice, collected once.
    flow_ids = [n["id"] for n in res["nodes"] if n["file_type"] == "flow"]
    assert sorted(flow_ids) == ["flow_alpha_flow", "flow_beta_flow"]
