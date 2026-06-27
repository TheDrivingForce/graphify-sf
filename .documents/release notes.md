# Release Notes

## New Features

### LWC-to-LWC composition edges (`lwc.py`)

The graph now models component composition: when an LWC template embeds another **local custom** LWC, an `embeds` edge is created from the parent component to the embedded child. This surfaces structural dependencies that were previously invisible — e.g. answering "if I change `badgeSection`, which components embed it?".

- The HTML parser (`extract_lwc_html`) scans templates for `c-` namespaced tags (e.g. `<c-badge-section>`) and emits one `embeds` edge per unique embedded child.
- Only local custom components (`c-` prefix) are matched. Base Lightning components (`<lightning-button>`), Aura, and standard HTML tags are ignored.
- Both paired (`<c-foo>...</c-foo>`) and self-closing (`<c-foo/>`) tags are detected. Duplicate child tags within one template produce a single edge.
- Child components are emitted as stub nodes (no dangling edges) and merge with the child's real node via the shared ID — the same cross-file resolution used for `wire_to`/Apex (ADR-002).
- Edges are sourced from the **base** component ID (`lwc_<stem>`), not the HTML sidecar node, so they survive the LWC merge pass.
- New `embeds` relation maps to the Neo4j `EMBEDS` relationship type.

**ID resolution:** a `<c-badge-section>` tag resolves to `lwc_badgesection` — kebab-case tag names map deterministically to the lowercased camelCase component folder name, so no separate resolution pass is required.

## Bug Fixes

### CPQ data extraction crash on field nodes (`cpq_data.py`)

`_ensure_field_node` called `_field_nid` with a single argument after `objects._field_nid` was changed to require both an object and field API name (commit `d78d732`). This raised a `TypeError` on any CPQ data extraction (`extract_cpq_data`).

- CPQ field node IDs are now object-scoped: a condition/action field is attributed to the object its rule applies to (`SBQQ__LookupObject__c` / `SBQQ__EvaluationEvent__c`, defaulting to `SBQQ__Quote__c`).
- Field nodes carry `sf_object`, and their IDs (`field_{object}_{field}`) now merge with the same field parsed from object metadata (ADR-002).

### MDT mapping field nodes failed to merge (`mdt_mapping.py`)

`mdt_mapping.py` carried its own stale copy of `_field_nid` producing the old unscoped ID scheme (`field_{field}`). After the object-scoping change this silently diverged: `maps_to` field endpoints no longer merged with field nodes parsed from object metadata.

- The local `_field_nid` now delegates to `objects._field_nid`, scoping each field to the object named in the mapping record (Main/Source and Second/Target objects).
- Falls back to `"unknown"` as the object scope when a mapping record omits the object, keeping the ID stable.
