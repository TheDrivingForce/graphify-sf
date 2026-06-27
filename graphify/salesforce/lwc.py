"""
graphify-sf: Lightning Web Component (LWC) parser.

Parses the two files that make up an LWC — the ``*.html`` template and the
``*.js`` controller — *separately* so each can be extracted by the parallel,
per-file pipeline (``_extract_single_file`` must be picklable, ADR-008). The
two results are reconciled later by ``merge_lwc_component()`` in the analysis
pass, NOT here.

Extracted signal:
    - ``extract_lwc_html``: an ``lwc_component`` node for the template, plus
      ``embeds`` edges to each LOCAL custom child component referenced via a
      ``c-`` tag (e.g. ``<c-badge-section>``).
    - ``extract_lwc_js``: an ``lwc_component`` node for the controller, plus
      ``wire_to`` edges (``@wire`` decorator -> imported Apex method) and
      ``@api`` public properties recorded as node attributes.

CRITICAL (ADR-002 / how-it-works "cross-file resolution"): ``wire_to`` edges
target the SAME Apex method node ID the Apex parser produces
(``apex_<class>_<method>``), so ``build_graph()`` resolves the LWC -> Apex link
without a dedicated pass. The referenced Apex method is emitted as a stub node
to keep this result free of dangling edges (ADR-012); the stub merges with the
real method node from ``apex_enhanced.py``.

Error handling (ADR-009 lenient): a read / decode failure degrades to a single
``concept`` error node instead of raising, so the rest of the repo keeps
analyzing. Regex-only — no JavaScript / HTML AST (the prohibition is explicit:
regex is sufficient for ``@wire`` / ``@api`` detection).
"""

from __future__ import annotations

import re
from pathlib import Path

#: ``@wire(<reference>, { ... })`` — capture the decorator's first argument
#: (the imported adapter / Apex method name).
_WIRE_RE = re.compile(r"@wire\(\s*([^,)\s]+)")

#: ``import getAccounts from '@salesforce/apex/AccountService.getAccounts';``
#: Captures (local import name, Apex class, Apex method).
_APEX_IMPORT_RE = re.compile(
    r"import\s+(\w+)\s+from\s+['\"]@salesforce/apex/([^/.'\"]+)\.([^/'\"]+)['\"]"
)

#: ``@api recordId;`` / ``@api label;`` — capture the public property name.
_API_PROP_RE = re.compile(r"@api\s+(\w+)")

#: ``export default class MyComponent extends LightningElement`` — the real
#: component name (more reliable than the file stem, which may carry prefixes).
_CLASS_DECL_RE = re.compile(r"export\s+default\s+class\s+(\w+)")

#: ``<c-badge-section ...>`` / ``<c-badge-section/>`` — a LOCAL custom LWC
#: embedded in a template. The ``c-`` namespace is the default for custom LWCs;
#: base (``lightning-``) and managed-package namespaces are intentionally
#: excluded. Captures the kebab-case component name after the ``c-`` prefix.
_EMBED_RE = re.compile(r"<c-([a-z0-9]+(?:-[a-z0-9]+)*)\b")


def _lwc_id(path: Path) -> str:
    """Build the component node ID (``lwc_<lowercased-stem>``).

    The HTML and JS files of one component share a stem, so both parsers derive
    the same base ID; the file-type suffix (html/js) is recorded as a node
    attribute rather than baked into the ID.
    """
    return f"lwc_{path.stem.lower()}"


def _embedded_lwc_id(tag_name: str) -> str:
    """Map a kebab-case ``c-`` tag name to its component node ID.

    ``"badge-section"`` -> ``"lwc_badgesection"``. Removing hyphens and
    lowercasing mirrors ``_lwc_id`` on the camelCase folder name, so the edge
    resolves to the same node (cross-file resolution, ADR-002).
    """
    return f"lwc_{tag_name.replace('-', '').lower()}"


def _apex_method_nid(apex_class: str, apex_method: str) -> str:
    """Build the Apex method node ID matching ``apex_enhanced.py``.

    Mirrors ``apex_<class-stem>_<method>`` so a ``wire_to`` edge resolves to the
    method node the Apex parser emits (cross-file resolution, ADR-002).
    """
    return f"apex_{apex_class.lower()}_{apex_method.lower()}"


def _parse_error_node(path: Path, error_type: str, error: Exception) -> dict:
    """Return a single ``concept`` error node for an unreadable LWC file."""
    return {
        "nodes": [
            {
                "id": _lwc_id(path),
                "label": f"[Parse Error] {path.name}",
                "file_type": "concept",
                "source_file": str(path),
                "sf_error_type": error_type,
                "sf_error_message": str(error),
            }
        ],
        "edges": [],
    }


def extract_lwc_html(path: Path) -> dict:
    """Parse an LWC ``*.html`` template file.

    Emits a single ``lwc_component`` node tagged ``sf_lwc_file_type: "html"``,
    plus an ``embeds`` edge for every LOCAL custom child component referenced in
    the template via a ``c-`` tag (e.g. ``<c-badge-section>``). Base Lightning
    components (``<lightning-...>``), Aura, and standard HTML are ignored. The
    template structure / event handlers are otherwise not modelled — the JS
    controller carries the analyzable behavior; the HTML node exists so the merge
    pass can pair it with its controller.

    The ``embeds`` edge is sourced from the BASE component ID (``lwc_<stem>``),
    not this HTML sidecar node (``lwc_<stem>_html``): ``_merge_lwc_components``
    folds the sidecar into the base node and drops it, so a sidecar-sourced edge
    would become dangling. Each child is emitted as a stub ``lwc_component`` node
    (ADR-012, no dangling edges) that merges with the child's real node via the
    shared ID (cross-file resolution, ADR-002).

    Returns:
        ``{"nodes": [...], "edges": [...]}``; a ``concept`` error node on read
        failure (graceful degradation, ADR-009).
    """
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            html_content = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        return _parse_error_node(path, "html_parse_error", exc)

    base_id = _lwc_id(path)
    nodes: list[dict] = [
        {
            "id": f"{base_id}_html",
            "label": f"{path.stem} (HTML)",
            "file_type": "lwc_component",
            "source_file": str(path),
            "sf_lwc_file_type": "html",
        }
    ]
    edges: list[dict] = []

    # ``<c-...>`` embeds -> embeds edges (LWC -> child LWC component).
    seen: set[str] = set()
    for m in _EMBED_RE.finditer(html_content):
        child_id = _embedded_lwc_id(m.group(1))
        if child_id == base_id or child_id in seen:
            continue  # ignore self-reference / duplicate tags
        seen.add(child_id)
        nodes.append(
            {
                "id": child_id,
                "label": m.group(1),
                "file_type": "lwc_component",
                "source_file": str(path),
            }
        )
        edges.append(
            {
                "source": base_id,
                "target": child_id,
                "relation": "embeds",
                "confidence": "EXTRACTED",
                "source_file": str(path),
                "sf_embedded_tag": f"c-{m.group(1)}",
            }
        )

    return {"nodes": nodes, "edges": edges}


def extract_lwc_js(path: Path) -> dict:
    """Parse an LWC ``*.js`` controller file.

    Extracts ``@wire`` decorators (-> ``wire_to`` edges to the imported Apex
    method) and ``@api`` public properties (recorded as
    ``sf_api_property_<name>`` node attributes).

    Returns:
        ``{"nodes": [...], "edges": [...]}``. Referenced Apex methods are emitted
        as stub ``code`` nodes so the result has no dangling edges; they merge
        with the real method nodes via the shared ID (ADR-002, ADR-012). A
        ``concept`` error node is returned on read failure (ADR-009).
    """
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            js_content = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        return _parse_error_node(path, "js_parse_error", exc)

    lwc_id = _lwc_id(path)
    class_decl = _CLASS_DECL_RE.search(js_content)
    component_node: dict = {
        "id": lwc_id,
        "label": class_decl.group(1) if class_decl else path.stem,
        "file_type": "lwc_component",
        "source_file": str(path),
        "sf_lwc_file_type": "js",
    }
    nodes: list[dict] = [component_node]
    edges: list[dict] = []

    # Map @salesforce/apex import name -> (class, method).
    apex_imports = {
        m.group(1): (m.group(2), m.group(3))
        for m in _APEX_IMPORT_RE.finditer(js_content)
    }

    def _ensure_apex_stub(apex_class: str, apex_method: str) -> str:
        apex_id = _apex_method_nid(apex_class, apex_method)
        if not any(n["id"] == apex_id for n in nodes):
            nodes.append(
                {
                    "id": apex_id,
                    "label": f"{apex_class}.{apex_method}",
                    "file_type": "code",
                    "source_file": str(path),
                    "sf_method_type": "method",
                }
            )
        return apex_id

    # 1. @wire decorators -> wire_to edges (LWC -> Apex method) -------------
    wired: set[str] = set()
    for wire_match in _WIRE_RE.finditer(js_content):
        wire_ref = wire_match.group(1)
        if wire_ref not in apex_imports:
            # @wire to a non-Apex adapter (e.g. getRecord, MessageContext) — not Apex.
            continue
        wired.add(wire_ref)
        apex_class, apex_method = apex_imports[wire_ref]
        edges.append(
            {
                "source": lwc_id,
                "target": _ensure_apex_stub(apex_class, apex_method),
                "relation": "wire_to",
                "confidence": "INFERRED",
                "confidence_value": 0.85,
                "source_file": str(path),
                "sf_wire_method": apex_method,
            }
        )

    # 2. Imperative Apex imports -> lwc_calls edges (LWC -> Apex method) -----
    # An ``@salesforce/apex/Class.method`` import NOT consumed by @wire is called
    # imperatively (e.g. ``init({...}).then(...)``). The Apex dependency is real
    # and matters for impact analysis, so it is captured even without @wire.
    for import_name, (apex_class, apex_method) in apex_imports.items():
        if import_name in wired:
            continue
        edges.append(
            {
                "source": lwc_id,
                "target": _ensure_apex_stub(apex_class, apex_method),
                "relation": "lwc_calls",
                "confidence": "INFERRED",
                "confidence_value": 0.8,
                "source_file": str(path),
                "sf_apex_method": apex_method,
            }
        )

    # 3. @api public properties -> node attributes -------------------------
    for prop_match in _API_PROP_RE.finditer(js_content):
        component_node[f"sf_api_property_{prop_match.group(1)}"] = True

    return {"nodes": nodes, "edges": edges}
