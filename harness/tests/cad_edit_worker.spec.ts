// CAD-edit worker negative control (Lane C1, card C1-7).
//
// Proves cad_edit OFF never constructs the Worker against the real
// EngineBoundary in web/src/cad/engineWorker.js, using a Worker constructor
// spy. The same boundary constructs a worker once enabled, and defaults
// dormant when no flag is supplied.
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
