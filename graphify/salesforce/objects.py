"""
graphify-sf: Custom Object / Custom Field metadata parser.

Parses Salesforce ``*.object-meta.xml`` (CustomObject) and
``*.field-meta.xml`` (CustomField) metadata into graph nodes / edges:

    - ``sobject`` node per CustomObject (ID from ``sobject_nid()`` — ADR-002).
    - ``field`` node per field, linked to its object with a ``field_of`` edge.
    - ``references`` edge for Lookup / Master-Detail relationships, resolved to
      the referenced SObject via ``sobject_nid()`` (cross-file resolution).

CRITICAL (ADR-002): SObject node IDs are built ONLY via
``constants.sobject_nid()`` so that the Apex, Flow and Object parsers all
converge on the same node in ``build_graph()``.

Error handling (ARCHITECTURE.md "파서 레벨 에러"): XML parse failures degrade
gracefully — a single ``concept`` error node is returned and the file is
skipped, never raising so that the rest of the repo keeps analyzing (ADR-009
lenient mode).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from graphify.salesforce.constants import sobject_nid

#: Salesforce metadata XML namespace.
_NS = {"md": "http://soap.sforce.com/2006/04/metadata"}

#: Field types that reference another SObject.
_RELATIONSHIP_TYPES = {"Lookup", "MasterDetail"}

#: Identifier (and dotted reference) token in a Salesforce formula, e.g.
#: ``SBQQ__Discount__c`` or ``Account.Owner.Name``.
_FORMULA_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")

#: Formula functions / literals that are NOT field references. A token directly
#: followed by ``(`` is already treated as a function call; this set additionally
#: filters bare literals and a few common operators that take no parens.
_FORMULA_FUNCTIONS = {
    "AND", "OR", "NOT", "IF", "CASE", "ISCHANGED", "ISNEW", "ISBLANK",
    "ISNULL", "ISNUMBER", "ISPICKVAL", "PRIORVALUE", "TEXT", "VALUE", "LEN",
    "BEGINS", "CONTAINS", "INCLUDES", "REGEX", "TODAY", "NOW", "DATE",
    "DATEVALUE", "ABS", "ROUND", "MAX", "MIN", "MOD", "FLOOR", "CEILING",
    "BLANKVALUE", "NULLVALUE", "VLOOKUP", "TRUE", "FALSE", "NULL",
}


def _extract_formula_fields(formula: str | None) -> list[str]:
    """Extract field references from a Validation Rule formula.

    Returns the distinct field tokens (order-preserving) a formula reads, with
    function calls and literals removed. Dotted cross-object references
    (``Account.Name``) are kept whole; the consumer normalizes to the bare field
    token. Best-effort static parse (ADR-030): conditional / dynamic logic is not
    resolved, so the result is a superset of fields the rule may actually use.
    """
    if not formula:
        return []
    fields: list[str] = []
    seen: set[str] = set()
    for match in _FORMULA_TOKEN_RE.finditer(formula):
        token = match.group(0)
        # A token immediately followed by "(" is a function call, not a field.
        after = formula[match.end():].lstrip()
        if after.startswith("("):
            continue
        if token.upper() in _FORMULA_FUNCTIONS:
            continue
        if token not in seen:
            seen.add(token)
            fields.append(token)
    return fields


def _validation_rule_nid(object_api: str, rule_name: str) -> str:
    """Build a stable node ID for a Validation Rule (``validation_<obj>_<rule>``)."""
    obj = object_api.lower().replace("__c", "").replace("__", "_")
    rule = rule_name.lower()
    return f"validation_{obj}_{rule}"


def _field_nid(object_api: str, field_api: str) -> str:
    """Build a stable node ID for a field scoped to its SObject.

    Uses the full API names (preserving ``__c``) so that fields with the same
    name on different objects never collide, and the ID is reversible.

    Example: ``("Account", "BillingCity")`` -> ``"field_account_billingcity"``
             ``("MyObj__c", "Background_Image_URL__c")``
               -> ``"field_myobj__c_background_image_url__c"``
    """
    return f"field_{object_api.lower()}_{field_api.lower()}"


def _parse_error_node(path: Path, error: Exception) -> dict:
    """Return a single ``concept`` error node for an unparseable XML file."""
    return {
        "nodes": [
            {
                "id": f"sobject_{path.stem.lower()}",
                "label": f"[Parse Error] {path.name}",
                "file_type": "concept",
                "source_file": str(path),
                "sf_error_type": "xml_parse_error",
                "sf_error_message": str(error),
            }
        ],
        "edges": [],
    }


def extract_custom_object(path: Path) -> dict:
    """Parse a ``*.object-meta.xml`` (CustomObject) file.

    Returns:
        ``{"nodes": [...], "edges": [...]}``. On XML parse failure a single
        ``concept`` error node is returned with no edges (graceful degradation).
    """
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _parse_error_node(path, exc)

    # 1. SObject node ------------------------------------------------------
    # API name from <fullName> (authoritative); fall back to the file stem
    # (e.g. "Document_Rule__c.object-meta.xml" -> "Document_Rule__c").
    # The human-readable <label> is kept for display only — the node ID must
    # be derived from the API name so it merges correctly with stub nodes
    # created by Apex SOQL, Flow, and lookup-field parsers (ADR-002).
    api_name = root.findtext("md:fullName", namespaces=_NS) or path.name.split(".")[0]
    label = root.findtext("md:label", namespaces=_NS) or api_name
    plural_label = root.findtext("md:pluralLabel", namespaces=_NS) or label

    sobject_id = sobject_nid(api_name)  # CRITICAL: ADR-002 single source of truth
    nodes: list[dict] = [
        {
            "id": sobject_id,
            "label": label,
            "file_type": "sobject",
            "source_file": str(path),
            "sf_plural_label": plural_label,
            "sf_object_type": "custom" if "__c" in api_name else "standard",
        }
    ]
    edges: list[dict] = []

    # 1b. Custom Setting enrichment — a CustomObject with <customSettingsType>
    # is a List/Hierarchy Custom Setting, not a real table. Tag the SObject node
    # so impact / governor analysis treats it as config storage (no SOQL limits).
    setting_type = root.findtext("md:customSettingsType", namespaces=_NS)
    if setting_type:
        nodes[0]["sf_is_custom_setting"] = True
        nodes[0]["sf_setting_type"] = setting_type

    # 2. Field nodes + field_of edges -------------------------------------
    for field_elem in root.findall("md:fields", namespaces=_NS):
        field_name = field_elem.findtext("md:fullName", namespaces=_NS)
        if not field_name:
            continue
        field_label = field_elem.findtext("md:label", namespaces=_NS) or field_name
        field_type = field_elem.findtext("md:type", namespaces=_NS)

        field_id = _field_nid(api_name, field_name)
        nodes.append(
            {
                "id": field_id,
                "label": field_label,
                "file_type": "field",
                "source_file": str(path),
                "sf_field_type": field_type,
                "sf_api_name": field_name,
            }
        )

        edges.append(
            {
                "source": field_id,
                "target": sobject_id,
                "relation": "field_of",
                "confidence": "EXTRACTED",
                "source_file": str(path),
            }
        )

        # 3. Lookup / Master-Detail relationship --------------------------
        if field_type in _RELATIONSHIP_TYPES:
            referenced = field_elem.findtext("md:referenceTo", namespaces=_NS)
            if referenced:
                referenced_id = sobject_nid(referenced)
                # Add a stub node for the referenced SObject so the edge is not
                # dangling within this result. build_graph() merges this with the
                # real node (from the referenced object's own *.object-meta.xml)
                # via the shared sobject_nid (ADR-002, ADR-012).
                _ensure_sobject_node(nodes, referenced_id, referenced, path)
                edges.append(
                    {
                        "source": sobject_id,
                        "target": referenced_id,
                        "relation": "references",
                        "sf_relationship_type": field_type,
                        "sf_field_api_name": field_name,
                        "confidence": "EXTRACTED",
                        "source_file": str(path),
                    }
                )

    # 4. Embedded Validation Rules (<validationRules> children) ------------
    for vr_elem in root.findall("md:validationRules", namespaces=_NS):
        rule_name = vr_elem.findtext("md:fullName", namespaces=_NS)
        if not rule_name:
            continue
        formula = vr_elem.findtext("md:errorConditionFormula", namespaces=_NS)
        active = vr_elem.findtext("md:active", namespaces=_NS) != "false"
        _add_validation_rule(
            nodes, edges, label, sobject_id, rule_name, formula, active, path
        )

    return {"nodes": nodes, "edges": edges}


def _add_validation_rule(
    nodes: list[dict],
    edges: list[dict],
    object_api: str,
    sobject_id: str,
    rule_name: str,
    formula: str | None,
    active: bool,
    path: Path,
) -> None:
    """Append a ``validation_rule`` node + a ``validates`` edge to its SObject.

    The node carries ``sf_referenced_fields`` (parsed from the error-condition
    formula) and ``sf_object`` so the CPQ ↔ Validation overlap pass
    (validation_cpq, ADR-030) and the Order-of-Execution pass (which consumes
    ``validates`` edges) both have what they need. ``_ensure_sobject_node`` keeps
    the ``validates`` edge from dangling.
    """
    rule_id = _validation_rule_nid(object_api, rule_name)
    nodes.append(
        {
            "id": rule_id,
            "label": rule_name,
            "file_type": "validation_rule",
            "source_file": str(path),
            "sf_object": object_api,
            "sf_active": active,
            "sf_referenced_fields": _extract_formula_fields(formula),
        }
    )
    _ensure_sobject_node(nodes, sobject_id, object_api, path)
    edges.append(
        {
            "source": rule_id,
            "target": sobject_id,
            "relation": "validates",
            "confidence": "EXTRACTED",
            "source_file": str(path),
        }
    )


def _ensure_sobject_node(
    nodes: list[dict], sobject_id: str, api_name: str, path: Path
) -> None:
    """Append a stub ``sobject`` node for *sobject_id* if not already present."""
    if any(n["id"] == sobject_id for n in nodes):
        return
    nodes.append(
        {
            "id": sobject_id,
            "label": api_name,
            "file_type": "sobject",
            "source_file": str(path),
            "sf_object_type": "custom" if "__c" in api_name else "standard",
        }
    )


def _parent_object_from_field_path(path: Path) -> str | None:
    """Infer the owning object's API name from a field-meta.xml path.

    Salesforce layout: ``objects/<Object>/fields/<Field>.field-meta.xml``.
    Returns the ``<Object>`` directory name, or ``None`` if the layout differs.
    """
    parts = path.parts
    if "fields" in parts:
        idx = parts.index("fields")
        if idx >= 1:
            return parts[idx - 1]
    return None


def extract_custom_field(path: Path) -> dict:
    """Parse a standalone ``*.field-meta.xml`` (CustomField) file.

    The owning object is inferred from the directory layout
    (``objects/<Object>/fields/<Field>.field-meta.xml``). Returns the field
    node, a ``field_of`` edge to the parent object (when resolvable), and a
    ``references`` edge for Lookup / Master-Detail fields.
    """
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _parse_error_node(path, exc)

    # The field API name lives in <fullName>, falling back to the file stem
    # (e.g. "BillingCity__c.field-meta.xml" -> "BillingCity__c").
    field_name = root.findtext("md:fullName", namespaces=_NS) or path.name.split(".")[0]
    field_label = root.findtext("md:label", namespaces=_NS) or field_name
    field_type = root.findtext("md:type", namespaces=_NS)

    parent_api = _parent_object_from_field_path(path)
    field_id = _field_nid(parent_api or path.parent.name, field_name)
    nodes: list[dict] = [
        {
            "id": field_id,
            "label": field_label,
            "file_type": "field",
            "source_file": str(path),
            "sf_field_type": field_type,
            "sf_api_name": field_name,
        }
    ]
    edges: list[dict] = []
    if parent_api:
        parent_id = sobject_nid(parent_api)
        # Ensure the field_of edge is not dangling: include the parent SObject
        # node so this dict is self-consistent (build merges it with any other
        # parser's node for the same object via the shared sobject_nid).
        nodes.append(
            {
                "id": parent_id,
                "label": parent_api,
                "file_type": "sobject",
                "source_file": str(path),
                "sf_object_type": "custom" if "__c" in parent_api else "standard",
            }
        )
        edges.append(
            {
                "source": field_id,
                "target": parent_id,
                "relation": "field_of",
                "confidence": "EXTRACTED",
                "source_file": str(path),
            }
        )

        if field_type in _RELATIONSHIP_TYPES:
            referenced = root.findtext("md:referenceTo", namespaces=_NS)
            if referenced:
                referenced_id = sobject_nid(referenced)
                _ensure_sobject_node(nodes, referenced_id, referenced, path)
                edges.append(
                    {
                        "source": parent_id,
                        "target": referenced_id,
                        "relation": "references",
                        "sf_relationship_type": field_type,
                        "sf_field_api_name": field_name,
                        "confidence": "EXTRACTED",
                        "source_file": str(path),
                    }
                )

    return {"nodes": nodes, "edges": edges}


def _parent_object_from_validation_path(path: Path) -> str | None:
    """Infer the owning object's API name from a validationRule-meta.xml path.

    Salesforce layout:
    ``objects/<Object>/validationRules/<Rule>.validationRule-meta.xml``.
    Returns the ``<Object>`` directory name, or ``None`` if the layout differs.
    """
    parts = path.parts
    if "validationRules" in parts:
        idx = parts.index("validationRules")
        if idx >= 1:
            return parts[idx - 1]
    return None


def extract_validation_rule(path: Path) -> dict:
    """Parse a standalone ``*.validationRule-meta.xml`` (ValidationRule) file.

    The owning object is inferred from the directory layout
    (``objects/<Object>/validationRules/<Rule>.validationRule-meta.xml``). The
    rule name comes from ``<fullName>`` (falling back to the file stem). Produces
    a ``validation_rule`` node (with ``sf_referenced_fields`` from the formula)
    and a ``validates`` edge to its SObject. On XML parse failure a single
    ``concept`` error node is returned (graceful degradation, ADR-009).
    """
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _parse_error_node(path, exc)

    rule_name = (
        root.findtext("md:fullName", namespaces=_NS)
        or path.name.split(".")[0]
    )
    formula = root.findtext("md:errorConditionFormula", namespaces=_NS)
    active = root.findtext("md:active", namespaces=_NS) != "false"

    object_api = _parent_object_from_validation_path(path)
    if object_api is None:
        # Layout doesn't reveal the object: emit the rule node alone (no edge)
        # so the formula's fields are still recorded, never a dangling edge.
        rule_id = _validation_rule_nid(path.stem, rule_name)
        return {
            "nodes": [
                {
                    "id": rule_id,
                    "label": rule_name,
                    "file_type": "validation_rule",
                    "source_file": str(path),
                    "sf_active": active,
                    "sf_referenced_fields": _extract_formula_fields(formula),
                }
            ],
            "edges": [],
        }

    nodes: list[dict] = []
    edges: list[dict] = []
    _add_validation_rule(
        nodes, edges, object_api, sobject_nid(object_api),
        rule_name, formula, active, path,
    )
    return {"nodes": nodes, "edges": edges}
