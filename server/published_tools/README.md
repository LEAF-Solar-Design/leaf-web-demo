# Published tools

This directory is empty until the first production signing (apart from this guide).
The server distributes reviewed artifacts using this layout:

```text
<name>/<version>/envelope.json
<name>/<version>/package.zip
<name>/<version>/audience.json   # required tenant visibility policy
revocations.json                 # optional publisher revocation list
```

`LEAF_PUBLISHED_TOOLS_DIR` can select a different read-only catalog root.
Envelopes and revocation lists are signed offline with the publisher key using
`tools/tool-signer` in Branch2025, then published by a reviewed PR. No private key
ever lives in this repo. The server validates shape and distributes stored bytes;
the plugin verifies signatures, entitlement tiers, and revocation.

Each tool version requires an `audience.json` beside its envelope and package.
To allow specific tenants:

```json
{"schema": "leaf.tool-audience.v1", "tenants": ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]}
```

To follow the current `LEAF_CUSTOMIZATION_INTERNAL_TENANTS` environment list:

```json
{"schema": "leaf.tool-audience.v1", "tenants": "internal"}
```

The file must be strict UTF-8 JSON, at most 4 KiB, with exactly `schema` and
`tenants`. Explicit tenant lists contain 1 to 1,000 canonical lowercase UUIDs
without duplicates. Duplicate keys, non-finite constants, extra keys, and invalid
tenant values are rejected. A missing, unreadable, or malformed audience hides
the tool from every tenant. Audiences are parsed when the catalog is loaded;
`internal` membership is resolved on each request. Hidden tools are absent from
the list and return the same `tool_not_published` 404 as unpublished tools for
envelope and package downloads. Revocation lists remain global. Responses retain
`Cache-Control: private, max-age=300`.
