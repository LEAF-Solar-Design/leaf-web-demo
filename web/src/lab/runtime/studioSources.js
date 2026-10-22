import { runtimeValue } from './evaluate.js';
import { createPoller } from './poller.js';

export const STUDIO_SOURCES = Object.freeze({
  'studio-jobs': Object.freeze({ path: '/api/jobs', key: 'jobs', minimumSeconds: 5 }),
  'studio-capabilities': Object.freeze({ path: '/api/capabilities', key: 'families', minimumSeconds: 30 }),
});

// Inject the host's authenticated fetch adapter. No credentials, host payloads,
// response text or fallback fixtures are written to documents or telemetry.
export function createStudioSources({ fetchImpl = globalThis.fetch, headers = {}, now = Date.now } = {}) {
  async function read(id, { signal } = {}) {
    if (!Object.prototype.hasOwnProperty.call(STUDIO_SOURCES, id)) throw new Error('Test: source unavailable');
    const source = STUDIO_SOURCES[id];
    try {
      const response = await fetchImpl(source.path, { method: 'GET', credentials: 'same-origin', headers, signal, redirect: 'error' });
      if (!response.ok) throw new Error('Test: source request failed');
      const payload = await response.json();
      if (!payload || !Array.isArray(payload[source.key])) throw new Error('Test: source response refused');
      // Receipt time is deliberately not an as_of fallback. An endpoint without
      // a source timestamp has unknown age even after a fast successful request.
      const asOf = payload.as_of ?? null;
      const age = asOf === null ? null : Math.max(0, now() - asOf);
      return runtimeValue(payload[source.key], {
        as_of: asOf, host_tainted: true, simulated: payload.simulated === true,
        stale: payload.stale === true || (age !== null && age > source.minimumSeconds * 1000),
        truncated: payload.truncated === true || payload.has_more === true,
      });
    } catch {
      return runtimeValue(null, { host_tainted: true, error: { code: 'source-failed' } });
    }
  }
  return Object.freeze({
    read,
    jobs: options => read('studio-jobs', options),
    capabilities: options => read('studio-capabilities', options),
    poll(id, options = {}) {
      if (!Object.prototype.hasOwnProperty.call(STUDIO_SOURCES, id)) throw new Error('Test: source unavailable');
      return createPoller({ ...options, minimumSeconds: STUDIO_SOURCES[id].minimumSeconds, read: context => read(id, context) });
    },
  });
}
