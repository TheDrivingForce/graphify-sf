# Release Notes Draft

## Bug Fixes

### Field node IDs now unique per object (`objects.py`)

Fields with the same API name on different SObjects (e.g. `Name`) previously produced the same node ID and were incorrectly merged into a single node. The merged node also lost its `file_type: "field"`, causing it to appear as `"concept"` in graph.json.

- `_field_nid` now takes both the object API name and field API name, producing `field_{object_api}_{field_api}` using full API names including `__c` suffixes.
  - Before: `field_billingcity`
  - After: `field_account_billingcity__c` (example)
- Fields with the same name on different objects now get distinct nodes and retain `file_type: "field"`.

### SObject node ID derived from API name, not label (`objects.py`)

`extract_custom_object` was building the SObject node ID from `<label>` (human-readable display name) rather than `<fullName>` (API name). This caused stub nodes created by Apex, Flow, and lookup-field parsers (which correctly use the API name via `sobject_nid()`) to fail to merge with the object parser's node.

- Now reads `<fullName>` as the authoritative API name; `<label>` is kept for display only.
- `sf_object_type` custom/standard detection also corrected to check the API name.

## Improvements

### Salesforce file types whitelisted in `build_from_json` (`build.py`)

The core `build_from_json` function normalised any unrecognised `file_type` to `"concept"`, which erased SF-specific node types when building graphs via the base pipeline. The full set of Salesforce types is now whitelisted:

`sobject`, `field`, `flow`, `validation_rule`, `lwc_component`, `profile`, `permission_set`, `permission_set_group`, `record_type`, `workflow`, `cmt_record`, `sharing_rule`, `custom_label`, `cpq_rule`, `cpq_condition`, `cpq_action`, `cpq_qcp_method`

### `.graphifyignore` respected during SF extraction (`__init__.py`)

`extract_sf` previously walked the repository with a plain `rglob("*")`, ignoring any `.graphifyignore` file. It now loads and applies ignore patterns, consistent with the core pipeline behaviour.

### `--no-ooe` flag to skip Order of Execution generation (`cli.py`, `__init__.py`)

`extract_sf` now accepts an `ooe: bool = True` keyword argument, and the CLI `extract` subcommand exposes it as `--no-ooe`. When set, the 18-step OoE chain nodes and edges are not generated, producing a smaller graph. Useful for orgs where OoE analysis is not needed or where graph size is a concern. Default behaviour is unchanged.

```bash
graphify-sfdx extract path/to/sf-repo --no-ooe
```

### `cluster-only` subcommand added to `graphify-sfdx` (`cli.py`)

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

### `--no-fields` flag to strip field nodes from the graph (`cli.py`, `__init__.py`)

`extract_sf` now accepts a `fields: bool = True` keyword argument, and the CLI `extract` subcommand exposes it as `--no-fields`. When set, all `field` nodes and their edges are removed after all analysis passes complete (so CPQ/validation overlap analysis is unaffected). Produces a smaller graph when field-level detail is not needed.

```bash
graphify-sfdx extract path/to/sf-repo --no-fields
graphify-sfdx extract path/to/sf-repo --no-fields --no-ooe
```

### SF node types preserved through merge (`__init__.py`)

`_merge_into` previously used a strict first-wins rule for all attributes including `file_type`. If a stub or parse-error node arrived first with `file_type: "concept"`, a later node carrying the correct specific type (e.g. `"sobject"`) could never overwrite it. `file_type` is now upgradeable from `"concept"` — any more specific type wins regardless of arrival order.

### `source` attribute no longer leaks onto graph nodes (`pipeline.py`)

`build_sf_graph` filtered only `"id"` when copying node attributes into the DiGraph. It now also excludes `"source"` (an edge key) to prevent it polluting node data.
