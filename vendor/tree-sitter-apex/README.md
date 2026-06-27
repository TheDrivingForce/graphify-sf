# tree-sitter-apex (vendored)

Python binding for the **Apex** tree-sitter grammar, used by `graphify.salesforce`
to parse `.cls` / `.trigger` files into an AST.

## Provenance

- Grammar source: [`aheber/tree-sitter-sfapex`](https://github.com/aheber/tree-sitter-sfapex) (MIT, see `LICENSE.sfapex`).
- Vendored commit: `27a3091a1a444ce19d6099e00cd3788f019d0c2b`.
- Only the **apex** grammar is vendored (the upstream repo also ships soql/sosl/sflog).
  Embedded SOQL/SOSL is parsed inline by the apex grammar, so no separate grammar is needed.
- `_src/parser.c` is the upstream pre-generated parser; there is **no external scanner**.

## What this package exposes

```python
import tree_sitter_apex
from tree_sitter import Language, Parser
parser = Parser(Language(tree_sitter_apex.language()))
tree = parser.parse(b"public class Foo {}")
```

`language()` returns a `tree_sitter.Language` capsule (tree-sitter >= 0.25 API).
Root node type is `parser_output`. See
[`docs/salesforce/apex-treesitter-node-types.md`](../../docs/salesforce/apex-treesitter-node-types.md)
for the node-type map.

## Building locally

Requires a C toolchain.

- **Windows (MinGW gcc):** `pip install .` defaults to MSVC and fails without it.
  Build a wheel with MinGW and install that instead:
  ```
  python setup.py build_ext --compiler=mingw32 bdist_wheel
  pip install --no-deps --force-reinstall dist/tree_sitter_apex-*.whl
  ```
- **Windows (MSVC) / Linux / macOS:** `pip install .` works directly.

The base `graphify` install does not depend on this package; it is an optional
extra (`graphify-sfdx[salesforce]`). When absent, the Apex parser degrades
gracefully (mirrors the `{ts_module} not installed` path in `extract.py`).

## Regenerating the grammar

To bump the grammar, re-clone the upstream repo at the desired commit and copy
`apex/src/parser.c` + `apex/src/tree_sitter/*.h` into `tree_sitter_apex/_src/`.
No `tree-sitter generate` / node step is needed (upstream ships the generated parser).
