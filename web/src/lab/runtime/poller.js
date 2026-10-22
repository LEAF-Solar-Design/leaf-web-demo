export const POLL_TIMEOUT_MS = 15000;

export function pollIntervalMs(seconds = 5, minimumSeconds = 5) {
  if (!Number.isFinite(seconds) || !Number.isFinite(minimumSeconds) || minimumSeconds < 0 || minimumSeconds > 300) {
    throw new Error('Test: polling interval refused');
  }
  return Math.max(5, minimumSeconds, Math.min(300, seconds)) * 1000;
}

// A source that ignores abort still owns the flight until it settles. A timeout
// reports failure but never starts a second request beside the abandoned one.
export function createPoller({
  read, onValue = () => {}, onError = () => {}, seconds = 5, minimumSeconds = 5,
  visibility = globalThis.document,
  setTimer = globalThis.setTimeout, clearTimer = globalThis.clearTimeout,
} = {}) {
  if (typeof read !== 'function') throw new Error('Test: read source required');
  const intervalMs = pollIntervalMs(seconds, minimumSeconds);
  let running = false; let disposed = false; let flight = null; let timer = null; let generation = 0;
  const hidden = () => visibility?.hidden === true;
  const cancelTimer = () => { if (timer !== null) clearTimer(timer); timer = null; };
  const notify = (callback, value) => { try { callback(value); } catch { /* Consumers cannot break scheduling. */ } };
  function schedule() {
    cancelTimer();
    if (running && !disposed && !hidden() && !flight) timer = setTimer(() => { timer = null; tick(); }, intervalMs);
  }
  function tick() {
    if (flight) return flight.promise;
    if (disposed || hidden()) return Promise.resolve(undefined);
    cancelTimer();
    const controller = new AbortController();
    const current = { controller, generation, promise: null, timeout: null, timedOut: false, cancel: null };
    flight = current;
    const publish = () => !disposed && !hidden() && generation === current.generation && !current.timedOut;
    let finishTimeout;
    const timeout = new Promise(resolve => { finishTimeout = resolve; });
    current.cancel = () => finishTimeout(undefined);
    current.timeout = setTimer(() => {
      if (!publish()) return;
      current.timedOut = true;
      controller.abort();
      notify(onError, Object.freeze({ code: 'source-timeout' }));
      finishTimeout(undefined);
    }, POLL_TIMEOUT_MS);
    const task = Promise.resolve().then(() => {
      if (controller.signal.aborted) throw new Error('Test: source cancelled');
      return read({ signal: controller.signal });
    }).then(value => {
      if (publish()) { notify(onValue, value); return value; }
      return undefined;
    }, () => {
      if (publish()) notify(onError, Object.freeze({ code: 'source-failed' }));
      return undefined;
    }).finally(() => {
      clearTimer(current.timeout);
      if (flight === current) { flight = null; schedule(); }
    });
    current.promise = Promise.race([task, timeout]);
    return current.promise;
  }
  function pause() {
    generation += 1; cancelTimer();
    if (flight) {
      clearTimer(flight.timeout); flight.controller.abort(); flight.cancel();
    }
  }
  function visibilityChanged() {
    if (hidden()) pause(); else if (running) tick();
  }
  visibility?.addEventListener?.('visibilitychange', visibilityChanged);
  return Object.freeze({
    intervalMs,
    tick,
    start() { if (disposed) throw new Error('Test: poller disposed'); running = true; return tick(); },
    stop() { running = false; pause(); },
    dispose() {
      disposed = true; running = false; pause();
      visibility?.removeEventListener?.('visibilitychange', visibilityChanged);
    },
    get inFlight() { return flight !== null; },
  });
}
