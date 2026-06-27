# Release Notes

## Bug Fixes

### Apex comments no longer pollute the parse (`apex_enhanced.py`)

The Apex parser ran its regexes against raw source that still contained comments. A doc comment such as `This class must be kept in sync` caused the class-declaration regex to match `class must`, so the class node was labelled `"must"` instead of the real class name. Commented-out code could likewise create phantom method / SOQL / DML / `implements` signals.

- Comments (`//` line and `/* */` block, including `/** */` doc) are now blanked out before matching, with line numbers and offsets preserved so loop / SOQL / DML line detection stays accurate.
- String literals are skipped, so a `//` or `/*` inside a string is not mistaken for a comment.
- The original, un-stripped source is still stored on the class node for the CPQ / governor passes.
- Regression test added (`test_apex_parser_ignores_comments`).
