# Project import protects native edits

The Solar editor imports Open DXF files into its current project. That import can create and select another canonical drawing before native parsing completes. An unsaved native edit must be resolved before that handoff.

The session now refuses project import before its API callback while a native edit is pending or its current bytes differ from the committed bytes. Pending edits keep their busy state until their actual reply. Unsaved edits keep their document, edited download, projection and Undo/Redo history. The user can save or undo the edits before trying another import. Download keeps a separate copy and does not mark the edit saved.

Empty and clean projects still import normally. Undoing to the clean floor permits import again. This guard does not validate the server importer or change immutable version selection.

From web, run `npx vitest run src/cadedit/solarProjectImportEdits.test.jsx src/cadedit/projectDrawingEditor.test.jsx src/cadedit/solarAtomicOpen.test.jsx src/cadedit/solarWorkerReply.test.jsx`. Public scripted worker fixtures model project handoff and byte-exact history. The regression comes from a fresh disposable LIVE project on source96249e02, where a truncated import returned201 and native parsing then failed. The edited DXF was retained, checkout released and browser closed. A changed-source readiness check remains required after delivery.
