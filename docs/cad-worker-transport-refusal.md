# CAD worker transport refusal

This boundary supports S01 production hardening for browser-editor load and edit recovery. Native `Worker.postMessage` can throw before delivering a message, including for an uncloneable payload or detached buffer. The editor already handles a false boundary result, but an escaping exception bypassed that cleanup and left busy set.

`EngineBoundary.post` now returns false and records a bounded `transport_post_failed` receipt if native posting throws. It retains no exception text or raw payload. Existing session callers report transport refusal and settle pending state; the same worker can accept a later valid message. Schema checks, worker ownership and protocol shapes remain unchanged.

From `web`, run:

```sh
npx vitest run src/cad/engineBoundary.test.js
```

All seventeen cases pass. Two added ordinary cases execute real Node worker threads, trigger native cloning failures, check the safe receipt and verify only the next valid message reaches the worker. Both fail on unchanged source. Threads terminate in `finally`; no WASM package or optional skip is involved.

Separate retained local evidence injects uncloneable payload and detached-transfer faults into the actual Node worker/WASM session. Both settle busy with typed transport refusal and then load/edit successfully. The compiled engine matches its retained source hashes. This is local runtime proof, not browser, HTTP, database or LIVE verification.

On this Windows checkout, `D:/GitHub` resolves to `C:/GitHub`. Run through the resolved path so the import fence compares the real source file with itself. A copied test with a different self-path does not provide that static compatibility proof. No existing assertion is changed to accommodate the runner.
