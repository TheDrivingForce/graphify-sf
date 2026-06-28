# Release Notes

## 2026-06-28 22:23

### Breaking Changes

#### LWC modelled as a bundle of file nodes (multiple JS/HTML per component)

An LWC is a *bundle* of files in a component folder. Previously the parser created a single `lwc_component` node per file **stem** and merged the matching `*.html` into the `*.js`, which (a) treated supplemental files as separate components and (b) orphaned any extra HTML template (`calendarView.html`, etc.) or supplemental JS module that didn't match the folder name.

The model is now bundle-based:

- **Component identity is the folder**, not the file stem: `lwc_<folder>` (e.g. `lwc_upcomingeventsviews`). This preserves the external link contract — `embeds` / `wire_to` / `imports` / `calls` still resolve to a component by this id (ADR-002).
- Each JS file becomes an `lwc_controller` node (`lwc_<folder>_js_<stem>`); each HTML template an `lwc_template` node (`lwc_<folder>_html_<stem>`). CSS is ignored, as before.
- Every file links to the bundle with a **`part_of`** edge. Supplemental JS/HTML are now first-class bundle members instead of being orphaned.
- The main file (stem matches the folder) is flagged `sf_main_controller` / `sf_main_template`; the main controller's `export default class` name becomes the bundle label.
- **Behavioral edges now source from the specific file node** that contains them (`@wire`, `@api`, `c/` imports, `calls`, and `embeds` from `c-` tags), not the bundle — so the graph attributes each dependency to the file that owns it.
- The bundle node carries summary flags `sf_has_template`, `sf_js_file_count`, `sf_html_file_count`.

New node types `lwc_controller` / `lwc_template` and relations `part_of` / `imports` / `member_of` / `instantiates` are registered in the SF schema reference, Neo4j export, and viz colour map.

Verified across the eventspark repo: 115 bundles, 0 orphan file nodes, 0 dangling edges, and all 5 multi-template bundles (e.g. `eventSpeakers` with 5 templates, `upcomingEventsViews` with 6) now correctly aggregate every template.

## 2026-06-28 21:52

### New Features

#### LWC-to-LWC JS-module imports and calls

JS-only LWC "service" modules (a `*.js` file with no `*.html` template, exporting named functions other components import) are now linked. Previously such a module was orphaned — nothing referenced it.

The LWC JS parser (`lwc.py`) now:

- Models each exported function (`export { foo, bar }`, `export function foo`, `export const foo = ...`) as a callable node `lwc_<stem>_<fn>` with a `member_of` edge to the component.
- Detects `import { foo } from 'c/otherModule'` and emits an `imports` edge (consumer → `lwc_<module>`).
- When an imported name is actually invoked (`foo(...)`) in the body, emits a `calls` edge to the specific function node `lwc_<module>_<foo>`.

Targets are emitted as stub nodes (ADR-012) that merge with the exporting module's real nodes via shared IDs (ADR-002), so the consumer's `calls` edge resolves to the exporter's function. Imported-but-uncalled names produce an `imports` edge only (a dependency, not a call). Base (`lwc`), `lightning/...`, and `@salesforce/...` imports are not treated as `c/` module links.

## 2026-06-28 21:29

### Fixes

#### Pure DTO / wrapper classes no longer orphaned when instantiated

A class instantiated only via `new TypeName()` — with no method ever called on it — was previously orphaned in the graph. The Apex parser walked only `method_invocation` nodes, so a `new TranslationResponse()` against a methodless DTO/wrapper produced no edge. The parser now also walks `object_creation_expression` nodes (`_constructed_type_name` in `apex_ts.py`), stashing the constructed type as `sf_unresolved_refs` on the class node. A new SF-wide pass `resolve_apex_refs` (`apex_calls.py`, wired into `extract_sf`) links the constructing class to the constructed class with an `instantiates` edge. Resolution is conservative — a name is linked only when exactly one Apex class declares it; collection containers (`new List<X>()`) and builtins (`new String()`) are skipped.

## 2026-06-28 18:26

### New Features

#### Method / class access scope on Apex nodes (`sf_scope`)

Apex class, trigger, interface, and method nodes now carry an `sf_scope` attribute holding the declared access modifier (`public` / `private` / `protected` / `global`). An un-annotated method falls back to `private`, matching Apex's implicit visibility. Scope is read from the AST in the tree-sitter parser (`apex_ts.py`) and from the modifier regexes in the fallback parser (`apex_enhanced.py`), so both paths emit the attribute.

#### `--no-same-class-calls` extract flag

New `graphify sf extract --no-same-class-calls` option restricts Apex `calls` edges to **inter-class** links only — intra-class method→method calls are dropped, so the call graph shows relationships *between* classes rather than each class's internal control flow. The filter runs after cross-file call resolution (`drop_same_class_calls` in `apex_calls.py`), so downstream passes (recursion detection) operate on the filtered edge set. Threaded through `extract_sf(no_same_class_calls=...)`.

### Fixes

#### CLI no longer crashes on non-UTF-8 consoles

`graphify sf` commands print status emoji (🚀 / ✅ / 💾) and other non-ASCII characters. On Windows the console is often a legacy code page (cp1252), where these raised `UnicodeEncodeError` and aborted the command (notably `extract`). `main()` now reconfigures stdout/stderr to UTF-8 with a `backslashreplace` fallback (`_force_utf8_output` in `cli.py`), so commands run cleanly regardless of console encoding — no `PYTHONIOENCODING=utf-8` workaround needed.

## 2026-06-27 22:09

### New Features

#### Apex parser rewritten on tree-sitter (`apex_ts.py`, `apex_enhanced.py`)

The Apex parser (`.cls` / `.trigger`) is now AST-based, built on the vendored `tree_sitter_apex` grammar (from the MIT-licensed `aheber/tree-sitter-sfapex`), replacing the former regex parser. `extract_apex_enhanced` now delegates to the tree-sitter implementation and falls back to the regex parser only when the grammar is not installed — both emit the same node/edge shapes and the same `apex_<class>` IDs, so the Flow / LWC / profile parsers and the CPQ / governor / OoE passes are unaffected.

- The grammar is an **optional** dependency (`graphify-sfdx[apex]`), vendored under `vendor/tree-sitter-apex/` and resolved via `[tool.uv.sources]` until it is published to PyPI. When absent, parsing degrades gracefully to the regex fallback.
- No more comment stripping: tree-sitter parses comments as nodes the walk never visits, so doc comments and commented-out code cannot pollute the parse.
- Loop detection (`sf_in_loop`) is now an exact ancestor walk over `for` / `while` / `do` nodes instead of brace counting.
- `interface` declarations are now modelled (`sf_code_type: "interface"`); the regex parser could not see them.
- Validated by a differential over 2,345 real Apex classes: zero regressions, zero crashes, and strictly more accurate extraction (real methods and interfaces the regex missed; correct rejection of uncompilable stubs).

#### Overload-aware Apex method nodes (`apex_ts.py`)

Overloaded methods no longer collapse onto one node. Disambiguation is applied **only when a name is overloaded** within a class: a lone `foo` keeps the bare ID `apex_<class>_foo` (preserving the cross-parser contract that LWC / Flow / profiles rely on), while overloads get a normalized param-type suffix — e.g. `apex_<class>_process_id` vs `apex_<class>_process_list_id`.

#### Apex → Apex call edges (`apex_ts.py`, `apex_calls.py`)

The graph now models method-to-method calls. Intra-file calls (`this.foo()`, same-class `foo()`, typed `var.bar()`) are resolved during the parse and emitted as `calls` edges; cross-file calls are resolved after merge by a new `resolve_apex_calls` pass that runs before `detect_recursive_triggers` so the new edges feed recursion-cycle detection (ADR-027). Resolution is conservative: a `Class.method()` or typed-instance call with a known receiver class is `EXTRACTED`; a bare name found in exactly one class is `INFERRED`; anything ambiguous (name in multiple classes, or overload arity not uniquely matched) produces no edge rather than a wrong one. Call edges are deduplicated per `(caller, callee)` pair.

### Improvements

#### Method-to-class edge renamed `calls` → `method_of` (`apex_ts.py`, `apex_enhanced.py`)

The edge linking a method to its owning class was previously `calls`, which read backwards (a method does not call its class). It is now `method_of` — source = method, target = class — mirroring `field_of` (field → object). This also cleanly separates membership from real calls: `calls` is now reserved for genuine method → method invocations, so `detect_recursive_triggers` (which builds its cycle graph from `calls`) no longer sees membership edges as noise. The relation maps to the Neo4j `METHOD_OF` type.



### Bug Fixes

#### Apex comments no longer pollute the parse (`apex_enhanced.py`)

The Apex parser ran its regexes against raw source that still contained comments. A doc comment such as `This class must be kept in sync` caused the class-declaration regex to match `class must`, so the class node was labelled `"must"` instead of the real class name. Commented-out code could likewise create phantom method / SOQL / DML / `implements` signals.

- Comments (`//` line and `/* */` block, including `/** */` doc) are now blanked out before matching, with line numbers and offsets preserved so loop / SOQL / DML line detection stays accurate.
- String literals are skipped, so a `//` or `/*` inside a string is not mistaken for a comment.
- The original, un-stripped source is still stored on the class node for the CPQ / governor passes.
- Regression test added (`test_apex_parser_ignores_comments`).

## 2026-06-27 19:34

### New Features

#### LWC-to-LWC composition edges (`lwc.py`)

The graph now models component composition: when an LWC template embeds another **local custom** LWC, an `embeds` edge is created from the parent component to the embedded child. This surfaces structural dependencies that were previously invisible — e.g. answering "if I change `badgeSection`, which components embed it?".

- The HTML parser (`extract_lwc_html`) scans templates for `c-` namespaced tags (e.g. `<c-badge-section>`) and emits one `embeds` edge per unique embedded child.
- Only local custom components (`c-` prefix) are matched. Base Lightning components (`<lightning-button>`), Aura, and standard HTML tags are ignored.
- Both paired (`<c-foo>...</c-foo>`) and self-closing (`<c-foo/>`) tags are detected. Duplicate child tags within one template produce a single edge.
- Child components are emitted as stub nodes (no dangling edges) and merge with the child's real node via the shared ID — the same cross-file resolution used for `wire_to`/Apex (ADR-002).
- Edges are sourced from the **base** component ID (`lwc_<stem>`), not the HTML sidecar node, so they survive the LWC merge pass.
- New `embeds` relation maps to the Neo4j `EMBEDS` relationship type.

**ID resolution:** a `<c-badge-section>` tag resolves to `lwc_badgesection` — kebab-case tag names map deterministically to the lowercased camelCase component folder name, so no separate resolution pass is required.

### Bug Fixes

#### CPQ data extraction crash on field nodes (`cpq_data.py`)

`_ensure_field_node` called `_field_nid` with a single argument after `objects._field_nid` was changed to require both an object and field API name (the 2026-06-27 15:26 release). This raised a `TypeError` on any CPQ data extraction (`extract_cpq_data`).

- CPQ field node IDs are now object-scoped: a condition/action field is attributed to the object its rule applies to (`SBQQ__LookupObject__c` / `SBQQ__EvaluationEvent__c`, defaulting to `SBQQ__Quote__c`).
- Field nodes carry `sf_object`, and their IDs (`field_{object}_{field}`) now merge with the same field parsed from object metadata (ADR-002).

#### MDT mapping field nodes failed to merge (`mdt_mapping.py`)

`mdt_mapping.py` carried its own stale copy of `_field_nid` producing the old unscoped ID scheme (`field_{field}`). After the object-scoping change this silently diverged: `maps_to` field endpoints no longer merged with field nodes parsed from object metadata.

- The local `_field_nid` now delegates to `objects._field_nid`, scoping each field to the object named in the mapping record (Main/Source and Second/Target objects).
- Falls back to `"unknown"` as the object scope when a mapping record omits the object, keeping the ID stable.

## 2026-06-27 15:26

### Bug Fixes

#### Field node IDs now unique per object (`objects.py`)

Fields with the same API name on different SObjects (e.g. `Name`) previously produced the same node ID and were incorrectly merged into a single node. The merged node also lost its `file_type: "field"`, causing it to appear as `"concept"` in graph.json.

- `_field_nid` now takes both the object API name and field API name, producing `field_{object_api}_{field_api}` using full API names including `__c` suffixes.
  - Before: `field_billingcity`
  - After: `field_account_billingcity__c` (example)
- Fields with the same name on different objects now get distinct nodes and retain `file_type: "field"`.

#### SObject node ID derived from API name, not label (`objects.py`)

`extract_custom_object` was building the SObject node ID from `<label>` (human-readable display name) rather than `<fullName>` (API name). This caused stub nodes created by Apex, Flow, and lookup-field parsers (which correctly use the API name via `sobject_nid()`) to fail to merge with the object parser's node.

- Now reads `<fullName>` as the authoritative API name; `<label>` is kept for display only.
- `sf_object_type` custom/standard detection also corrected to check the API name.

### Improvements

#### Salesforce file types whitelisted in `build_from_json` (`build.py`)

The core `build_from_json` function normalised any unrecognised `file_type` to `"concept"`, which erased SF-specific node types when building graphs via the base pipeline. The full set of Salesforce types is now whitelisted:

`sobject`, `field`, `flow`, `validation_rule`, `lwc_component`, `profile`, `permission_set`, `permission_set_group`, `record_type`, `workflow`, `cmt_record`, `sharing_rule`, `custom_label`, `cpq_rule`, `cpq_condition`, `cpq_action`, `cpq_qcp_method`

#### `.graphifyignore` respected during SF extraction (`__init__.py`)

`extract_sf` previously walked the repository with a plain `rglob("*")`, ignoring any `.graphifyignore` file. It now loads and applies ignore patterns, consistent with the core pipeline behaviour.

#### `--no-ooe` flag to skip Order of Execution generation (`cli.py`, `__init__.py`)

`extract_sf` now accepts an `ooe: bool = True` keyword argument, and the CLI `extract` subcommand exposes it as `--no-ooe`. When set, the 18-step OoE chain nodes and edges are not generated, producing a smaller graph. Useful for orgs where OoE analysis is not needed or where graph size is a concern. Default behaviour is unchanged.

```bash
graphify-sfdx extract path/to/sf-repo --no-ooe
```

#### `cluster-only` subcommand added to `graphify-sfdx` (`cli.py`)

The base `graphify cluster-only` command runs in a separate Python environment that does not recognise SF-specific `file_type` values, causing `sobject`, `field`, `flow`, and other SF node types to be silently downgraded to `"concept"` on every re-cluster.

`graphify-sfdx cluster-only` is a direct replacement that runs entirely within the graphify-sfdx package, preserving all SF node types. It supports the same community label remapping and resolution tuning as the base command.

```bash
# instead of: graphify cluster-only .
graphify-sfdx cluster-only .

# with explicit graph path:
graphify-sfdx cluster-only --graph graphify-out/graph.json

# with resolution tuning:
graphify-sfdx cluster-only . --resolution 1.5
```

#### `--no-fields` flag to strip field nodes from the graph (`cli.py`, `__init__.py`)

`extract_sf` now accepts a `fields: bool = True` keyword argument, and the CLI `extract` subcommand exposes it as `--no-fields`. When set, all `field` nodes and their edges are removed after all analysis passes complete (so CPQ/validation overlap analysis is unaffected). Produces a smaller graph when field-level detail is not needed.

```bash
graphify-sfdx extract path/to/sf-repo --no-fields
graphify-sfdx extract path/to/sf-repo --no-fields --no-ooe
```

#### SF node types preserved through merge (`__init__.py`)

`_merge_into` previously used a strict first-wins rule for all attributes including `file_type`. If a stub or parse-error node arrived first with `file_type: "concept"`, a later node carrying the correct specific type (e.g. `"sobject"`) could never overwrite it. `file_type` is now upgradeable from `"concept"` — any more specific type wins regardless of arrival order.

#### `source` attribute no longer leaks onto graph nodes (`pipeline.py`)

`build_sf_graph` filtered only `"id"` when copying node attributes into the DiGraph. It now also excludes `"source"` (an edge key) to prevent it polluting node data.
