"""Orphan-gated Apex type-reference (``references``) edge tests (ADR-104).

A class used ONLY as a declared type — variable / parameter / return / field —
with no ``new X()`` and no method calls (pure DTO / wrapper) would be an orphan
node. ``apex_ts`` collects those declarations as ``sf_unresolved_type_refs`` and
``resolve_apex_type_refs`` links them, but only toward classes with no other
incoming usage edge, so routine declarations never flood the graph.
"""

from __future__ import annotations

from pathlib import Path

from graphify.salesforce import extract_sf
from graphify.salesforce.apex_enhanced import extract_apex_enhanced


def _references(edges: list[dict]) -> set[tuple[str, str]]:
    return {
        (e["source"], e["target"])
        for e in edges
        if e["relation"] == "references"
    }


def _write_dto(tmp_path: Path) -> None:
    """A pure DTO: fields only, no methods, never ``new``-ed by the tests' consumers."""
    (tmp_path / "FlockDto.cls").write_text(
        "public class FlockDto {\n"
        "    public String name;\n"
        "    public Boolean success;\n"
        "}\n",
        encoding="utf-8",
    )


def test_type_only_dto_gets_references_edge(tmp_path: Path) -> None:
    """A DTO used only as a local var type is linked via ``references``."""
    _write_dto(tmp_path)
    (tmp_path / "Consumer.cls").write_text(
        "public class Consumer {\n"
        "    public void run() {\n"
        "        FlockDto response = Svc.fetch();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    result = extract_sf(tmp_path)
    refs = [e for e in result["edges"] if e["relation"] == "references"]
    assert ("apex_consumer", "apex_flockdto") in _references(result["edges"])
    edge = next(e for e in refs if e["target"] == "apex_flockdto")
    assert edge["context"] == "type_ref"
    assert edge["confidence"] == "EXTRACTED"
    # Deferred metadata never leaks into the exported graph.
    assert all("sf_unresolved_type_refs" not in n for n in result["nodes"])


def test_all_consumers_link_to_an_orphan_dto(tmp_path: Path) -> None:
    """Every class referencing an orphan DTO gets its own edge (fan-in = all)."""
    _write_dto(tmp_path)
    (tmp_path / "ConsumerA.cls").write_text(
        "public class ConsumerA {\n"
        "    public void handle(FlockDto input) {}\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "ConsumerB.cls").write_text(
        "public class ConsumerB {\n"
        "    private FlockDto cached;\n"
        "}\n",
        encoding="utf-8",
    )

    refs = _references(extract_sf(tmp_path)["edges"])
    assert ("apex_consumera", "apex_flockdto") in refs
    assert ("apex_consumerb", "apex_flockdto") in refs


def test_instantiated_class_gets_no_references_edge(tmp_path: Path) -> None:
    """The orphan gate: a class already linked by ``instantiates`` needs no fallback."""
    _write_dto(tmp_path)
    (tmp_path / "Builder.cls").write_text(
        "public class Builder {\n"
        "    public void build() {\n"
        "        FlockDto d = new FlockDto();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    edges = extract_sf(tmp_path)["edges"]
    instantiates = {
        (e["source"], e["target"]) for e in edges if e["relation"] == "instantiates"
    }
    assert ("apex_builder", "apex_flockdto") in instantiates
    assert ("apex_builder", "apex_flockdto") not in _references(edges)


def test_called_class_gets_no_references_edge(tmp_path: Path) -> None:
    """The orphan gate also respects ``calls`` edges into the class's methods."""
    (tmp_path / "Svc.cls").write_text(
        "public class Svc {\n"
        "    public static void run() {}\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "Caller.cls").write_text(
        "public class Caller {\n"
        "    private Svc unusedField;\n"
        "    public void go() { Svc.run(); }\n"
        "}\n",
        encoding="utf-8",
    )

    edges = extract_sf(tmp_path)["edges"]
    calls = {(e["source"], e["target"]) for e in edges if e["relation"] == "calls"}
    assert ("apex_caller_go", "apex_svc_run") in calls
    assert ("apex_caller", "apex_svc") not in _references(edges)


def test_profile_grant_does_not_defeat_orphan_gate(tmp_path: Path) -> None:
    """``grants_access_to`` is administrative, not usage — the DTO still links.

    A profile grants class access to nearly every class in an org; that edge
    must not mark a type-only DTO as "already referenced" (ADR-104).
    """
    from graphify.salesforce.apex_calls import resolve_apex_type_refs

    _write_dto(tmp_path)
    (tmp_path / "Consumer.cls").write_text(
        "public class Consumer {\n"
        "    public void handle(FlockDto input) {}\n"
        "}\n",
        encoding="utf-8",
    )

    nodes: list[dict] = []
    edges: list[dict] = []
    for f in sorted(tmp_path.glob("*.cls")):
        res = extract_apex_enhanced(f)
        nodes += res["nodes"]
        edges += res["edges"]
    edges.append(
        {
            "source": "profile_admin",
            "target": "apex_flockdto",
            "relation": "grants_access_to",
            "confidence": "EXTRACTED",
        }
    )

    new_edges = resolve_apex_type_refs(nodes, edges)
    assert ("apex_consumer", "apex_flockdto") in _references(new_edges)


def test_collection_covers_all_declaration_positions(tmp_path: Path) -> None:
    """Locals, params, return types, fields, and generic elements are collected.

    Checks the deferred ``sf_unresolved_type_refs`` metadata directly (before the
    resolution pass consumes it): each declared type name appears exactly once
    per class regardless of how many declarations mention it, builtins and
    self-references never do.
    """
    cls = tmp_path / "Kitchen.cls"
    cls.write_text(
        "public class Kitchen {\n"
        "    private FieldDto stored;\n"
        "    public Map<Id, MapValueDto> lookup;\n"
        "    public ReturnDto fetch(ParamDto input) { return null; }\n"
        "    public void run() {\n"
        "        LocalDto x = fetch(null);\n"
        "        List<ListDto> items = null;\n"
        "        LocalDto again = null;\n"
        "        String builtin = null;\n"
        "        Kitchen self = null;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    res = extract_apex_enhanced(cls)
    pending = res["nodes"][0].get("sf_unresolved_type_refs", [])
    names = [r["type_name"] for r in pending]
    assert set(names) == {
        "FieldDto", "MapValueDto", "ReturnDto", "ParamDto", "LocalDto", "ListDto",
    }
    # Deduped per class: LocalDto declared twice, collected once.
    assert len(names) == len(set(names))
    assert all(r["caller_id"] == "apex_kitchen" for r in pending)


def test_ambiguous_type_name_is_skipped(tmp_path: Path) -> None:
    """Two classes with the same label: the type name resolves to no edge."""
    (tmp_path / "DupOne.cls").write_text(
        "public class Dup {\n    public String a;\n}\n", encoding="utf-8"
    )
    (tmp_path / "DupTwo.cls").write_text(
        "public class Dup {\n    public String b;\n}\n", encoding="utf-8"
    )
    (tmp_path / "User1.cls").write_text(
        "public class User1 {\n    private Dup ref;\n}\n", encoding="utf-8"
    )

    refs = _references(extract_sf(tmp_path)["edges"])
    assert not any(s == "apex_user1" for s, _t in refs)


def test_sfdx_cache_dir_is_not_parsed(tmp_path: Path) -> None:
    """``.sfdx`` (Salesforce CLI cache) is pruned from the extraction walk.

    Its ``StandardApexLibrary`` holds thousands of standard-library ``.cls``
    stubs that are not org source; before the ADR-104 walk fix they flooded the
    graph (64% of eventspark's nodes) and hijacked type-name resolution.
    """
    _write_dto(tmp_path)
    stub_dir = tmp_path / ".sfdx" / "tools" / "262" / "StandardApexLibrary" / "System"
    stub_dir.mkdir(parents=True)
    (stub_dir / "RestRequest.cls").write_text(
        "global class RestRequest {\n    global String httpMethod;\n}\n",
        encoding="utf-8",
    )

    node_ids = {n["id"] for n in extract_sf(tmp_path)["nodes"]}
    assert "apex_flockdto" in node_ids
    assert "apex_restrequest" not in node_ids


def test_no_type_refs_flag_suppresses_edges(tmp_path: Path) -> None:
    """``--no-type-refs``: no ``references`` edges, and no metadata leakage."""
    _write_dto(tmp_path)
    (tmp_path / "Consumer.cls").write_text(
        "public class Consumer {\n"
        "    public void handle(FlockDto input) {}\n"
        "}\n",
        encoding="utf-8",
    )

    result = extract_sf(tmp_path, no_type_refs=True)
    assert not _references(result["edges"])
    assert all("sf_unresolved_type_refs" not in n for n in result["nodes"])
