# Signed-in browser suite

This suite uses a private PostgreSQL stack and per-run RSA tokens checked by the production verifier. Identity controls cover valid, absent, wrong-key and expired tokens. The project test pins finding 1: creating a project from a drawing currently leaves zero drawing versions, with the expected failure armed only after setup succeeds.

Run it with:

```sh
python C:/Users/ehaug/.claude/scripts/mini_e2e.py run --worktree <worktree> --config playwright.signed-in.config.mjs
```

The `signedInUser` fixture provisions a real organization with a bootstrap token, then installs a bound token in an isolated Playwright context. The private signing key stays in process memory; only public JWKS is written to disk.

This does not prove the Auth0 login screen or staging or production authentication. PostgreSQL binaries must be installed on the host. The suite compiles the harness when needed and is not in the required gate.
