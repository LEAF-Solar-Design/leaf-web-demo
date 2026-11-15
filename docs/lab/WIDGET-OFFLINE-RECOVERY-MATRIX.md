# Widget offline recovery matrix

Studio18 L01 widget authoring uses the same project document store as the Lab workspace. These cases describe recovery for a draft, its saved revision and its downloaded definition.

| Failure | Visible result | Explicit recovery | Executable evidence | Proof level |
|---|---|---|---|---|
| Save times out before a late acknowledgment | Request failed; local edits remain. The late acknowledgment does not advance the displayed head. | Save again. If the first request committed, resolve the conflict with Reload before editing the stored revision. | web/src/lab/widgets/requestTimeout.test.jsx | Mocked client and rendered UI |
| Save or Load succeeds without a valid revision | Request failed; local draft and prior head remain. | Retry explicitly. A committed save can cause a conflict; Reload retrieves its valid revision. | web/src/lab/widgets/saveReceipt.test.jsx; desktop/L01-delivered-recovery-20261114T215002Z | Mocked UI plus bounded real-service browser response fault |
| HTTP200 contains null, an array or unreadable JSON | Request failed; no document or revision is invented. | Save or Load explicitly after the store returns a readable object. | web/src/lab/widgets/unreadableResponse.test.jsx | Mocked client and rendered UI |
| Prior project's Load resolves after switching projects | The current project draft remains. | Continue editing or save in the current project. | web/src/lab/widgets/navigationRace.test.jsx | Mocked rendered UI |
| Loaded text violates the document contract | Invalid widget; prior draft remains; Save and export are unavailable. | Load a valid stored revision explicitly, or choose New to start a new document. | web/src/lab/widgets/textImportRecovery.test.jsx | Mocked rendered UI,3 tests passed |
| Literal text contains newlines or exceeds the short field | Full text editor opens for loaded values; exact text remains document data. | Edit in Full text. Use short text only when it fits one line and 40 UTF-16 units. | web/src/lab/widgets/unicodeEditing.test.jsx; web/e2e/widgets/widget-unicode.case.mjs | Component tests and production-browser fixture; source PR3043, not yet LIVE |

Tests intercept only their own injected client or fixture endpoints. A fixture pass proves the browser path and downloaded JSON, not deployed service health, authorization or shared staging state. The service proof uses a new disposable project, modifies one response in its own browser and leaves the real store write intact. Original accepted evidence stays unchanged.

No path retries a Save automatically. A timeout can mean that the write committed but its acknowledgment was lost. Do not infer rollback from the error or replace a local draft with an unvalidated response. Downloading a definition does not save it to the project.
