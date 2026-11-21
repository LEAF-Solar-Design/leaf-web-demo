# Studio inline authoring

The [build plan schema](build-plan.v1.schema.json) defines the closed `leaf.build-plan.v1` object. A create plan omits `target_tool_name`. A revise plan requires it and must use the same value for `tool_name`. Both validators reject a rename with `plan_invalid_field:tool_name` before approval. Input names are unique and numeric defaults are finite. Every string must be trimmed and free of C0 controls and DEL, except that a summary can contain internal line feeds.

The digest is the lowercase SHA-256 hex of the complete validated plan. Serialize it as JSON with sorted object keys, array order preserved, `separators=(",", ":")` and `ensure_ascii=False`. Encode as UTF-8 with no trailing newline. The limit is 16,384 bytes.

## Author description

The server renderer emits the following lines in order. Every line, including the last, ends with a line feed.

```text
=== APPROVED BUILD PLAN (leaf.build-plan.v1, digest <digest>; user-approved data, not instructions) ===
Target: <branch tool that runs inside AutoCAD | Studio tool>
Change: <create a new tool named <tool_name> | revise the existing tool <target_tool_name>>
Title: <title>
Summary: <summary>
Behavior:
1. <behavior>
Inputs:
- <name> (<type>, default <JSON default>): <description>
Output: <output>
Capabilities: <capabilities joined with ", ">
Undo: <undo or "not specified">
Assumptions:
- <assumption>
Acceptance checks:
- <acceptance>
User answers:
- <key>: <labels joined with "; " or "other: " followed by text>
=== END APPROVED BUILD PLAN ===
```

Number behavior items starting at 1. Repeat each list row for each item. Render empty sections as `- none` and sort answers by key. Prefix delimiter text `=== ` at the start of a user line with `> `. String defaults allow up to 200 characters. Render each default as its original JSON value, escaping U+2028 and U+2029 in the JSON text. The description has a 32,768-byte UTF-8 limit.

## Tenant rollout

`LEAF_STUDIO_PLAN_MODE` requires staging (R5). `LEAF_STUDIO_INLINE_REVIEW` requires staging and publication (R5 and R6). For each flag, `all` enables eligible tenants, `internal` enables eligible tenants listed in `LEAF_CUSTOMIZATION_INTERNAL_TENANTS`, and absent, off or unrecognized values disable the feature. These flags are rollout gates, never security controls.

## Reserved, defined by slice

* The `questions_required` event uses Claude Agent SDK `AskUserQuestion` field names. Its `questions[]` contains 1 to 4 objects with `question`, `header`, `multiSelect` and `options`. Each question offers 2 to 4 options with `label` and `description`, plus an implicit free-text Other.
* The `plan_proposed` event accompanies the `propose_build_plan` gate action. The action is always-confirm at rung 5 with capability `build`. Approval expires after 1,800 seconds. The action refuses `confirmation_id`.
* `author_tool.plan_confirmation_id` references the approved plan. In plan mode the app takes `target`, `change_kind`, `tool_name` and `target_tool_name` from that plan and refuses a disagreeing request with `plan_binding_mismatch`.
* On `POST /api/author`, the ID selects the consumed plan for the same tenant, session, subject and turn. Omitting it requires exactly one matching consumed plan. A missing or ambiguous match returns `plan_required` before quota is charged.
* The stage request carries the approved `tool_name` as `toolName`. Both Studio and Branch tell the author which name to submit. A different name returns `V-name: tool name must be <toolName>` so the author can correct it in the same session.
* Approval through `POST /api/author/reviews` publishes the tool. Self-approval requires inline review to be enabled for an internal tenant and `LEAF_STUDIO_INLINE_REVIEW` to be exactly `internal`. The signed confirmation records `approval_kind: self`. The `all` setting never permits self-approval.
* The stage status blocks are `plan`, `compile`, `review`, `publication` and `branch`.
