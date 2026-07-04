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

## ADR-101 — Visualforce pages / components as first-class nodes

**Status:** Accepted — 2026-07-02

### Context

Visualforce (`*.page`, `*.component`) is the classic server-rendered UI layer
that predates LWC/Aura and is still widespread in older orgs (eventspark has
~70 pages/components). The tool modelled Apex, LWC, and Aura but skipped VF, so
a page's dependency on its Apex controller and on the components it embeds was
invisible to impact analysis — changing a controller gave no signal that a VF
page consuming it might break.

VF markup is HTML-ish but not reliably well-formed XML in practice (unescaped
`&`, HTML5 void tags, `{! ... }` merge fields), and the only signal we need
lives on the root tag's attributes plus `<c:...>` child tags. This matches the
regex-only approach already used for LWC (upstream) rather than an XML parser.

### Decision

Add `visualforce.py` with `extract_visualforce`, one parser for both suffixes,
wired into `register()` and `_parser_for`. Node/edge model:

- One node per file: `vf_page` (`vf_page_<stem>`) or `vf_component`
  (`vf_component_<stem>`). The kind is in the id so a page and component that
  share a base name stay distinct; `<c:...>` embeds only ever resolve to
  `vf_component_<name>` (pages are not embeddable).
- `calls` edges to Apex controllers: `controller=` and each comma-separated
  class in `extensions="A,B"`, targeting `apex_<class>` (the Apex parser's id).
- `calls` edge to `standardController="<Object>"` targeting `sobject_<name>`
  via `sobject_nid` — a standard/custom object is data, not Apex, so it links to
  the object node (decision: link, don't just annotate, so the dependency is
  traversable).
- `embeds` edges to each `<c:...>` local custom component
  (`vf_component_<name>`). Base `<apex:...>` tags and standard HTML are ignored.
- `embeds` edges to each LWC surfaced via **Lightning Out**
  (`$Lightning.createComponent("<ns>:<name>", ...)`), targeting the LWC bundle
  node `lwc_<name>` (the LWC parser's id, ADR-002). The namespace prefix is
  stripped — component identity is the camelCase name, which is the LWC folder.
  The `$Lightning.use("<ns>:LightningOutApp", ...)` call names the container Aura
  *app*, not the embedded component, so only `createComponent` is matched. These
  edges are `INFERRED` (0.9), not `EXTRACTED`, since the target is resolved from a
  string literal in JS rather than a markup tag; a name that resolves to an Aura
  component instead of an LWC simply leaves a non-merging stub.

All link targets are emitted as stub nodes (ADR-012) that merge with the real
nodes via the shared id (ADR-002), exactly as LWC does. Lenient parsing
(ADR-009): a read/decode failure degrades to one `concept` error node.

### Consequences

- **VF is now visible in impact analysis.** A controller change surfaces the VF
  pages that call it; a component change surfaces the pages/components embedding
  it. `standardController` pages link to their object.
- **Deliberate scope limits.** `<apex:attribute type="SomeController">` (a type
  reference inside a component's attribute declaration) is NOT treated as a
  `calls` link — attribute types are frequently SObjects/primitives, so it would
  be noisy. `<c:...>` embeds inside `.email` templates are out of scope (only
  `.page`/`.component` files are parsed). Both can be revisited if needed.
- **New node types** `vf_page` / `vf_component` added to the Neo4j label map
  (`VisualforcePage` / `VisualforceComponent`) and the HTML viz colour map.

## ADR-102 — Aura components / applications as first-class nodes

**Status:** Accepted — 2026-07-02

### Context

Aura (`*.cmp`, `*.app`) is the Lightning-era component framework that sits
between Visualforce and LWC. It was the last major UI layer the tool did not
model, so an Aura component's Apex controller dependency and the children/LWCs it
renders were invisible to impact analysis. Aura is also the bridge for Lightning
Out: a `.app` extending `ltng:outApp` is the container that surfaces LWCs into
Visualforce (see ADR-101), and it declares those LWCs via `<aura:dependency>`.

An Aura bundle is a folder of files (markup `.cmp`/`.app`, plus
`*Controller.js`/`*Helper.js`/`*Renderer.js`, `.css`, `.design`, `.svg`,
`.auradoc`, `.evt`). Unlike LWC — where the Apex dependency lives in the JS
(`@salesforce/apex` imports) — an Aura bundle's class-level dependency is the
`controller=` attribute on the markup root, and the components it renders are
`<c:...>` tags in the markup. So the markup file alone carries every signal we
model; the JS files call Apex methods via `c.method` but resolving those to
specific method nodes is a deeper, cross-file-within-bundle concern deferred for
now.

The hard problem: the `c:` namespace is shared by Aura components AND LWCs, so
`<c:foo>` in a `.cmp` could be either, and there is no syntactic marker. An Aura
component can embed an LWC but not the reverse.

### Decision

Add `aura.py` with `extract_aura`, one parser for both `.cmp` and `.app`, wired
into `register()` and `_parser_for` (guarded to an `aura/` directory like LWC).
Only the markup file is parsed; one node per bundle. Node/edge model:

- One `aura_component` node per bundle (`aura_<folder>`), tagged
  `sf_aura_type` = `component` / `application`.
- `calls` edge to the Apex `controller=` class -> `apex_<class>` (ADR-002 id).
- `embeds` edge per `<c:...>` child, disambiguated by the **SF naming rule**: an
  LWC's name MUST start lowercase, an Aura component conventionally starts
  uppercase. So lowercase-initial -> `lwc_<name>`, uppercase-initial ->
  `aura_<name>`. Verified accurate across every eventspark Aura embed (e.g.
  `SetupMain` renders both `<c:SetupDashboard>` (Aura) and `<c:setupStatus>`
  (LWC)). The edge records `sf_embed_kind`.
- `embeds` edge per `<aura:dependency resource="X"/>` -> `lwc_<X>` (INFERRED
  0.9), capturing the LWCs a Lightning Out container `.app` surfaces.

All targets are stub nodes (ADR-012) that merge with real nodes via the shared id
(ADR-002). Lenient parsing (ADR-009): a read/decode failure -> one `concept`
error node.

### Consequences

- **Aura is now visible in impact analysis**, closing the last UI-layer gap
  (VF/LWC/Aura all modelled). The Lightning Out chain is now traversable
  end-to-end: VF page `$Lightning.createComponent` -> LWC, and the
  `LightningOutApp.app` -> its declared LWC dependencies.
- **Case heuristic is a convention, not a guarantee.** If a project ever names an
  Aura component with a lowercase initial (against convention), its embed would
  resolve to a non-existent `lwc_<name>` stub. This is the same graceful outcome
  as any unresolved embed (a non-merging stub, no dangling edge), and the rule
  held for 100% of the real corpus, so it was preferred over emitting both ids.
- **Deliberate scope limit.** JS `c.<method>` Apex calls in Aura controllers are
  NOT resolved to method nodes — only the class-level `controller=` link is
  captured. Method-level resolution can be added later.
- **`aura_component`** was already in the Neo4j label map (`AuraComponent`);
  added a viz colour.

---

## ADR-103 — Apex triggers link to the SObject they fire on

**Status:** Accepted — 2026-07-04

### Context

The `triggers_on` relation was already wired end-to-end — the Neo4j label map
(`TRIGGERS_ON`), the `validate_sf` known-relations allowlist, and the Order of
Execution pass (`_OOE_TRIGGERING_RELATIONS = {"triggers_on", "validates"}`) all
expected it — but no parser emitted it. An Apex trigger declares its subject
SObject in the header (`trigger AccountTrigger on Account (...)`); that signal
was parsed for the trigger node but discarded, so triggers had no edge to the
object they run against and OoE chains were only ever seeded by Validation Rules.

### Decision

Emit a `triggers_on` edge (`EXTRACTED`) from the trigger's `apex_<stem>` node to
`sobject_nid(<SObject>)` — the identifier after the `on` keyword. Added to both
Apex parsers so they keep identical node/edge shapes (ADR-019): `apex_ts`
(tree-sitter, the primary path — reads the `identifier` child following the `on`
child of `trigger_declaration`) and `_extract_apex_regex` (fallback — a
`trigger \w+ on (\w+)` regex). The target reuses the shared `sobject_nid` so it
merges with the real object node (ADR-002) via a stub (ADR-012), and the
`_is_sobject_name` guard rejects `__r`/non-SObject names as with SOQL/DML targets.

### Consequences

- **Triggers are now first-class in impact analysis and OoE.** Every triggered
  SObject seeds its 18-step Order of Execution chain, not just those with
  Validation Rules; the trigger→object relationship is traversable.
- **No new plumbing was required** — the relation was already registered in every
  downstream consumer, so the change is purely additive at the parser layer.

---

## ADR-104 — Orphan-gated `references` edges for type-only Apex classes

**Status:** Accepted — 2026-07-04

### Context

Pure DTO / wrapper classes (e.g. `FlockEventResponseDto`) that declare no
methods and are never constructed with `new` end up as orphan nodes, even
though other classes clearly depend on them by declaring variables, parameters,
return types, or fields of that type. The existing anti-orphan mechanism —
`new X()` collected as `sf_unresolved_refs` and resolved into `instantiates`
edges (upstream Phase B) — misses classes handed around purely by type.
Emitting an edge for *every* type declaration is not an option: a real org has
thousands of such declarations, and unconditional edges would drown the call
graph in low-signal links.

### Decision

Collect declared type names during the tree-sitter walk (locals, parameters,
method return types, and a new `field_declaration` visitor — the field case is
what links DTO-in-DTO nesting) as `sf_unresolved_type_refs`, deduped per class.
A new post-parse pass, `resolve_apex_type_refs`, runs AFTER
`resolve_apex_calls` and `resolve_apex_refs` and emits a `references` edge
(`context: "type_ref"`, `EXTRACTED`) only toward classes that are still
**orphaned** — no incoming *usage* edge from outside the class itself, on the
class node or any of its method nodes. Non-usage relations are excluded from
the gate: membership (`method_of`/`field_of`) is structure, and profile /
permission-set grants (`grants_access_to`) are administrative — a profile
grants access to nearly every class in an org (1,142 such edges in eventspark),
which says nothing about actual usage and would otherwise defeat the gate for
almost every DTO. When a class is orphaned, ALL classes referencing it are linked
(one edge per source→target pair), so the graph shows the DTO's real consumers.
Name resolution mirrors `resolve_apex_refs`: a type name links only when
exactly one Apex class declares that label; ambiguous names get no edge.

The relation reuses `references`, matching the base extractor's convention for
type usage in other languages (`context` = `parameter_type`/`return_type`/...)
and `objects.py`'s field→sobject lookups; it is now declared in `SF_RELATIONS`
(resolving its former schema drift) and mapped to `REFERENCES` in Neo4j.
Default ON; `--no-type-refs` suppresses the edges (the deferred metadata is
still consumed so it never leaks into the exported graph).

### Consequences

- **Type-only DTOs are reachable** in impact analysis and community detection
  instead of floating as orphans; edge volume stays tiny because the fallback
  fires only for otherwise-orphaned classes.
- **Well-connected classes gain nothing** — a class already called or
  instantiated anywhere never receives `references` edges, so routine
  declarations stay out of the graph by design.
- Internal-only activity does not de-orphan: a class whose own methods call
  each other but that nobody references still counts as an orphan and gets the
  fallback — deliberate, since the gate asks "does anyone OUTSIDE use it?".
- The gate is computed once, before the pass emits; `references` edges added by
  the pass do not un-orphan a target for later sources (deterministic,
  order-independent output).

---

## ADR-105 — Apex → Flow launch as a `calls` edge

**Status:** Accepted — 2026-07-04

### Context

Apex launches a flow programmatically with
`Flow.Interview.<FlowApiName> f = new Flow.Interview.<FlowApiName>(inputs); f.start();`
(e.g. `EmailSendUsingFlow.cls` in eventspark launching `Send_Email_Using_Flow`).
This is a direct Apex → Flow dependency, but nothing captured it: the only
flow-crossing relation was `flow_invokes` (Flow ApexAction → Apex, the *reverse*
direction). The construction's type node is a dotted `scoped_type_identifier`
(`Flow.Interview.Send_Email_Using_Flow`), which `_constructed_type_name`
deliberately returns `None` for (scoped types are not Apex class links), so the
`new`-expression walk skipped it and no edge was produced.

### Decision

Detect the `new Flow.Interview.<Name>(...)` shape in the existing
`object_creation_expression` walk (`apex_ts` section 4.6) via a new
`_flow_interview_name` helper that matches exactly the three-segment
`Flow.Interview.<Name>` scoped type and returns the last segment (the flow API
name). Emit a **`calls`** edge (`context: "flow_interview"`, `EXTRACTED`) from
the Apex **class** node (`apex_<stem>`) to `flow_<name.lower()>`, plus a flow
node **stub** (id + label + `file_type: "flow"`, no `source_file`) so the
per-file edge is not dangling before merge. The stub merges with the real flow
node parsed from `<Name>.flow-meta.xml` by shared id (ADR-002/ADR-012):
`_flow_name` and the Apex side both lowercase the identical underscore-preserving
API name, so ids match deterministically with no fuzzy resolution. The stub
omits `source_file` precisely so it cannot shadow the real flow's definition path
when merged first (`_merge_into` fills gaps, first-write-wins).

`calls` was chosen over a new relation because launching a flow is semantically a
call (Apex invokes the flow like a method); the relation is already registered
(`SF_RELATIONS`, `CALLS` in Neo4j) and its consumers all safely ignore the
class → flow shape: `drop_same_class_calls` / `_owner_class_id` operate only on
method-node endpoints, and recursion detection can't cycle through a flow node
(flows have no outgoing `calls`). The `context` attribute distinguishes flow
launches from method calls for any consumer that cares.

### Consequences

- **Apex → Flow dependencies are now traversable** in impact analysis, and a
  flow's callers are visible alongside its `flow_invokes` callees — the two
  relations together give the full bidirectional Apex ↔ Flow picture.
- **Detection is deterministic**, keyed on the `new Flow.Interview.X` construction
  rather than the fragile `f.start()` call site (which would need variable-type
  tracking to a flow); the launch is the reliable signal.
- Only the tree-sitter parser handles this; the regex fallback (`apex_enhanced`)
  does not, consistent with `instantiates` (ADR precedent) — the fallback is a
  degraded path used only when the grammar is unavailable.
- Verified on eventspark: `apex_emailsendusingflow` → `flow_send_email_using_flow`
  resolves to the real flow node (its `source_file` is the `.flow-meta.xml`),
  0 dangling.
