# Runbook: roles override mount loss

Audience: the on-call operator. Scope: `server/roles.py` and the app
container's `LEAF_ROLES_FILE` override. Last verified: 2026-10-05 against
the role loader and deployment documentation (source inspection).

## Symptom

`LEAF_ROLES_FILE` points at a missing or unreadable file, often after a
mounted volume disappears or its permissions change. In `server/roles.py`,
`load_role_policy()` returns `{}`: every role grant is absent, including
`platform_admin` elevated grants. The tier baseline still applies, so
capabilities already granted by the plan remain available.

Requests that relied on role grants may now receive 403s. This looks like a
permissions bug, but the behavior is by design: role overrides fail closed.
A vanished override must never silently restore the shipped defaults,
which could undo an operator's emergency revocation. The loader returns no
grants without a log line; the missing policy does not itself cause a 503.

## How to confirm

1. Check `LEAF_ROLES_FILE` on the running app container, rather than in the
   host shell. For the compose stack:

   ```bash
   docker compose exec app printenv LEAF_ROLES_FILE
   ```

2. Check that the file exists at that exact path on the mounted volume and
   is readable by the app's runtime user. Run `ls` and `cat` inside the app
   container as that user:

   ```bash
   docker compose exec app sh -c 'ls -l -- "$LEAF_ROLES_FILE"'
   docker compose exec app sh -c 'cat -- "$LEAF_ROLES_FILE"'
   ```

   A missing path or permission error confirms the file-access problem.
   If the file is readable, check that its contents are valid JSON in the
   role-policy schema; unreadable policy content also grants nothing.

3. Compare a request that needs a role grant with one allowed by the tier
   baseline. Loss of the role-only capability with the baseline intact is
   consistent with the empty role policy.

## Recovery

1. Restore the mount and intended override file at `LEAF_ROLES_FILE`, with
   permissions that let the app read it. Preserve the intended grants and
   revocations. Alternatively, if returning to the shipped policy is the
   intended recovery, unset `LEAF_ROLES_FILE` in the app's deployment
   configuration to fall back to the shipped `server/roles.json`.
2. Restart the app after recovery. The policy is read at request time, so a
   repaired file can take effect on the next request, but the app process's
   environment is fixed at boot. An environment change requires a new app
   process; for compose, recreate the app container after removing the
   override from its configuration (`docker compose up -d --force-recreate app`).
3. Repeat the environment and file checks on the replacement app container,
   then repeat a request expected to receive a role grant. Elevated grants
   still require the subject to be on `LEAF_PLATFORM_ADMIN_SUBJECTS`.

## Alert hint

No alarm exists today for roles override mount loss. Consider alerting when
`LEAF_ROLES_FILE` points at a nonexistent or unreadable path, checked from
inside the app container as its runtime user. Also consider monitoring the
role-gated 403s-to-grants ratio for a sudden change: mount loss should reduce
successful role grants and increase denials for requests that depend on
them. These are proposed checks, not existing alarms.
