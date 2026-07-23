import { describe, expect, it, vi } from "vitest";

import { createShutdownHandler } from "../src/shutdown.js";

function fixture() {
  let closeCallback: (() => void) | undefined;
  let forcedCallback: (() => void) | undefined;
  let clock = 1_000;
  const server = {
    close: vi.fn((callback: () => void) => {
      closeCallback = callback;
      return server;
    }),
    closeAllConnections: vi.fn(),
  };
  const log = vi.fn();
  const exit = vi.fn();
  const cleanup = vi.fn();
  const shutdown = createShutdownHandler({
    server,
    log,
    exit,
    cleanup,
    now: () => clock,
    forceAfterMs: 25_000,
    schedule: (callback, delayMs) => {
      expect(delayMs).toBe(25_000);
      forcedCallback = callback;
      return { unref: vi.fn() };
    },
  });
  return {
    server,
    log,
    exit,
    cleanup,
    shutdown,
    advance: (milliseconds: number) => {
      clock += milliseconds;
    },
    close: () => closeCallback?.(),
    force: () => forcedCallback?.(),
  };
}

describe("harness shutdown contract", () => {
  it("exits zero after a graceful close and records elapsed time", () => {
    const f = fixture();
    f.shutdown("SIGTERM");
    f.advance(125);
    f.close();

    expect(f.server.close).toHaveBeenCalledOnce();
    expect(f.server.closeAllConnections).not.toHaveBeenCalled();
    expect(f.log).toHaveBeenCalledWith("[harness] shutdown complete elapsed_ms=125");
    expect(f.cleanup).toHaveBeenCalledOnce();
    expect(f.exit).toHaveBeenCalledWith(0);
  });

  it("closes all connections, records elapsed time, and exits nonzero on fallback", () => {
    const f = fixture();
    f.shutdown("SIGTERM");
    f.advance(25_000);
    f.force();

    expect(f.server.closeAllConnections).toHaveBeenCalledOnce();
    expect(f.log).toHaveBeenCalledWith(
      "[harness] shutdown forced fallback elapsed_ms=25000 limit_ms=25000 exit_code=1",
    );
    expect(f.cleanup).toHaveBeenCalledOnce();
    expect(f.exit).toHaveBeenCalledWith(1);
    f.close();
    expect(f.exit).toHaveBeenCalledTimes(1);
  });

  it("does not install a second shutdown path for a repeated signal", () => {
    const f = fixture();
    f.shutdown("SIGTERM");
    f.shutdown("SIGINT");

    expect(f.server.close).toHaveBeenCalledOnce();
    expect(f.log).toHaveBeenCalledWith("[harness] SIGINT -> shutdown already in progress");
  });
});
