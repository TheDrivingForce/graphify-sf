# Analysis Pass: Order of Execution

**Pass:** SF-3  
**Module:** [`graphify/salesforce/order_of_execution.py`](../../graphify/salesforce/order_of_execution.py)  
**Runs after:** CPQ analysis pass (SF-1), LWC merge pass (SF-2)  
**Mutates:** `all_nodes` and `all_edges` in place

---

## Purpose

Materializes the Salesforce Order of Execution as an explicit 18-step node chain in the graph for every SObject that actually triggers it. This lets impact analysis, shortest-path queries, and LLM consumption reason about *when* in the save cycle each piece of automation fires — without that knowledge being implicit or buried in documentation.

---

## When nodes are created

The pass scans all edges for targets of two relations:

| Relation | Source | Meaning |
|---|---|---|
| `triggers_on` | Apex trigger node | A trigger fires on this SObject |
| `validates` | Validation rule node | A validation rule fires on this SObject |

Any SObject reached by one of these edges gets an 18-step chain. SObjects that are only referenced via `queries` or `dml_operates_on` (SOQL reads / DML writes without a trigger) are **intentionally excluded** — a SELECT does not run the Order of Execution, and generating chains for every queried standard object (Account, Contact, etc.) would bloat the graph.

---

## What gets created

For each qualifying SObject, 18 `concept` nodes are appended — one per Salesforce OoE step:

| Step | Label | Trigger type |
|---|---|---|
| 1 | System Validation | system |
| 2 | Before Trigger | apex |
| 3 | Custom Validation | validation_rule |
| 4 | Before-Save Flow | flow |
| 5 | CPQ Calc Engine | cpq |
| 6 | Duplicate Rules | duplicate_rule |
| 7 | Database Save (Before Commit) | system |
| 8 | After Trigger | apex |
| 9 | Assignment Rules | assignment_rule |
| 10 | Auto-Response Rules | auto_response_rule |
| 11 | Workflow Rules | workflow_rule |
| 12 | Process Builder | process_builder |
| 13 | After-Save Flow | flow |
| 14 | Escalation Rules | escalation_rule |
| 15 | Roll-Up Summary | rollup_summary |
| 16 | Criteria-Based Sharing | sharing |
| 17 | Commit DML | system |
| 18 | Post-Commit Logic | post_commit |

### Node ID format

```
ooe_{sobject_id}_{step_num}
```

Example: `ooe_sobject_quote__c_2` = the "Before Trigger" step for `Quote__c`.

### Node attributes

| Attribute | Value |
|---|---|
| `file_type` | `"concept"` (synthetic — no backing metadata file) |
| `label` | `"{SObject label}: {step description}"` |
| `sf_ooe_step` | Integer 1–18 |
| `sf_ooe_sobject` | Owning SObject node ID |
| `source_file` | `""` (generated, not parsed) |

---

## Edge structure

Three `order_of_execution` edges are emitted per SObject:

1. **Anchor edge** — `SObject → step 1 (System Validation)` makes the chain reachable from the object.
2. **Chain edges** — `step N → step N+1` for all 18 steps, forming a linear DAG.

```
SObject ──order_of_execution──▶ step_1 ──▶ step_2 ──▶ … ──▶ step_18
```

The `order_of_execution` subgraph is validated as a DAG by `validate_sf.validate_sf_graph` (cycles indicate a bug in the pass).

---

## Consumers

| Location | How it uses OoE |
|---|---|
| [`query.py:241`](../../graphify/salesforce/query.py#L241) | `ooe_chain(sobject)` — walks `order_of_execution` edges from the SObject to return the ordered step list |
| [`validate_sf.py:167`](../../graphify/salesforce/validate_sf.py#L167) | Asserts the OoE subgraph is acyclic |
| [`neo4j_sf.py:62`](../../graphify/salesforce/neo4j_sf.py#L62) | Maps `order_of_execution` → `ORDER_OF_EXECUTION` for Neo4j export |
| CLI `graphify-sfdx ooe <SObject>` | Prints the OoE chain for a given SObject |
| MCP tool `sf_ooe` | Returns the OoE chain to an LLM client |

---

## Design notes

- **Idempotent** — if `ooe_{sobject_id}_1` already exists the SObject is skipped, so running the pass twice is safe (ADR-016).
- **`file_type: "concept"`** — steps are synthetic; they have no backing metadata file.
- **O(N) graph-wide pass** — never cached per file; always re-run on the full merged graph.
- **Step definitions** live in [`graphify/salesforce/constants.py`](../../graphify/salesforce/constants.py) as `SALESFORCE_OOE_STEPS` so any change to the canonical step list is made in one place.
