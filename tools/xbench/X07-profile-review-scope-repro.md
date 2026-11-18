# Scoped profile review replay

Parent: Studio X-bench X07 drawing role discovery and reusable customer profiles.
This injected composition is offline. Authenticated mounting and seven-step
staging acceptance remain separate work.

From the repository root, with existing web dependencies:

```powershell
node tools/xbench/tests/x07_profile_review_focus_dom.cjs --deps web/node_modules
node tools/xbench/tests/x07_profile_review_client_aba_dom.cjs --deps web/node_modules
node tools/xbench/tests/x07_role_review_adapter_journey.cjs --deps web/node_modules
```

Run each bounded fixture separately. The focus fixture sends keyboard events
and explicitly supplies jsdom's missing default button activation. It expects
one confirmation, focus on Retry loading profile after a generic load refusal,
then focus on Module layer after explicit retry. Every described-by and
labelled-by reference resolves. Native Enter is verified in the browser receipt.

The client fixture switches A to B to A during a deferred load, after a settled
confirmation, and during a save. Old operations are aborted. No old editor or
save success returns, and a new confirmation requires an explicit action.
The Vitest profile-review suite also switches drawing A to B to A with an
unchanged binding and checks missing, malformed and foreign project pins.
Invalid receipts show only the fixed refusal, retain the choice and never load
or save a profile. A correct receipt can be retried explicitly.

The real adapter's project pin supplies customer, project, profile, safe version
and pinned roles. Its workflow result supplies drawing identity. The wrapper
checks both against the current caller, including a binding drawing if supplied.
It does not add a drawing field to the store or infer customer identity.

The adapter journey confirms customer A, edits ROOF into current version 2 and
keeps pinned version 1 and its original Panel/MODULES roles visible. It reopens
two later A drawings, then chooses one copy for B. Independent SQLite checks
retain old pins and immutable versions. Summary values are copied from the
confirmed pin, so later callbacks or edits cannot rewrite the display.

Each fixture closes its owned DOM, child processes and disposable stores in
finally. No customer drawing, network, shared route or deployment is involved.
