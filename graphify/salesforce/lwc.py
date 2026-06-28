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

#: ``import { fireActionClickEvent, prepBookingData } from 'c/eventBookingUtils';``
#: A LOCAL LWC module import (the ``c/`` namespace). JS-only "service" modules
#: with no template expose named functions other components import and call.
#: Captures (brace-list of names, module name). Default-import / namespaced
#: package imports (``lightning/...``, ``@salesforce/...``) are NOT matched.
_MODULE_IMPORT_RE = re.compile(
    r"import\s*\{([^}]*)\}\s*from\s*['\"]c/(\w+)['\"]"
)

#: ``export { foo, bar };`` — the named-export block of a service module.
#: Captures the brace list; each name becomes a callable function node.
_EXPORT_BLOCK_RE = re.compile(r"export\s*\{([^}]*)\}")

#: ``export function foo(`` / ``export const foo =`` / ``export let foo =`` —
#: inline named exports. Captures the exported function name.
_EXPORT_INLINE_RE = re.compile(
    r"export\s+(?:async\s+)?(?:function|const|let|var)\s+(\w+)"
)


def _bundle_id(path: Path) -> str:
    """Build the LWC *bundle* (component) node ID from its folder name.

    An LWC is a bundle of files in a folder named after the component
    (``upcomingEventsViews/``); the main JS/HTML share that folder name but the
    bundle may also hold supplemental ``*.js`` / ``*.html`` files whose stems
    differ. Component identity is therefore the FOLDER, not the file stem:
    ``.../lwc/eventSpeakers/eventSpeakersLayoutPanel.html`` -> ``lwc_eventspeakers``.

    Keeping the bundle id at ``lwc_<folder>`` preserves the external contract
    other parsers rely on (``embeds`` / ``wire_to`` / ``imports`` / ``calls``
    target a component by this id — cross-file resolution, ADR-002).
    """
    return f"lwc_{path.parent.name.lower()}"


def _js_file_id(path: Path) -> str:
    """Node ID for one JS file within a bundle (``<bundle>_js_<stem>``)."""
    return f"{_bundle_id(path)}_js_{path.stem.lower()}"


def _html_file_id(path: Path) -> str:
    """Node ID for one HTML file within a bundle (``<bundle>_html_<stem>``)."""
    return f"{_bundle_id(path)}_html_{path.stem.lower()}"


def _is_main_file(path: Path) -> bool:
    """True when this file's stem matches its bundle folder (the entry file).

    Exactly one JS and one HTML file in a bundle may share the folder name — the
    main controller / main template; all others are supplemental.
    """
    return path.stem.lower() == path.parent.name.lower()


def _embedded_lwc_id(tag_name: str) -> str:
    """Map a kebab-case ``c-`` tag name to its component node ID.

    ``"badge-section"`` -> ``"lwc_badgesection"``. Removing hyphens and
    lowercasing mirrors ``_lwc_id`` on the camelCase folder name, so the edge
    resolves to the same node (cross-file resolution, ADR-002).
    """
    return f"lwc_{tag_name.replace('-', '').lower()}"


def _module_lwc_id(module_name: str) -> str:
    """Map a ``c/`` import module name to its component node ID.

    ``"eventBookingUtils"`` -> ``"lwc_eventbookingutils"``. Lowercasing mirrors
    ``_lwc_id`` on the module's own file stem, so an ``imports`` edge resolves to
    the same node the module's JS parser emits (cross-file resolution, ADR-002).
    """
    return f"lwc_{module_name.lower()}"


def _lwc_function_nid(component_id: str, fn_name: str) -> str:
    """Build a JS-module function node ID (``<lwc_id>_<fn>``, lowercased).

    Mirrors the Apex ``apex_<class>_<method>`` convention so a consumer's
    ``calls`` edge resolves to the function node the exporting module emits.
    """
    return f"{component_id}_{fn_name.lower()}"


def _apex_method_nid(apex_class: str, apex_method: str) -> str:
    """Build the Apex method node ID matching ``apex_enhanced.py``.

    Mirrors ``apex_<class-stem>_<method>`` so a ``wire_to`` edge resolves to the
    method node the Apex parser emits (cross-file resolution, ADR-002).
    """
    return f"apex_{apex_class.lower()}_{apex_method.lower()}"


def _brace_list_names(raw: str) -> list[str]:
    """Parse the inside of an ``{ a, b as c, d }`` import/export brace list.

    Returns the *local* binding names: for ``foo as bar`` the binding used in
    this file is ``bar``, so that is what is matched against call sites. Bare
    ``foo`` returns ``foo``. Whitespace / trailing commas are tolerated.
    """
    names: list[str] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        # ``foo as bar`` -> the local name is ``bar``; otherwise the token itself.
        if " as " in token:
            token = token.split(" as ", 1)[1].strip()
        if token.isidentifier():
            names.append(token)
    return names


def _bundle_stub(path: Path) -> dict:
    """A stub ``lwc_component`` (bundle) node so ``part_of`` edges don't dangle.

    The bundle node carries the component identity; the main JS file upgrades its
    label to the ``export default class`` name. Emitting it from every file in
    the bundle (deduped by id) means a template-only or supplemental-only bundle
    still has a component node (ADR-012).
    """
    bundle_id = _bundle_id(path)
    return {
        "id": bundle_id,
        "label": path.parent.name,
        "file_type": "lwc_component",
        "source_file": str(path.parent),
    }


def _parse_error_node(path: Path, error_type: str, error: Exception, file_id: str) -> dict:
    """Return a single ``concept`` error node for an unreadable LWC file."""
    return {
        "nodes": [
            {
                "id": file_id,
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
    """Parse one LWC ``*.html`` template file in a component bundle.

    A bundle may hold more than one template — exactly one main template (stem
    matches the folder) plus optional supplemental templates the controller swaps
    in (``calendarView.html`` etc.). Each template gets its OWN file node
    (``lwc_<folder>_html_<stem>``) with a ``part_of`` edge to the bundle node
    (``lwc_<folder>``), so supplemental templates are no longer orphaned.

    For each LOCAL custom child component referenced via a ``c-`` tag
    (``<c-badge-section>``), an ``embeds`` edge is emitted FROM this HTML file
    node (the file that actually contains the tag) to the child's BUNDLE node.
    Base Lightning components (``<lightning-...>``), Aura, and standard HTML are
    ignored. The child bundle is emitted as a stub ``lwc_component`` node (ADR-012)
    that merges with the child's real node via the shared id (ADR-002).

    Returns:
        ``{"nodes": [...], "edges": [...]}``; a ``concept`` error node on read
        failure (graceful degradation, ADR-009).
    """
    path = Path(path)
    file_id = _html_file_id(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            html_content = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        return _parse_error_node(path, "html_parse_error", exc, file_id)

    bundle_id = _bundle_id(path)
    nodes: list[dict] = [
        _bundle_stub(path),
        {
            "id": file_id,
            "label": f"{path.stem}.html",
            "file_type": "lwc_template",
            "source_file": str(path),
            "sf_lwc_file_type": "html",
            "sf_main_template": _is_main_file(path),
        },
    ]
    edges: list[dict] = [
        {
            "source": file_id,
            "target": bundle_id,
            "relation": "part_of",
            "confidence": "EXTRACTED",
            "source_file": str(path),
        }
    ]

    # ``<c-...>`` embeds -> embeds edges (this HTML file -> child LWC bundle).
    seen: set[str] = set()
    for m in _EMBED_RE.finditer(html_content):
        child_id = _embedded_lwc_id(m.group(1))
        if child_id == bundle_id or child_id in seen:
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
                "source": file_id,
                "target": child_id,
                "relation": "embeds",
                "confidence": "EXTRACTED",
                "source_file": str(path),
                "sf_embedded_tag": f"c-{m.group(1)}",
            }
        )

    return {"nodes": nodes, "edges": edges}


def extract_lwc_js(path: Path) -> dict:
    """Parse one LWC ``*.js`` file in a component bundle.

    A bundle may hold more than one JS file — exactly one main controller (stem
    matches the folder) plus optional supplemental modules (helpers, constants).
    Each JS file gets its OWN file node (``lwc_<folder>_js_<stem>``) with a
    ``part_of`` edge to the bundle node (``lwc_<folder>``), so supplemental
    modules are no longer orphaned. The main file additionally upgrades the
    bundle node's label to the ``export default class`` name.

    Behavioral signal is sourced from the FILE node that contains it:

      - ``@wire`` decorators -> ``wire_to`` edges to the imported Apex method.
      - imperative ``@salesforce/apex`` imports -> ``lwc_calls`` edges.
      - ``@api`` public properties -> ``sf_api_property_<name>`` file attributes.
      - ``export { foo }`` / ``export function foo`` -> a callable function node
        (``lwc_<folder>_<fn>``) with a ``member_of`` edge to the BUNDLE, so
        service modules expose their functions as link targets.
      - ``import { foo } from 'c/otherModule'`` -> an ``imports`` edge to the
        ``c/`` module bundle; if ``foo`` is then invoked, a ``calls`` edge to the
        function node (``lwc_<module>_<foo>``).

    Returns:
        ``{"nodes": [...], "edges": [...]}``. Referenced Apex methods, imported
        modules, and called functions are emitted as stub nodes so the result has
        no dangling edges; they merge with the real nodes via the shared ID
        (ADR-002, ADR-012). A ``concept`` error node is returned on read failure
        (ADR-009).
    """
    path = Path(path)
    file_id = _js_file_id(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            js_content = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        return _parse_error_node(path, "js_parse_error", exc, file_id)

    bundle_id = _bundle_id(path)
    is_main = _is_main_file(path)
    class_decl = _CLASS_DECL_RE.search(js_content)

    bundle_node = _bundle_stub(path)
    # The main controller names the component: promote its class name onto the
    # bundle node label so the component reads as the class, not the folder.
    component_label = class_decl.group(1) if (is_main and class_decl) else None
    if component_label:
        bundle_node["label"] = component_label

    # This JS file's own node. @api / @wire / imports attach HERE, not the bundle.
    # The main controller carries the authoritative component label so the bundle
    # pass can apply it even if another parser's stub merged in first (labels are
    # first-wins in _merge_into; e.g. an embeds stub uses the kebab tag name).
    file_node: dict = {
        "id": file_id,
        "label": f"{path.stem}.js",
        "file_type": "lwc_controller",
        "source_file": str(path),
        "sf_lwc_file_type": "js",
        "sf_main_controller": is_main,
    }
    if component_label:
        file_node["sf_component_label"] = component_label
    nodes: list[dict] = [bundle_node, file_node]
    edges: list[dict] = [
        {
            "source": file_id,
            "target": bundle_id,
            "relation": "part_of",
            "confidence": "EXTRACTED",
            "source_file": str(path),
        }
    ]

    # Behavioral edges source from the file node (the file that declares them).
    lwc_id = file_id

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

    # 3. @api public properties -> file-node attributes --------------------
    for prop_match in _API_PROP_RE.finditer(js_content):
        file_node[f"sf_api_property_{prop_match.group(1)}"] = True

    # 4. Exported functions -> callable function nodes ----------------------
    # A JS "service" module exports named functions other components import and
    # call. Each export becomes a function node ``member_of`` the BUNDLE (the
    # component the function belongs to), so a consumer's ``calls`` edge — which
    # targets ``lwc_<module>_<fn>`` — resolves here. Block-form and inline exports
    # both count.
    exported: set[str] = set()
    for block in _EXPORT_BLOCK_RE.finditer(js_content):
        exported.update(_brace_list_names(block.group(1)))
    for inline in _EXPORT_INLINE_RE.finditer(js_content):
        exported.add(inline.group(1))
    for fn_name in sorted(exported):
        fn_id = _lwc_function_nid(bundle_id, fn_name)
        nodes.append(
            {
                "id": fn_id,
                "label": f"{fn_name}()",
                "file_type": "code",
                "source_file": str(path),
                "sf_lwc_function": fn_name,
            }
        )
        edges.append(
            {
                "source": fn_id,
                "target": bundle_id,
                "relation": "member_of",
                "confidence": "EXTRACTED",
                "source_file": str(path),
            }
        )

    # 5. ``c/`` module imports -> imports + calls edges (LWC -> LWC) ---------
    # ``import { fn } from 'c/otherModule'`` is a dependency on a local LWC module.
    # Emit one ``imports`` edge per module, and a ``calls`` edge for each imported
    # name that is actually invoked in this file's body. Targets are emitted as
    # stub nodes (ADR-012) that merge with the exporting module's real nodes.
    def _ensure_stub(node_id: str, label: str, **attrs) -> None:
        if not any(n["id"] == node_id for n in nodes):
            nodes.append(
                {
                    "id": node_id,
                    "label": label,
                    "file_type": attrs.pop("file_type", "lwc_component"),
                    "source_file": str(path),
                    **attrs,
                }
            )

    imported_modules: set[str] = set()
    for imp in _MODULE_IMPORT_RE.finditer(js_content):
        module_name = imp.group(2)
        module_id = _module_lwc_id(module_name)
        if module_id == bundle_id:
            continue  # defensive: importing from one's own bundle
        names = _brace_list_names(imp.group(1))
        if module_id not in imported_modules:
            imported_modules.add(module_id)
            _ensure_stub(module_id, module_name)
            edges.append(
                {
                    "source": lwc_id,
                    "target": module_id,
                    "relation": "imports",
                    "confidence": "EXTRACTED",
                    "source_file": str(path),
                }
            )
        for name in names:
            # Only emit a `calls` edge when the imported name is actually invoked
            # as ``name(...)`` in the body — an unused import is a dep, not a call.
            if not re.search(rf"\b{re.escape(name)}\s*\(", js_content):
                continue
            fn_id = _lwc_function_nid(module_id, name)
            _ensure_stub(
                fn_id, f"{name}()", file_type="code", sf_lwc_function=name
            )
            edges.append(
                {
                    "source": lwc_id,
                    "target": fn_id,
                    "relation": "calls",
                    "context": "call",
                    "confidence": "INFERRED",
                    "confidence_value": 0.85,
                    "source_file": str(path),
                    "sf_lwc_function": name,
                }
            )

    return {"nodes": nodes, "edges": edges}
