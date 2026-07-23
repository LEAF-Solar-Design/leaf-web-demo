export interface ShutdownServer {
  close(callback: () => void): unknown;
  closeAllConnections(): void;
}

export interface ShutdownOptions {
  server: ShutdownServer;
  log: (message: string) => void;
  exit: (code: number) => void;
  cleanup?: () => void;
  now?: () => number;
  schedule?: (callback: () => void, delayMs: number) => { unref(): unknown };
  forceAfterMs?: number;
}

export function createShutdownHandler(options: ShutdownOptions): (signal: string) => void {
  const now = options.now ?? Date.now;
  const schedule = options.schedule ?? ((callback, delayMs) => setTimeout(callback, delayMs));
  const forceAfterMs = options.forceAfterMs ?? 25_000;
  let stopping = false;
  let finished = false;

  return (signal: string): void => {
    if (stopping) {
      options.log(`[harness] ${signal} -> shutdown already in progress`);
      return;
    }
    stopping = true;
    const started = now();
    options.log(`[harness] ${signal} -> closing`);
    options.server.close(() => {
      if (finished) return;
      finished = true;
      const elapsedMs = Math.max(0, now() - started);
      options.log(`[harness] shutdown complete elapsed_ms=${elapsedMs}`);
      options.cleanup?.();
      options.exit(0);
    });
    schedule(() => {
      if (finished) return;
      finished = true;
      const elapsedMs = Math.max(0, now() - started);
      options.log(
        `[harness] shutdown forced fallback elapsed_ms=${elapsedMs} ` +
          `limit_ms=${forceAfterMs} exit_code=1`,
      );
      options.server.closeAllConnections();
      options.cleanup?.();
      options.exit(1);
    }, forceAfterMs).unref();
  };
}
