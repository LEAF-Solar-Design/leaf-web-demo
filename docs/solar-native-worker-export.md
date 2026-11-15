# Native Solar edit exports

This worker boundary supports S01 production hardening after its accepted Solar layout benchmark. A native frame pose edit must update both DXF geometry and the embedded physical document. A geometrically valid export with stale embedded geometry fails the existing strict server decoder.

`vendor/acadrust-worker/native-layout-bytes.mjs` verifies the parent namespace and checksums, then updates only owned frame coordinates and their embedded vertex tuples. It retains opaque parameters, generation membership, scope and other physical fields. It removes only absent zero vertex width/bulge defaults introduced by the writer and restores canonical owned numeric text. Foreign bytes from the writer output remain exact. This does not claim that the underlying writer retains every foreign input byte.

Resize, reflection, owned attribute changes, pile changes, added managed entities, missing or duplicate handles and metadata changes refuse the edit. The worker rolls back failed native edits. While checksum work awaits, a second edit refuses; a new load or disposal invalidates the old result.

## Replay

From the repository root, with Python server dependencies and Node installed:

```powershell
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_solar_native_layout_dxf.py -q
```

The eight added ordinary cases in the existing native suite generate synthetic inputs through the real Python native producer, invoke the JavaScript repair module, and validate output with the unchanged strict Python decoder. They cover two successive moves, metadata/foreign-byte retention, five strict refusals and generic bypass. Writer lexemes in this suite model the observed padding and zero defaults; these cases do not execute WASM.

Separate retained local evidence executes the actual worker with a source-matched development WASM build: successive moves, an attribute refusal followed by a valid move, a generic drawing, and new-load/dispose interleaves during checksum awaits. Its six cases and three cross-language decoded exports are local runtime proof, not database, browser or LIVE publication proof. Compiled binaries and private evidence paths do not belong in the public repository.

Saving remains a separate server admission contract. A save receipt must bind the exact submitted bytes. This worker change does not rewrite server receipt hashes, relax the native decoder or prove authenticated database publication. Reopen and LIVE checks require a new disposable project on a changed staging source; preserve accepted benchmark projects.
