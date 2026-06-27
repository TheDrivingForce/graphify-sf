# Apex Parser

**Primary module:** [`graphify/salesforce/apex_ts.py`](../../graphify/salesforce/apex_ts.py) (tree-sitter)
**Fallback module:** [`graphify/salesforce/apex_enhanced.py`](../../graphify/salesforce/apex_enhanced.py) (legacy regex, used only when the grammar is absent)
**Dispatched for:** `.cls` and `.trigger` files (see [`graphify/salesforce/__init__.py`](../../graphify/salesforce/__init__.py))
**Entry point:** `extract_apex_enhanced(path)` — delegates to `apex_ts.extract_apex` when the grammar is installed, else the regex parser
**Returns:** `{"nodes": [...], "edges": [...]}` (or empty lists plus an `"error"` key)

---

## Overview

An **AST-based** parser built on the vendored `tree_sitter_apex` grammar (from [`aheber/tree-sitter-sfapex`](https://github.com/aheber/tree-sitter-sfapex), MIT). It parses each file once and walks the syntax tree. The grammar is an **optional dependency** (`graphify-sfdx[apex]`); when it is not installed, `extract_apex_enhanced` degrades to the legacy regex parser so analysis still runs. Both emit the **same node/edge shapes** and the same `apex_<class>` ids.

Confirmed node-type names for the grammar are in [apex-treesitter-node-types.md](apex-treesitter-node-types.md).

Every SObject node ID is built via [`sobject_nid()`](../../graphify/salesforce/constants.py) (ADR-002). This is what makes cross-file resolution work: the Apex, Flow, and Object parsers all converge on the *same* node ID for a given SObject, so `build_graph()` can merge them.

**No comment stripping needed.** Tree-sitter parses comments as `comment` nodes that the named-node walk never visits, so doc comments and commented-out code cannot pollute the parse — the class of regressions the regex parser had to defend against (e.g. a doc comment making `class must` match) is structurally impossible here.

---

## Step 1 — Class / trigger / interface definition

`_find_definition()` walks the tree for the first `class_declaration`, `interface_declaration`, or `trigger_declaration`. (The regex parser could not see interfaces; the AST parser models them as `sf_code_type:interface`.) If none is found, the function returns an `"error"` result and the caller skips the file (ADR-009 lenient).

A single **code node** is created with:

- `id = apex_<filestem>`
- `sf_code_type` of `class`, `trigger`, or `interface`
- the original `source` attached (for the CPQ / governor passes)

---

## Step 2 — Method signatures

Each `method_declaration` node yields a method node. The name is the `identifier` directly preceding `formal_parameters` (so return-type identifiers are not confused for the name). A **`method_of`** membership edge runs from the method to its owning class (`confidence: EXTRACTED`) — mirroring `field_of` (field → object). This is deliberately *not* a `calls` edge: a method is a member of its class, not a caller of it; `calls` is reserved for real method→method invocations (see below). The label keeps a human-readable param list (`getAccounts(String name)`).

---

## Step 3 — SOQL queries

Each `soql_query_body` node is read for its `from_clause` → `storage_identifier` SObject. **Subquery bodies are skipped** (a `soql_query_body` whose parent is a `subquery`) — a `__r` child relationship after `FROM` is a traversal, not a queryable object — and `_is_sobject_name()` additionally drops `__r` names and non-SObject keywords. Each surviving object emits a `queries` edge `class -> sobject`, `confidence: EXTRACTED`.

---

## Step 4 — DML operations

`_build_var_types()` walks `local_variable_declaration` and `formal_parameter` nodes, reading the declared type from the AST. Collections (`List<Opportunity>`) resolve to the generic element type; non-SObject scalar/collection types (`_NON_SOBJECT_TYPES`) are skipped.

Each `dml_expression` node yields the keyword (`insert` / `update` / `delete`) from its `dml_type` child and the operand variable, resolved back to its SObject type. When the type cannot be inferred it falls back to `"unknown"` and the edge is tagged `sf_ambiguous`.

DML edges are `confidence: INFERRED` with a `confidence_value` of **0.9**, or **0.85** if in a loop.

---

## Loop detection (governor limits)

`_in_loop()` walks a node's **ancestors**; if any is a `for_statement`, `enhanced_for_statement`, `while_statement`, or `do_statement`, the SOQL/DML is tagged `sf_in_loop: true`. This replaces the regex parser's brace-counting and is exact for nested loops. It is the signal the governor-limit analysis pass (SF-4) uses to flag SOQL/DML-in-loop violations.

---

## Step 5 — Interface hints

The `interfaces` clause text is read structurally:

- `SBQQ.QuoteCalculatorPlugin` / `QuoteCalculatorPlugin` — emits an `implements` edge to a CPQ plugin node.
- `Database.Batchable` — sets `sf_async_pattern = "batchable"` on the class node.

---

## Overloads & Apex → Apex calls

Overloaded methods get **distinct** node ids. Disambiguation is applied **only when a name is overloaded** within a class (>1 declaration): a lone `foo` keeps the bare id `apex_<class>_foo` (preserving the cross-parser contract that lwc/flow/profiles rely on), while overloads get a normalized param-type suffix — e.g. `apex_<class>_process_id` vs `apex_<class>_process_list_id`. Method nodes also carry their body `source` so the recursion-guard scan works on method-level cycles.

**Intra-file calls** are resolved during the walk and emitted as method→method `calls` edges:

- `this.foo()` / bare `foo()` → the caller's own class (overload-matched by arity; ambiguous overloads are skipped).
- `var.baz()` where `var`'s declared type is a class in this file.

**Cross-file calls** are stashed on the class node as `sf_unresolved_calls` and resolved after all files are merged by `resolve_apex_calls` ([apex_calls.py](../../graphify/salesforce/apex_calls.py)), a pass that runs in `extract_sf` **before** `detect_recursive_triggers` so the new edges feed recursion-cycle detection (ADR-027). Resolution is **conservative** (mirrors the core resolver, #543):

- `Class.method(...)` / typed `var.method(...)` with a known receiver class → `EXTRACTED`.
- A bare name declared in exactly one class → `INFERRED`.
- Anything ambiguous (name in multiple classes, or overload arity not uniquely matched) → **no edge** rather than a wrong one.

## Migration notes (ADR-019)

The parser was migrated from regex to tree-sitter in a parity-then-extend effort. Phase A reproduced the regex parser's output exactly; a differential over 2345 real Apex classes showed **zero regressions** and strictly more accurate extraction (real methods and interfaces the regex missed; correct rejection of uncompilable stubs). Phase B added the overload-aware ids and Apex→Apex call resolution described above.
