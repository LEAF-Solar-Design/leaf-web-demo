# Published tools

This directory is empty until the first production signing (apart from this guide).
The server distributes reviewed artifacts using this layout:

```text
<name>/<version>/envelope.json
<name>/<version>/package.zip
revocations.json                 # optional publisher revocation list
```

`LEAF_PUBLISHED_TOOLS_DIR` can select a different read-only catalog root.
Envelopes and revocation lists are signed offline with the publisher key using
`tools/tool-signer` in Branch2025, then published by a reviewed PR. No private key
ever lives in this repo. The server validates shape and distributes stored bytes;
the plugin verifies signatures, entitlement tiers, and revocation.
