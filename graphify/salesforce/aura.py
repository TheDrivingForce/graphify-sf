"""
graphify-sf: Aura component / application parser.

Parses the Aura *markup* files — ``*.cmp`` (component) and ``*.app``
(application) — into first-class graph nodes so the Aura UI layer is visible
alongside VF / LWC in impact analysis. Only the markup file is parsed: it is the
bundle's identity and carries every signal we model (the Apex ``controller=``
attribute, the child components it renders, the LWCs it depends on). The
supplemental bundle files (``*Controller.js`` / ``*Helper.js`` / ``*Renderer.js``,
``.css``, ``.design``, ``.svg``, ``.auradoc``, ``.evt``) are not parsed — mirroring
how the VF parser treats a page/component as one node.

Each file is parsed independently by the parallel, per-file pipeline
(``_extract_single_file`` must be picklable, ADR-008); cross-file links resolve
later via shared node IDs (ADR-002), exactly as the LWC / VF parsers do.

Extracted signal (per file):
    - an ``aura_component`` node for the bundle (``aura_<folder>``);
    - a ``calls`` edge to the Apex ``controller=`` class on the root
      ``<aura:component>`` / ``<aura:application>`` tag -> ``apex_<class>``;
    - ``embeds`` edges to each LOCAL custom child referenced via a ``c:`` tag
      (``<c:QuickCheckInCard>``). Because the ``c:`` namespace is shared by Aura
      components AND LWCs, the target is disambiguated by the framework's naming
      rule: an LWC name MUST start lowercase (``lwc_<name>``), an Aura component
      conventionally starts uppercase (``aura_<name>``). An Aura component can
      embed an LWC but not vice versa.
    - ``embeds`` edges to each LWC declared via ``<aura:dependency resource="X"/>``
      -> ``lwc_<X>`` (the LWCs a Lightning Out container ``.app`` surfaces).

CRITICAL (ADR-002 cross-file resolution): controller / child / LWC targets use
the same node IDs the owning parser produces, so ``build_sf_graph`` resolves the
link without a dedicated pass. Each target is emitted as a stub node to keep this
result free of dangling edges (ADR-012); the stub merges with the real node via
the shared id.

Error handling (ADR-009 lenient): a read / decode failure degrades to a single
``concept`` error node instead of raising. Regex-only — Aura markup is XML-ish
but not a reliable well-formed-XML contract (merge-field braces, HTML in
templates), so a tolerant regex scan is used rather than an XML parser.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The root ``<aura:component ...>`` / ``<aura:application ...>`` OPEN tag,
#: captured up to the first ``>``. Aura markup always opens with one of these;
#: the ``controller=`` attribute we care about lives on it. ``re.DOTALL`` lets
#: the tag span multiple lines (attributes are often wrapped). Only the FIRST
#: match is used — the root element.
_ROOT_TAG_RE = re.compile(
    r"<aura:(component|application)\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL
)

#: ``controller="QuickCheckInController"`` on the root tag — the Apex controller
#: class backing the bundle. Value captured without quotes.
_CONTROLLER_RE = re.compile(
    r"""\bcontroller\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)

#: ``<c:QuickCheckInCard ...>`` / ``<c:commsRuleEditForm>`` — a LOCAL custom child
#: (Aura component or LWC) rendered in this markup. The ``c:`` namespace is the
#: default for custom components; base tags (``aura:``, ``lightning:``,
#: ``force:``, ``ltng:``, standard HTML) are excluded. Captures the name after
#: the ``c:`` prefix, PRESERVING case (case disambiguates Aura vs LWC).
_EMBED_RE = re.compile(r"<c:([A-Za-z][A-Za-z0-9_]*)\b")

#: ``<aura:dependency resource="locationMap"/>`` — a Lightning Out container
#: ``.app`` declares each LWC it can surface as a dependency. Captures the
#: resource name (an LWC folder). A namespaced resource (``ns:foo``) keeps only
#: the name after the colon. Wildcard resources (``markup://c:*``) are ignored
#: by the identifier-only capture.
_DEPENDENCY_RE = re.compile(
    r"""<aura:dependency\b[^>]*\bresource\s*=\s*["'](?:markup://)?(?:[^"':]*:)?([A-Za-z][A-Za-z0-9_]*)["']""",
    re.IGNORECASE,
)


def _aura_bundle_id(path: Path) -> str:
    """Build the Aura bundle node ID from its folder name.

    An Aura component is a *bundle* of files in a folder named after the
    component (``QuickCheckIn/``); the markup file shares that folder name.
    Component identity is the FOLDER:
    ``.../aura/QuickCheckIn/QuickCheckIn.cmp`` -> ``aura_quickcheckin``.

    Keeping the id at ``aura_<folder>`` preserves the cross-file contract other
    parsers rely on (an ``embeds`` edge targets a component by this id, ADR-002).
    """
    return f"aura_{path.parent.name.lower()}"


def _aura_child_id(name: str) -> str:
    """Map an uppercase-initial ``c:`` tag name to an Aura component node ID.

    ``"QuickCheckInCard"`` -> ``"aura_quickcheckincard"``. Lowercasing mirrors
    ``_aura_bundle_id`` on the child's folder name, so the ``embeds`` edge
    resolves to the child's bundle node (cross-file resolution, ADR-002).
    """
    return f"aura_{name.lower()}"


def _lwc_bundle_id(name: str) -> str:
    """Map a lowercase-initial ``c:`` tag / dependency name to an LWC bundle ID.

    ``"commsRuleEditForm"`` -> ``"lwc_commsruleeditform"``. Lowercasing mirrors
    ``lwc._bundle_id`` on the LWC's camelCase folder, so the ``embeds`` edge
    resolves to the LWC bundle node the LWC parser emits (cross-file resolution,
    ADR-002).
    """
    return f"lwc_{name.lower()}"


def _apex_class_nid(class_name: str) -> str:
    """Build the Apex class node ID matching ``apex_enhanced.py``.

    Mirrors ``apex_<class-stem>`` so a ``calls`` edge to the controller resolves
    to the Apex class node (cross-file resolution, ADR-002).
    """
    return f"apex_{class_name.lower()}"


def _resolve_embed(name: str) -> tuple[str, str, str]:
    """Resolve a ``c:`` child name to ``(node_id, file_type, kind)``.

    Disambiguates the shared ``c:`` namespace by the SF framework naming rule:
    an LWC's name MUST begin lowercase; an Aura component conventionally begins
    uppercase. So a lowercase-initial name is an embedded LWC, an uppercase one
    an embedded Aura component. Returns the target node id, the stub's
    ``file_type``, and a human ``kind`` label for the edge attribute.
    """
    if name[0].islower():
        return _lwc_bundle_id(name), "lwc_component", "lwc"
    return _aura_child_id(name), "aura_component", "aura"


def _parse_error_node(path: Path, error: Exception, file_id: str) -> dict:
    """Return a single ``concept`` error node for an unreadable Aura file."""
    return {
        "nodes": [
            {
                "id": file_id,
                "label": f"[Parse Error] {path.name}",
                "file_type": "concept",
                "source_file": str(path),
                "sf_error_type": "aura_parse_error",
                "sf_error_message": str(error),
            }
        ],
        "edges": [],
    }


def extract_aura(path: Path) -> dict:
    """Parse one Aura markup file — ``*.cmp`` (component) or ``*.app`` (application).

    Emits a single ``aura_component`` bundle node for the file plus:

      - a ``calls`` edge to the Apex ``controller=`` class -> ``apex_<class>``.
      - ``embeds`` edges to each local ``<c:...>`` child: an LWC (lowercase-initial
        name -> ``lwc_<name>``) or an Aura component (uppercase -> ``aura_<name>``).
      - ``embeds`` edges to each ``<aura:dependency resource="X"/>`` LWC (the LWCs
        a Lightning Out container app surfaces) -> ``lwc_<X>``.

    Referenced controllers, children, and LWCs are emitted as stub nodes so the
    result has no dangling edges; they merge with the real nodes via the shared ID
    (ADR-002, ADR-012).

    Returns:
        ``{"nodes": [...], "edges": [...]}``; a ``concept`` error node on read
        failure (graceful degradation, ADR-009).
    """
    path = Path(path)
    file_id = _aura_bundle_id(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        return _parse_error_node(path, exc, file_id)

    is_app = path.suffix.lower() == ".app"

    self_node: dict = {
        "id": file_id,
        "label": path.parent.name,
        "file_type": "aura_component",
        "source_file": str(path),
        "sf_aura_type": "application" if is_app else "component",
    }
    nodes: list[dict] = [self_node]
    edges: list[dict] = []

    def _ensure_stub(node_id: str, node_label: str, node_type: str) -> None:
        if not any(n["id"] == node_id for n in nodes):
            nodes.append(
                {
                    "id": node_id,
                    "label": node_label,
                    "file_type": node_type,
                    "source_file": str(path),
                }
            )

    # Attributes we care about live on the root <aura:component>/<aura:application>
    # open tag. Scanning only that tag avoids matching a ``controller=`` that
    # appears in unrelated child markup.
    root = _ROOT_TAG_RE.search(content)
    attrs = root.group("attrs") if root else ""

    # 1. Apex controller -> calls edge ------------------------------------
    ctrl = _CONTROLLER_RE.search(attrs)
    if ctrl:
        class_name = ctrl.group(1).strip()
        apex_id = _apex_class_nid(class_name)
        _ensure_stub(apex_id, class_name, "code")
        edges.append(
            {
                "source": file_id,
                "target": apex_id,
                "relation": "calls",
                "context": "call",
                "confidence": "EXTRACTED",
                "source_file": str(path),
                "sf_aura_controller": class_name,
            }
        )

    # 2. <c:...> child embeds -> embeds edges (Aura -> Aura or Aura -> LWC) --
    seen_embed: set[str] = set()
    for m in _EMBED_RE.finditer(content):
        child_name = m.group(1)
        child_id, child_type, kind = _resolve_embed(child_name)
        if child_id == file_id or child_id in seen_embed:
            continue  # ignore self-reference / duplicate tags
        seen_embed.add(child_id)
        _ensure_stub(child_id, child_name, child_type)
        edges.append(
            {
                "source": file_id,
                "target": child_id,
                "relation": "embeds",
                "confidence": "EXTRACTED",
                "source_file": str(path),
                "sf_embedded_tag": f"c:{child_name}",
                "sf_embed_kind": kind,
            }
        )

    # 3. <aura:dependency resource="X"/> -> embeds edges (Aura app -> LWC) ---
    # A Lightning Out container ``.app`` declares each LWC it surfaces as a
    # dependency; these are the LWCs the VF ``$Lightning.createComponent`` calls
    # actually mount. Treat each as an embedded LWC (``lwc_<X>``).
    for m in _DEPENDENCY_RE.finditer(content):
        resource = m.group(1)
        lwc_id = _lwc_bundle_id(resource)
        if lwc_id == file_id or lwc_id in seen_embed:
            continue
        seen_embed.add(lwc_id)
        _ensure_stub(lwc_id, resource, "lwc_component")
        edges.append(
            {
                "source": file_id,
                "target": lwc_id,
                "relation": "embeds",
                "confidence": "INFERRED",
                "confidence_value": 0.9,
                "source_file": str(path),
                "sf_aura_dependency": resource,
            }
        )

    return {"nodes": nodes, "edges": edges}
