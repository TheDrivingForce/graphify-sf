# sfapex grammar — confirmed node types (Phase 0 reference)

Source grammar: `aheber/tree-sitter-sfapex`, apex grammar. Built locally as a
`tree_sitter_apex` wheel (no external scanner; pre-generated `parser.c`).
Symbol: `tree_sitter_apex`. Root node type: **`parser_output`** (wraps the file).
Confirmed against `sf_AccountService.cls`, `sf_AccountTrigger.trigger`, and a
`__r` subquery case — all parse with `has_error == False`.

tree-sitter 0.25 note: `Node.sexp()` was removed; walk `node.children` /
`node.type` / `node.start_point` directly. Language is constructed as
`Language(tree_sitter_apex.language())`.

## Node-type map (for the Phase A walk)

| Concept | Node type | Extraction |
|---|---|---|
| Class | `class_declaration` | child `identifier` = name; `modifiers`→`modifier` for visibility |
| Interface | `interface_declaration` | same shape |
| Trigger | `trigger_declaration` | 1st `identifier` = trigger name, **2nd `identifier` = SObject**; `trigger_event` children (`before_insert`, `after_update`, …) |
| Class body | `class_body` / `interface_body` / `trigger_body`→`block` | container for members |
| Method | `method_declaration` | return type child (`generic_type` \| `void_type` \| `type_identifier`), then `identifier` = name, then `formal_parameters` |
| Params | `formal_parameters`→`formal_parameter` | each has a type child + `identifier`; used for var-types and overload signature |
| Local var | `local_variable_declaration` | type child + `variable_declarator`→`identifier` (var name) |
| Generic type | `generic_type` | `type_identifier` (container) + `type_arguments`→`type_identifier` (element) — gives `List<Opportunity>`→`Opportunity` |
| SOQL | `query_expression`→`soql_query_body` | `from_clause`→`storage_identifier`→`identifier` = SObject |
| SOQL subquery (`__r`) | nested `subquery`→`soql_query_body`→`from_clause` | structurally nested under `select_clause`; relationship name lands here, NOT a root FROM target |
| DML | `dml_expression` | `dml_type`→keyword child (`insert`\|`update`\|`delete`\|`undelete`); operand is the following `identifier`/expression |
| Loops | `enhanced_for_statement`, `for_statement`, `while_statement`, do-while | ancestor-walk: any of these in the ancestry ⇒ `sf_in_loop` |
| Call | `method_invocation` | callee = trailing `identifier`; receiver = leading `identifier` or `field_access`; args in `argument_list` |
| Comments | `comment` | unnamed/skipped by named-walk — no manual stripping needed |

## Build notes (Win11 dev box)

- No MSVC; built with MinGW gcc 13.2 via `python setup.py build_ext --compiler=mingw32`.
- `pip install .` fails (defaults to MSVC). For the vendored package, document the
  `--compiler=mingw32` path for local Windows dev, or rely on `cibuildwheel` (MSVC in CI) for distribution.
- Extension: sources = `_binding.c` + `_src/parser.c`, include `_src` (for `tree_sitter/*.h`), `-std=c11`.
