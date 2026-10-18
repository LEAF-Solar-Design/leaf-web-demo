# S02 useful local Studio sub-tab, slice B

Parent: Leaf Automation Studio Lab and widget designer. The parent outcome is
one shared Lab in both hosts with widget authoring. This slice integrates the
accepted slice A text editor into the existing Studio Browser project board.
It does not establish hosted availability or complete the parent roadmap.

## Exposure and mount

Lab is available only when `import.meta.env.DEV` is true and
`import.meta.env.VITE_LAB_PREVIEW === '1'`. Production stays off even when the
variable is present. URL parameters and local storage cannot opt in.
Browser, CAD, Solar CAD and iOS remain the existing workspace profiles.
Lab is a local panel within the Browser board, with one Lab toggle and one
working Widgets sub-tab. There are no placeholder tabs or new project choices.
The header's existing ProjectSwitcher remains the project-switch control.

`App.jsx` mounts `LabWidgetsPreview` through the existing `PersistentSeat`
from `ProjectWorkspacePanels.jsx`, outside the conditional profile/panel tree.
The seat targets a destination in the Browser board's existing panel flow.
The seat and accepted `WidgetsView` are unkeyed: closing Lab hides the panel,
and leaving Browser detaches its destination without unmounting the editor.
Returning restores the same draft, raw inputs and selection. Scoped CSS uses
the board's normal scrolling and the accepted editor's responsive layout;
Lab adds no header overlay or positional manager.

The editor is consumed from `web/src/lab/widgets/WidgetsView.jsx` without
modifying slice A. Its accepted six-file dependency is PR2039, candidate
`1001547ae78734e4a2e56e09c7ed7498bfbb3159`, integrated by the parent owner.
See [slice A](WIDGET-PREVIEW-SLICE.md) for the text model and preview limits.

## Scope and draft lifetime

`resolveLabPreviewScope` consumes App's existing session, the existing
`composerDraftAccountScope()` namespace, tenant, orgId, openProjectId,
workspace and wsLoading. Editing requires the opt-in, explicit non-mock mode,
an active session, a nonempty non-guest account namespace, a resolved integer
session generation, concrete tenant/organization/project identifiers, finished
loading and a hydrated `workspace.project.project_id` matching the open project.
An emitted `workspace.project.org_id` must also match the current organization;
its absence is supported. No additional producer fields are required.

The scope is an unambiguous JSON tuple of account namespace, session generation,
tenant, organization and project. Profile and panel selections do not affect
it. A/B project drafts restore independently within the mounted session.
Account and session-generation changes select separate draft buckets.
The account namespace partitions drafts; it supplies no authentication or
backend authority. Session/token presence alone cannot enable editing.

Unresolved states pass null to the editor, removing previous draft content and
mutation controls immediately. The panel explains the existing state: sign in,
select an organization, open a project, wait for loading, or resolve a context
mismatch. Offline demo mode cannot create an editable scope. A failed project
open with a null workspace stays unavailable. The existing controller retains
last-good hydration on a transient refresh failure; this slice does not change
that controller policy or introduce an extra readiness signal.

Drafts exist only in mounted React state. Reloading or unmounting App discards
them. This slice adds no localStorage, credentials, API calls, entitlement,
persistence service or publication path.

## Exact replay and remaining proof

From the worktree root, the parent verifier runs:

```sh
npm --prefix web run test:unit -- src/lab/widgets/widgets.test.jsx src/lab/labPreview.test.jsx src/appSessionWiring.test.js
```

The focused Lab tests render the real accepted editor in the existing
PersistentSeat. They exercise open/close, editor identity and selection across
destination detach/reattach, editing and immediate preview, A/B project drafts,
readiness transitions, account/session isolation, production/flag rejection and
public helper context boundaries. They do not mount the full App or establish
browser geometry.

For a local development server, use the existing local API/session setup with:

```sh
VITE_MOCK=0 VITE_LAB_PREVIEW=1 npm --prefix web run dev -- --host 127.0.0.1
```

Open `/app`, select Browser, and use the supported header ProjectSwitcher to
open project A. Open Lab, add two text elements, edit text and alignment, select
the first, and check the immediate preview. Close/reopen Lab and switch through
CAD, Solar CAD and iOS back to Browser; check draft and selection continuity.
Use ProjectSwitcher to open B, create a different draft, and return to A.
Hold project hydration pending or fail the project-open response and confirm
that no prior project draft or mutation controls remain. Repeat with inactive
session, missing account scope, mock mode and mismatched hydration. Change
account/session generation and confirm the previous draft is absent.

The parent owner performs the actual shared-shell browser proof, including
keyboard operation and narrow viewport geometry at 390 CSS pixels: all controls
must fit, the board must scroll normally, and the shared
header and project selector must remain reachable. API-fixture evidence must
be labelled fixture; it is not authenticated-provider or hosted acceptance.
Also check a flag-off development run and a production build with the opt-in
variable present: neither may expose Lab or mount the editor.

The parent owns final visible proof, publication and the pending hosted-audience
F4 decision. Shared Lab across both hosts, composition, persistence, native
output and the broader roadmap remain open. The executor does not run the
verification command or claim those acceptance checks here.
