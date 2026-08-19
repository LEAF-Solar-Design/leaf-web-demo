// CAD-edit worker negative control (Lane C1, card C1-7).
//
// The upload -> version receipt -> read round-trip E2E and the cad_upload
// OFF negative control now live where the real surface actually is:
// server/tests/test_cad_upload_e2e.py and server/tests/test_cad_fence.py,
// driving the REAL server/routers/cad_upload.py through a TestClient. That
// route has nothing to do with the isolated-worker primitive this file used
// to exercise -- it is a plain synchronous multipart handler.
//
// What genuinely belongs here: "cad_edit OFF never mounts the worker" is a
// JS-side claim (cad_edit has no server route at all -- C1-5/C1-6 landed it
// entirely client-side as web/src/cad/engineWorker.js's EngineBoundary). This
// spec proves that claim against the REAL EngineBoundary with a real
// `Worker` constructor spy, not a toy re-implementation of the boundary.
import { afterEach, describe, expect, it, vi } from "vitest";

import { EngineBoundary } from "../../web/src/cad/engineWorker.js";

describe("cad_edit OFF: worker negative control (real EngineBoundary)", () => {
  afterEach(() => {
    delete (globalThis as any).Worker;
  });

  it("never touches the real Worker constructor when cad_edit is off", () => {
    const workerCtor = vi.fn();
    (globalThis as any).Worker = workerCtor;

    const boundary = new EngineBoundary({ flags: { cad_edit: false } });
    const started = boundary.start();

    expect(started).toBe(false);
    expect(boundary.instantiated).toBe(false);
    expect(workerCtor).not.toHaveBeenCalled();
  });

  it("flip-time proof: the SAME boundary instantiates once flipped on, in the same process", () => {
    const workerCtor = vi.fn(function FakeWorker(this: any) {
      this.addEventListener = vi.fn();
      this.postMessage = vi.fn();
      this.terminate = vi.fn();
    });
    (globalThis as any).Worker = workerCtor;

    const offBoundary = new EngineBoundary({ flags: { cad_edit: false } });
    expect(offBoundary.start()).toBe(false);
    expect(workerCtor).not.toHaveBeenCalled();

    const onBoundary = new EngineBoundary({ flags: { cad_edit: true } });
    expect(onBoundary.start()).toBe(true);
    expect(onBoundary.instantiated).toBe(true);
    expect(workerCtor).toHaveBeenCalledTimes(1);
  });

  it("defaults dormant with no flag at all: no Worker global access", () => {
    const workerCtor = vi.fn();
    (globalThis as any).Worker = workerCtor;

    const boundary = new EngineBoundary({});
    expect(boundary.start()).toBe(false);
    expect(workerCtor).not.toHaveBeenCalled();
  });
});
