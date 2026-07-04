# graphify-sfdx — Node Types & Link Types

A catalog of every node `file_type` and every edge `relation` that the
graphify-sfdx Salesforce extractor can produce. Compiled directly from the
parser/analysis-pass source (not just the declared schema in `validate_sf.py`),
so it reflects what is actually emitted.

_Last verified: 2026-07-02._

## Node types (`file_type`)

| Type | Emitted by | Represents |
|---|---|---|
| `sobject` | apex, flow, objects, metadata, cpq_data | A Salesforce object (Account, `SBQQ__Quote__c`, `X__mdt`) |
| `field` | objects, cpq_data, mdt_mapping | A custom field on an object |
| `code` | apex, lwc, flow, cpq_data, visualforce | Apex class/method node, LWC/JS function, stubbed callee |
| `flow` | flow | A Flow / Process Builder |
| `validation_rule` | objects | A Validation Rule |
| `record_type` | metadata | A Record Type |
| `workflow` | metadata | A Workflow rule set (per SObject) |
| `permission_set` | metadata | A Permission Set |
| `permission_set_group` | metadata | A Permission Set Group |
| `profile` | profiles | A Profile |
| `custom_label` | metadata | A Custom Label (`$Label.X` target) |
| `cmt_record` | metadata | A Custom Metadata record |
| `sharing_rule` | metadata | A Sharing Rule |
| `lwc_component` | lwc, visualforce | LWC bundle node (one per folder) |
| `lwc_controller` | lwc | One `.js` file in an LWC bundle |
| `lwc_template` | lwc | One `.html` template in an LWC bundle |
| `cpq_rule` | cpq_data | A CPQ Price/Product Rule |
| `cpq_qcp_method` | cpq, cpq_data | A QCP Calc-Engine callback method |
| `cpq_condition` | cpq_data | A CPQ Price/Error Condition record |
| `cpq_action` | cpq_data | A CPQ Price/Product Action record |
| `vf_page` | visualforce | A Visualforce `.page` |
| `vf_component` | visualforce | A Visualforce `.component` |
| `concept` | all parsers (fallback) | Parse-error nodes, stubs, and governor-limit sentinels |

## Edge relations (`relation`) — source → target

### Apex

| Relation | Source → Target | Meaning |
|---|---|---|
| `method_of` | method → class | Method membership |
| `calls` | method → method / VF → Apex / LWC fn → fn | Invocation |
| `queries` | method/flow → sobject | SOQL read |
| `dml_operates_on` | method/flow/workflow → sobject | DML write |
| `instantiates` | class → class | `new X()` |
| `implements` | class → interface/method | Interface implementation ⚠️ |
| `references` | field → sobject | Lookup / master-detail field reference ⚠️ |

### Flow

| Relation | Source → Target | Meaning |
|---|---|---|
| `flow_invokes` | flow → Apex class | Flow ApexAction |

### LWC

| Relation | Source → Target | Meaning |
|---|---|---|
| `part_of` | js/html file → bundle | Bundle membership |
| `embeds` | html/VF file → child component | `<c-…>` / `<c:…>` / Lightning Out |
| `wire_to` | LWC → Apex method | `@wire` |
| `lwc_calls` | LWC → Apex method | Imperative `@salesforce/apex` import |
| `imports` | js → `c/` module bundle | Module import |
| `member_of` | exported fn → bundle | Exported LWC function |

### Objects / metadata / permissions

| Relation | Source → Target | Meaning |
|---|---|---|
| `field_of` | field → object | Field membership |
| `validates` | validation rule → sobject | VR target |
| `record_type_of` | record type → sobject | RT target |
| `cmt_record_of` | CMT record → `__mdt` sobject | CMT membership |
| `shares` | sharing rule → sobject | Sharing target |
| `maps_to` | field → field | CMT field-mapping |
| `grants_access_to` | profile/permset → object/field | Access grant |
| `contains_permission_set` | PSG → permission set | Group membership |

### CPQ

| Relation | Source → Target | Meaning |
|---|---|---|
| `cpq_applies_to` | rule/QCP → object | Rule target (carries `execution_order`) |
| `cpq_has_condition` | rule → condition | |
| `cpq_has_action` | rule → action | |
| `cpq_reads_field` | condition → field | |
| `cpq_writes_field` | action → field | |

### Order of Execution

| Relation | Source → Target | Meaning |
|---|---|---|
| `order_of_execution` | OoE step N → step N+1 | DAG chain |

### Diagnostics / risk (ADR-027–030)

| Relation | Source → Target | Meaning |
|---|---|---|
| `governor_violation` | method → limit sentinel | Governor-limit / recursion risk |
| `gov_permission_violation` | CPQ rule → profile | FLS-restricted field |
| `cpq_validation_risk` | CPQ rule ↔ validation rule | Field-overlap conflict |
| `infinite_loop_risk` | flow ↔ CPQ | Loop risk |

## ⚠️ Schema drift (declared vs. emitted)

The declared registry in `graphify/salesforce/validate_sf.py`
(`SF_FILE_TYPES` / `SF_RELATIONS`) is out of sync with what the parsers
actually emit.

**Emitted but NOT declared** (base validation treats these as unknown types):

- Node types: `vf_page`, `vf_component` (new `visualforce.py`); plus base
  types `code` and `concept`, which every parser uses but are not in
  `SF_FILE_TYPES`.
- Relations: `implements` (`apex_enhanced.py`, `apex_ts.py`), `references`
  (`objects.py`).

**Declared but never emitted** (dead schema entries):

- Node type: `aura_component`.
- Relation: `publishes_event`.
