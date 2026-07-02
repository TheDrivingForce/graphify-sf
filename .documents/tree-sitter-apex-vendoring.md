# tree-sitter-apex: how it's built and what "vendoring" means

Reference notes on the vendored `tree_sitter_apex` package under
[`vendor/tree-sitter-apex/`](../vendor/tree-sitter-apex/), used by
`graphify.salesforce` to parse `.cls` / `.trigger` files into an AST.

---

## How the package is built

It's a **CPython C extension** — a small Python package wrapping a compiled
tree-sitter parser. Three layers:

1. **The grammar/parser** —
   [`_src/parser.c`](../vendor/tree-sitter-apex/tree_sitter_apex/_src/parser.c),
   a **~242,000-line** pre-generated C file. This *is* the Apex grammar,
   compiled into a giant LR parse-table state machine. Alongside it are the
   tree-sitter runtime headers in
   [`_src/tree_sitter/`](../vendor/tree-sitter-apex/tree_sitter_apex/_src/tree_sitter/)
   (`parser.h`, `array.h`, `alloc.h`). There is **no external scanner** and no
   `grammar.js` / `grammar.json` vendored — only the generated output.

2. **The Python↔C bridge** —
   [`_binding.c`](../vendor/tree-sitter-apex/tree_sitter_apex/_binding.c).
   It exposes one function, `language()`, which calls the C symbol
   `tree_sitter_apex()` (defined inside `parser.c`) and wraps the returned
   pointer in a `PyCapsule` tagged `"tree_sitter.Language"`. That's the handle
   the `tree_sitter` runtime expects.

3. **The build config** —
   [`setup.py`](../vendor/tree-sitter-apex/setup.py) declares one `Extension`
   that compiles `_binding.c` + `_src/parser.c` together into
   `_binding.<abi>.pyd` (e.g. the committed `.cp314-win_amd64.pyd`).
   [`__init__.py`](../vendor/tree-sitter-apex/tree_sitter_apex/__init__.py) just
   re-exports `language` from that compiled module.

At runtime:
`graphify.salesforce` → `tree_sitter_apex.language()` → C capsule →
fed to `tree_sitter.Parser`.

---

## Why C is required

Tree-sitter grammars **are C programs**. The grammar is authored in JavaScript
(`grammar.js`) upstream, but `tree-sitter generate` compiles that into
`parser.c` — a hand-unrollable LR parse table expressed as C arrays and switch
statements (that's why it's ~242k lines). To actually parse, that C must be
compiled to native code and linked against the tree-sitter runtime. Python
can't execute it directly, hence the `.c` → `.pyd` compile step and the need
for a C toolchain (MSVC, or MinGW per the README build notes).

---

## Where the actual language definition lives

**The editable grammar definition is NOT in this vendored package.** What's here
(`parser.c`) is generated, machine-written, and not meant to be edited by hand.

The real source is upstream, recorded in the package README provenance:

- **Repo:** [`aheber/tree-sitter-sfapex`](https://github.com/aheber/tree-sitter-sfapex) (MIT)
- **Pinned commit:** `27a3091a1a444ce19d6099e00cd3788f019d0c2b`
- Only the **apex** sub-grammar is vendored (upstream also has soql/sosl/sflog;
  embedded SOQL/SOSL is handled inline by the apex grammar).

The human-editable definition is `apex/grammar.js` in that upstream repo.

---

## What "vendored in" means

"Vendoring" means **copying a third-party dependency's source directly into your
repo and committing it**, instead of pulling it at install time from an external
source (pip, npm, a git submodule, etc.).

The dependency's code becomes *your* code in every practical sense: it lives in
your tree, it's in your git history, it builds with your build, and it ships
when you ship. The term comes from "vendor" — you've taken the vendor's product
in-house.

Here, everything under `vendor/tree-sitter-apex/` is a copy of files that
originated in `aheber/tree-sitter-sfapex`. The README provenance section is the
bookkeeping that records *where it came from and at exactly which commit*. That
provenance note is the only link back to upstream — there is **no live,
automatic connection**.

### How it was done here (a partial, transformed vendor)

1. **Selective copy.** Only the `apex` sub-grammar's *generated* artifacts were
   taken — `apex/src/parser.c` and `apex/src/tree_sitter/*.h` — copied into
   `_src/`. Upstream's other grammars (soql/sosl/sflog), its `grammar.js`, test
   corpus, and tooling were **left behind**. So the *output* of the grammar was
   vendored, not the editable *source*.

2. **Repackaging.** A thin Python wrapper that doesn't exist upstream was added
   — `_binding.c`, `setup.py`, `pyproject.toml`, `__init__.py` — turning the raw
   grammar into an installable `tree-sitter-apex` Python package. The upstream
   license was preserved as `LICENSE.sfapex` (required — it's MIT).

Mechanically, vendoring is "just copy files in and commit them" — but doing it
*well* means recording the source + commit, keeping the license, and writing
down the re-sync recipe, all of which the README does.

---

## What happens when upstream is modified

**Nothing — automatically.** This is the central trade-off of vendoring.

Because there's no live link (no submodule, no version range in a package
manager), changes in `aheber/tree-sitter-sfapex` after commit `27a3091...` have
**zero effect** on this repo. The copy is frozen at that commit until a human
deliberately re-syncs. That cuts both ways:

| | Vendored (this setup) | Live dependency (pip/submodule) |
|---|---|---|
| Upstream pushes a fix | Not received until re-sync | Received on next install/update |
| Upstream pushes a breaking change | Can't break you | Can break your build unexpectedly |
| Reproducible build | Always — code is right there | Depends on registry/network/pins |
| Works offline / forever | Yes | No (source could vanish) |
| Cost to update | Manual, deliberate effort | Often automatic |

So "what happens when upstream is modified" is really "**how do you pull
upstream changes into a vendor**" — a manual procedure.

---

## Re-syncing / modifying the grammar

To bump to a newer upstream commit (per the package README):

1. Re-clone `aheber/tree-sitter-sfapex` at the new desired commit.
2. Copy the regenerated `apex/src/parser.c` + `apex/src/tree_sitter/*.h` over
   the files in `_src/`.
3. Rebuild the C extension into a fresh `.pyd` / wheel:
   - **Windows (MinGW gcc):**
     `python setup.py build_ext --compiler=mingw32 bdist_wheel`
     then `pip install --no-deps --force-reinstall dist/tree_sitter_apex-*.whl`
   - **Windows (MSVC) / Linux / macOS:** `pip install .` works directly.
4. Update the "Vendored commit" line in the package README to the new SHA.

No `tree-sitter generate` / Node step is needed for a straight bump — upstream
ships the generated parser.

### If you want to change the grammar yourself

You **cannot** hand-edit the vendored `parser.c` — it's machine-generated. To
change grammar behavior you must fork upstream, edit its `grammar.js`, run
`tree-sitter generate` (Node + tree-sitter CLI), and vendor the regenerated
result. The editable source of truth permanently lives upstream (or a fork);
this repo only ever holds compiled-down copies. graphify *consumes* the grammar,
it doesn't own it.

### Caveat after any re-sync

A grammar bump can rename or restructure node types, and graphify's Apex
extractor keys off those names (root node `parser_output`, plus the node-type
map in
[`docs/salesforce/apex-treesitter-node-types.md`](../docs/salesforce/apex-treesitter-node-types.md)).
Re-validate the extractor against the new node types after any re-sync.
