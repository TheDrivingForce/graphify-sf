"""
graphify-sf: Visualforce page / component parser.

Parses the two classic Visualforce markup files — ``*.page`` and
``*.component`` — into first-class graph nodes so a legacy VF UI layer is
visible alongside LWC / Aura in impact analysis. Each file is parsed
independently by the parallel, per-file pipeline (``_extract_single_file`` must
be picklable, ADR-008); cross-file links resolve later via shared node IDs
(ADR-002), exactly as the LWC parser does.

Extracted signal (per file):
    - a node for the page (``vf_page``) or component (``vf_component``);
    - ``calls`` edges to each Apex controller referenced on the root tag —
      ``controller=`` and every comma-separated name in ``extensions=`` — each
      targeting the SAME ``apex_<class>`` node id the Apex parser emits;
    - a ``calls`` edge to the SObject named by ``standardController=`` (a standard
      or custom object, NOT Apex), targeting ``sobject_<name>`` via
      ``constants.sobject_nid`` so it merges with the object's real node;
    - ``embeds`` edges to each LOCAL custom VF component referenced via a ``c:``
      tag (``<c:themeLoader>``), targeting the component's ``vf_component_<name>``
      node.
    - ``embeds`` edges to each LWC surfaced via **Lightning Out** —
      ``$Lightning.createComponent("<ns>:<lwcName>", ...)`` — targeting the LWC
      bundle node ``lwc_<name>`` the LWC parser emits.

CRITICAL (ADR-002 cross-file resolution): controller / SObject / embed targets
use the same node IDs the owning parser produces, so ``build_sf_graph`` resolves
the link without a dedicated pass. Each target is emitted as a stub node to keep
this result free of dangling edges (ADR-012); the stub merges with the real node
via the shared id.

Error handling (ADR-009 lenient): a read / decode failure degrades to a single
``concept`` error node instead of raising, so the rest of the repo keeps
analyzing. Regex-only — VF markup is not a well-formed XML contract in practice
(unescaped ``&``, HTML5 void tags, merge-field braces), so a tolerant regex scan
over the root tag's attributes is used rather than an XML parser.
"""

from __future__ import annotations

import re
from pathlib import Path

from graphify.salesforce.constants import sobject_nid

#: The root ``<apex:page ...>`` / ``<apex:component ...>`` OPEN tag, captured up
#: to the first ``>``. VF markup always opens with one of these two tags; the
#: attributes we care about (controller / extensions / standardController) live
#: on it. ``re.DOTALL`` lets the tag span multiple lines (attributes are often
#: wrapped). Only the FIRST match is used — the root element.
_ROOT_TAG_RE = re.compile(
    r"<apex:(page|component)\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL
)

#: ``controller="EventSiteController"`` on the root tag — a single custom Apex
#: controller class. Value captured without quotes.
_CONTROLLER_RE = re.compile(
    r"""\bcontroller\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)

#: ``extensions="A,B,C"`` on the root tag — one or more Apex controller-extension
#: classes, comma-separated. The raw list is captured; split downstream.
_EXTENSIONS_RE = re.compile(
    r"""\bextensions\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)

#: ``standardController="Contact"`` — a standard/custom SObject the page is bound
#: to (NOT an Apex class). Value captured without quotes.
_STANDARD_CONTROLLER_RE = re.compile(
    r"""\bstandardController\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)

#: ``<c:themeLoader ...>`` / ``<c:PageLoadingOverlay>`` — a LOCAL custom VF
#: component embedded in this markup. The ``c:`` namespace is the default for
#: custom components; base (``apex:``) tags and standard HTML are excluded.
#: Captures the component name after the ``c:`` prefix. Case-insensitive on the
#: prefix; the name is preserved as written (VF is case-insensitive, normalized
#: to lowercase for the node id).
_EMBED_RE = re.compile(r"<c:([A-Za-z][A-Za-z0-9_]*)\b", re.IGNORECASE)

#: ``$Lightning.createComponent("evsprk:eventSpeakersByCategory", { ... }, ...)``
#: — Lightning Out, the mechanism that surfaces an LWC (or Aura component) inside
#: Visualforce. The first argument is a ``"<namespace>:<componentName>"`` string;
#: capture the component name after the colon. The namespace (``evsprk``, ``c``,
#: a managed-package prefix) is discarded — component identity is the name, which
#: is the LWC's camelCase folder. ``$Lightning.use("...:LightningOutApp", ...)``
#: names the container Aura *app*, not the embedded component, so it is NOT
#: matched here (only ``createComponent`` is).
_LIGHTNING_OUT_RE = re.compile(
    r"""\$Lightning\.createComponent\(\s*["'][^"':]*:([A-Za-z][A-Za-z0-9_]*)["']""",
)


def _vf_node_id(path: Path) -> str:
    """Build the VF node ID from the file kind and stem.

    ``.../pages/event01home.page``          -> ``vf_page_event01home``
    ``.../components/themeLoader.component`` -> ``vf_component_themeloader``

    A page and a component may share a base name; keying the id on the file
    kind keeps them distinct while ``c:`` embeds (which reference components)
    resolve only to ``vf_component_<name>`` (see ``_embedded_component_id``).
    """
    kind = "page" if path.suffix.lower() == ".page" else "component"
    return f"vf_{kind}_{path.stem.lower()}"


def _embedded_component_id(name: str) -> str:
    """Map a ``c:`` tag name to its component node ID.

    ``"themeLoader"`` -> ``"vf_component_themeloader"``. Lowercasing mirrors
    ``_vf_node_id`` on the component's own file stem, so an ``embeds`` edge
    resolves to the node that component's parser emits (cross-file resolution,
    ADR-002). A ``c:`` tag always names a component (pages are not embeddable).
    """
    return f"vf_component_{name.lower()}"


def _lwc_bundle_id(name: str) -> str:
    """Map a Lightning Out component name to its LWC bundle node ID.

    ``"eventSpeakersByCategory"`` -> ``"lwc_eventspeakersbycategory"``.
    Lowercasing mirrors ``lwc._bundle_id`` on the component's camelCase folder
    name, so an ``embeds`` edge resolves to the LWC bundle node the LWC parser
    emits (cross-file resolution, ADR-002). If the referenced component is
    actually an Aura component (Lightning Out can host either), the stub simply
    won't merge — the same graceful outcome as any unresolved embed.
    """
    return f"lwc_{name.lower()}"


def _apex_class_nid(class_name: str) -> str:
    """Build the Apex class node ID matching ``apex_enhanced.py``.

    Mirrors ``apex_<class-stem>`` so a ``calls`` edge to a controller /
    extension resolves to the class node the Apex parser emits (cross-file
    resolution, ADR-002).
    """
    return f"apex_{class_name.lower()}"


def _parse_error_node(path: Path, error: Exception, file_id: str) -> dict:
    """Return a single ``concept`` error node for an unreadable VF file."""
    return {
        "nodes": [
            {
                "id": file_id,
                "label": f"[Parse Error] {path.name}",
                "file_type": "concept",
                "source_file": str(path),
                "sf_error_type": "visualforce_parse_error",
                "sf_error_message": str(error),
            }
        ],
        "edges": [],
    }


def extract_visualforce(path: Path) -> dict:
    """Parse one Visualforce ``*.page`` or ``*.component`` file.

    Emits a single ``vf_page`` / ``vf_component`` node for the file plus:

      - ``calls`` edges to each Apex controller (``controller=``) and extension
        (``extensions="A,B"``) named on the root tag -> ``apex_<class>`` nodes.
      - a ``calls`` edge to the ``standardController=`` SObject (if any) ->
        ``sobject_<name>`` node (a data binding, not an Apex class).
      - ``embeds`` edges to each LOCAL ``<c:...>`` custom component ->
        ``vf_component_<name>`` node.
      - ``embeds`` edges to each LWC surfaced via Lightning Out
        (``$Lightning.createComponent("<ns>:<name>", ...)``) -> ``lwc_<name>`` node.

    Referenced controllers, SObjects, embedded components, and Lightning Out LWCs
    are emitted as stub nodes so the result has no dangling edges; they merge with
    the real nodes via the shared ID (ADR-002, ADR-012).

    Returns:
        ``{"nodes": [...], "edges": [...]}``; a ``concept`` error node on read
        failure (graceful degradation, ADR-009).
    """
    path = Path(path)
    file_id = _vf_node_id(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        return _parse_error_node(path, exc, file_id)

    is_page = path.suffix.lower() == ".page"
    file_type = "vf_page" if is_page else "vf_component"
    label = f"{path.stem}.{'page' if is_page else 'component'}"

    self_node: dict = {
        "id": file_id,
        "label": label,
        "file_type": file_type,
        "source_file": str(path),
    }
    nodes: list[dict] = [self_node]
    edges: list[dict] = []

    # Attributes we care about live on the root <apex:page>/<apex:component>
    # open tag. Scanning only that tag (not the whole body) avoids matching a
    # ``controller=`` that appears in unrelated child markup / comments.
    root = _ROOT_TAG_RE.search(content)
    attrs = root.group("attrs") if root else ""

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

    # 1. Apex controllers: controller= + extensions="A,B,C" -> calls edges -----
    apex_classes: list[str] = []
    ctrl = _CONTROLLER_RE.search(attrs)
    if ctrl:
        apex_classes.append(ctrl.group(1).strip())
    ext = _EXTENSIONS_RE.search(attrs)
    if ext:
        apex_classes.extend(
            name.strip() for name in ext.group(1).split(",") if name.strip()
        )

    seen_apex: set[str] = set()
    for class_name in apex_classes:
        apex_id = _apex_class_nid(class_name)
        if apex_id in seen_apex:
            continue
        seen_apex.add(apex_id)
        _ensure_stub(apex_id, class_name, "code")
        edges.append(
            {
                "source": file_id,
                "target": apex_id,
                "relation": "calls",
                "context": "call",
                "confidence": "EXTRACTED",
                "source_file": str(path),
                "sf_vf_controller": class_name,
            }
        )

    # 2. standardController="Contact" -> calls edge to the SObject -------------
    # A standard/custom object binding, not an Apex class: target the object
    # node (sobject_<name>) so a VF page on a standard object still shows its
    # data dependency (ADR-002 cross-file resolution).
    std = _STANDARD_CONTROLLER_RE.search(attrs)
    if std:
        sobject_name = std.group(1).strip()
        sobj_id = sobject_nid(sobject_name)
        _ensure_stub(sobj_id, sobject_name, "sobject")
        edges.append(
            {
                "source": file_id,
                "target": sobj_id,
                "relation": "calls",
                "context": "standard_controller",
                "confidence": "EXTRACTED",
                "source_file": str(path),
                "sf_standard_controller": sobject_name,
            }
        )

    # 3. <c:...> embeds -> embeds edges (this VF file -> child component) -------
    seen_embed: set[str] = set()
    for m in _EMBED_RE.finditer(content):
        child_name = m.group(1)
        child_id = _embedded_component_id(child_name)
        if child_id == file_id or child_id in seen_embed:
            continue  # ignore self-reference / duplicate tags
        seen_embed.add(child_id)
        _ensure_stub(child_id, child_name, "vf_component")
        edges.append(
            {
                "source": file_id,
                "target": child_id,
                "relation": "embeds",
                "confidence": "EXTRACTED",
                "source_file": str(path),
                "sf_embedded_tag": f"c:{child_name}",
            }
        )

    # 4. Lightning Out LWC embeds -> embeds edges (this VF file -> LWC bundle) --
    # ``$Lightning.createComponent("evsprk:eventSpeakersByCategory", ...)`` mounts
    # an LWC into the page's DOM. The component name after the ``<ns>:`` prefix is
    # the LWC's camelCase folder, so the edge targets ``lwc_<name>`` — the same id
    # the LWC parser emits (cross-file resolution, ADR-002).
    seen_lwc: set[str] = set()
    for m in _LIGHTNING_OUT_RE.finditer(content):
        lwc_name = m.group(1)
        lwc_id = _lwc_bundle_id(lwc_name)
        if lwc_id in seen_lwc:
            continue  # same LWC created more than once -> one edge
        seen_lwc.add(lwc_id)
        _ensure_stub(lwc_id, lwc_name, "lwc_component")
        edges.append(
            {
                "source": file_id,
                "target": lwc_id,
                "relation": "embeds",
                "confidence": "INFERRED",
                "confidence_value": 0.9,
                "source_file": str(path),
                "sf_lightning_out": lwc_name,
            }
        )

    return {"nodes": nodes, "edges": edges}
