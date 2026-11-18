# Staging investor demo link

Leaf Automation uses this link to show the bundled rooftop drawing in a view-only
Studio workspace. Every visitor uses the same fixed read-only subject and demo
org on staging. Anyone holding the link can open another session until it expires.
The identity and route contract is in [AUTH.md, section 12](../../contract/AUTH.md#12-investor-demo-identity-staging-only).

## First-time setup

1. Use the staging AWS account and a Python environment with boto3. From the
   repository root, create the dedicated SSM SecureString.

   ```sh
   python scripts/investor_demo_link.py put-secret --region us-east-1
   ```

   Git Bash on Windows rewrites an argument beginning with `/` into a file
   path, so either omit `--parameter` or set `MSYS_NO_PATHCONV=1`.

2. In the staging Terraform configuration, mirror the existing
   `service-browser-session-key` wiring in `leaf_platform.tf`. Set
   `LEAF_INVESTOR_DEMO_ENABLED=1` in both app environment lists. Add
   `LEAF_INVESTOR_DEMO_SECRET` to both secrets lists with the SSM parameter ARN
   as its source. Grant `ssm:GetParameters` on that ARN to the app execution
   role. Confirm `LEAF_RUNTIME_ENV=staging` and `LEAF_AUTH_LIVE=1`.
3. Apply the staging change and deliver the server and web build. Replace the
   running tasks so all processes receive the new flag and secret.
4. Confirm startup logs show `diagnostic_code=investor_demo_ready`. A mismatch
   or unavailable workspace requires checking the demo org and its binding
   before distributing links.

Clients choose their Host header, so the Host check cannot protect production.
Production relies on the disabled flag, the exact `LEAF_RUNTIME_ENV=staging`
and `LEAF_AUTH_LIVE=1` requirements, and the absence of the investor secret.

## Mint and check

Use a private directory outside every git working tree and not in a synced folder
(OneDrive, Dropbox). On Windows, the tool restricts the link file to the
current user before writing the link. This shell example chooses a UTC date
seven days from today. The tool creates a new file and prints its path, key ID
and expiry. It refuses to overwrite an existing file.

```sh
mkdir -p -m 700 "$HOME/.leaf-investor"
expiry=$(python -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc).date()+timedelta(days=7)).isoformat())')
python scripts/investor_demo_link.py mint --expires "$expiry" --out "$HOME/.leaf-investor/staging-link.txt"
python scripts/investor_demo_link.py check --file "$HOME/.leaf-investor/staging-link.txt"
```

`check` verifies the token locally against the current SSM value. It prints
`valid until` with the UTC expiry (exit 0), `invalid` (exit 1), or
`cannot check: secret unavailable` (exit 2). For exit 2, restore access to
SSM and retry. The staging walk also checks the deployed app.
Share the link privately with its intended visitors. Keep it out of tickets,
repositories, screen recordings and command lines.

Mail gateways that rewrite or pre-open links, including Safe Links, Proofpoint
and Mimecast, copy the whole URL including its fragment into their logs and may
open it. The link is a shared, view-only credential by design. Rotate the
secret if a gateway has retained a link that needs to be revoked.

## Visitor experience and limits

On a full page load the console removes the fragment and exchanges the link
for a session. Pasting the link into an open tab on the same page forces a
full page load. Existing human and service sessions take precedence.
Visitors can inspect the sample
drawing, catalog and demo workspace. Tool runs, uploads, edits, builds and
access to other drawings or orgs are refused. Sign-out ends the local visit.
Sessions last up to two hours, bounded by link expiry. Links expire at
23:59:59 UTC on the selected date, at most 31 days from mint time.

## Revoke access

* Rotate with `python scripts/investor_demo_link.py put-secret --rotate` and
  replace every staging app task to load the new SSM version. Each updated task
  refuses old links and live sessions signed with the previous key.
  Wait until every task has been replaced before minting a link or running
  the walk. During a rolling replacement, old and new tasks use different
  keys, so fresh links fail at random depending on which task receives them.
* Set the enable flag to `0` in both lists and replace all tasks. Each updated
  task refuses exchanges and previously issued sessions while disabled.
* Set the binding to `revoked` or the org to a non-active status to refuse new
  exchanges and the next authenticated read from each live session. Reactivation
  can restore unexpired credentials. Deleting the rows does not revoke access
  durably because startup inserts missing rows again.

## Staging walk

```sh
LEAF_E2E_INVESTOR_LINK_FILE="$HOME/.leaf-investor/staging-link.txt" npm --prefix web run proof:staging -- investor-link.spec.mjs
```

Use a fresh link and the fixed staging origin. The report attaches only the
paths of forbidden API responses during boot. Each walk makes three exchanges.
Repeated walks can hit the per-client limit of 10 exchanges per minute per
address. Individual link holders cannot be revoked separately.
