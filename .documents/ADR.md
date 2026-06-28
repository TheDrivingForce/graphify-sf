# Architecture Decision Records

This is the **local** ADR log for this fork of graphify-sf.

The upstream project recorded its decisions in `docs/ADR.md`, which did not make
it into our clone — but those decisions are still cited throughout the code as
`ADR-002` … `ADR-030` (e.g. ADR-002 cross-file node-id convergence, ADR-008 the
per-file pipeline, ADR-009 lenient parsing, ADR-012 no-dangling-edge stubs). To
avoid colliding with those upstream numbers while their text remains unavailable,
**our own decisions start at ADR-100**. When a new decision supersedes or revises
an upstream one, reference the upstream number in the entry's *Context*.

## Format

Each entry: a short title, **Status**, **Context** (the problem / forces),
**Decision** (what we chose), and **Consequences** (what follows, good and bad).
Newest entries go at the bottom. Once an entry is `Accepted`, treat it as
immutable history — supersede it with a new ADR rather than editing it.

---

## ADR-100 — LWC modelled as a bundle of file nodes

**Status:** Accepted — 2026-06-28

### Context

A Lightning Web Component is a *bundle* of files in a folder named after the
component: exactly one main JavaScript controller and one main HTML template
(both matching the folder name), plus optional **supplemental** files — extra
`*.js` modules (helper functions, business constants) and extra `*.html`
templates the controller swaps in at runtime (`calendarView.html`,
`listView.html`, …). CSS files round out the bundle.

The original model (upstream ADR-008) gave each file a node keyed on its **file
stem** (`lwc_<stem>`) and, in the merge pass, folded the `*.html` whose stem
matched its `*.js` sibling into a single `lwc_component` node per stem. This had
two failures on real bundles:

1. **Supplemental templates were orphaned.** `calendarView.html` became a lone
   `lwc_calendarview` node with no sibling JS to merge into and nothing linking
   to it — it looked like a separate, unused component. In the eventspark repo
   five bundles have multiple templates (`upcomingEventsViews` has six), so this
   was not an edge case.
2. **Stem-based identity conflated files with components.** A supplemental file
   whose stem happened to match another component's folder could be mistaken for
   that component.

Salesforce's own documentation is explicit that a bundle may contain more than
one JS file (one main + supplemental helpers), which the stem model could not
represent.

### Decision

Reverse the "one node per component, fold HTML into JS" decision (upstream
ADR-008) in favour of an explicit **bundle model**:

- **Component identity is the folder, not the file stem.** The bundle node is
  `lwc_<folder>` (e.g. `lwc_upcomingeventsviews`). Keeping this id stable
  preserves the external link contract other parsers rely on — `embeds`,
  `wire_to`, `imports`, and `calls` still resolve to a component by this id
  (upstream ADR-002 cross-file resolution is unaffected).
- **Each file is its own node, `part_of` the bundle.** JS files become
  `lwc_controller` nodes (`lwc_<folder>_js_<stem>`); HTML templates become
  `lwc_template` nodes (`lwc_<folder>_html_<stem>`). Every file emits a
  `part_of` edge to the bundle. CSS is ignored, as before. Each file parser also
  emits a **stub** bundle node so `part_of` never dangles (upstream ADR-012); the
  stubs merge by id.
- **Main vs supplemental is a flag, not a separate identity.** The file whose
  stem matches the folder is tagged `sf_main_controller` / `sf_main_template`.
  The main controller's `export default class` name becomes the bundle label
  (carried on the file node as `sf_component_label` so it wins even when another
  parser's stub — e.g. a kebab-cased `embeds` stub — merged in first).
- **Behavioral edges source from the file node that contains them**, not the
  bundle: `@wire` → `wire_to`, imperative `@salesforce/apex` imports →
  `lwc_calls`, `@api` properties (as file-node attributes), `c/` module
  `imports`/`calls`, and `embeds` from `c-` tags. This attributes each
  dependency to the specific file that owns it.
- The bundle node carries summary flags derived in the bundle pass:
  `sf_has_template`, `sf_js_file_count`, `sf_html_file_count`.

New node types `lwc_controller` / `lwc_template` and the `part_of` relation are
registered in the SF schema reference (`validate_sf.py`), the Neo4j export
(`neo4j_sf.py`), and the viz colour map (`viz.py`).

### Consequences

- **Supplemental files are first-class.** Extra templates and helper JS modules
  are no longer orphaned; they appear as bundle members and participate in impact
  analysis. Verified across eventspark: 115 bundles, 0 orphan file nodes, 0
  dangling edges, and all five multi-template bundles aggregate every template.
- **Finer attribution.** A consumer can see which file in a bundle owns a given
  `@wire` / import / embed, rather than attributing everything to the component.
- **Breaking schema change for downstream consumers.** Anything that counted
  `lwc_component` nodes per file, or expected behavioral edges sourced from the
  component node, must adapt: there is now one bundle node plus separate
  `lwc_controller` / `lwc_template` file nodes, with edges sourced from files.
  The external target contract (`lwc_<folder>` as a link target) is unchanged.
- **Merge pass simplified.** `_merge_lwc_components` no longer folds/deletes
  nodes; it only derives bundle summary flags and applies the authoritative
  component label from the main controller.
