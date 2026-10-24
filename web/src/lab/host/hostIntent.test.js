import { webcrypto } from 'node:crypto';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { emptyGraph } from '../canvas/graphDoc.js';
import { evaluateGraph } from '../runtime/evaluate.js';
import { HOST_INTENT_ID, HOST_INTENT_KINDS, intentManifest } from './hostProvider.js';
import { createHostIntentDispatcher } from './hostIntent.js';

afterEach(() => vi.useRealTimers());
const intent = { kind: 'engine.halt', engine_id: 'engine-1' };
const draft = () => ({ document: emptyGraph(), ready: true, problems: [] });
function harness(options = {}) {
  const calls = [];
  const bridge = { admitted: true, state: null,
    proposal: vi.fn(async value => { calls.push(['proposal', value]); return true; }),
    run: vi.fn(async () => { calls.push(['run']); return true; }),
  };
  const dispatcher = createHostIntentDispatcher({ bridge, framed: true,
    intentsEnabled: () => true, cryptoImpl: webcrypto, ...options });
  return { bridge, calls, dispatcher };
}
const placed = (id = 'intent-1', settings = {}) => ({ id,
  registry: { id: HOST_INTENT_ID, version: '1.0.0', code_sha256: intentManifest().version.code_sha256 },
  settings,
});

describe('explicit host intent clicks', () => {
  it('refuses standalone before inspecting admission or the gate', async () => {
    const gate = vi.fn();
    const bridge = { get admitted() { throw new Error('must not inspect'); } };
    const { dispatcher } = harness({ bridge, framed: false, intentsEnabled: gate });
    expect(await dispatcher.propose(null, null)).toEqual({ ok: false,
      reason: 'Open Lab inside Ops to send a host intent' });
    expect(gate).not.toHaveBeenCalled();
  });

  it.each(['unaccepted handshake', 'wrong principal'])('refuses an %s before the gate', async () => {
    const gate = vi.fn();
    const h = harness({ intentsEnabled: gate });
    h.bridge.admitted = false;
    expect(await h.dispatcher.propose(null, null)).toEqual({ ok: false,
      reason: 'Ops has not accepted this Lab session' });
    expect(gate).not.toHaveBeenCalled();
    expect(h.calls).toEqual([]);
  });

  it('refuses a disabled intent gate before validating the request', async () => {
    const h = harness({ intentsEnabled: () => false });
    expect(await h.dispatcher.propose(null, null)).toEqual({ ok: false, reason: 'Lab intents are off' });
    expect(h.calls).toEqual([]);
  });

  it.each([null, {}, { ...intent, kind: 'engine.delete' }, { ...intent, engine_id: 'Engine' },
    { ...intent, engine_id: '' }, { ...intent, engine_id: 'a'.repeat(33) },
    { ...intent, engine_id: null }, { ...intent, nonce: 'caller-controlled' },
    { ...intent, source: 'live-source' }])('refuses malformed or unsupported intent %j', async value => {
    const h = harness();
    expect(await h.dispatcher.propose(draft(), value)).toEqual({ ok: false,
      reason: 'Host intent is not a supported kind or engine' });
    expect(h.calls).toEqual([]);
  });

  it('refuses multiple host intents without a bridge call', async () => {
    const h = harness();
    const value = draft();
    value.document.blocks = [placed('one'), placed('two')];
    expect(await h.dispatcher.propose(value, intent)).toEqual({ ok: false,
      reason: 'A graph can hold one host intent' });
    expect(h.calls).toEqual([]);
  });

  it.each([null, {}, { document: {} }, { ...draft(), ready: 'yes' }])('refuses malformed draft %j', async value => {
    const h = harness();
    expect(await h.dispatcher.propose(value, intent)).toEqual({ ok: false,
      reason: 'Host intent draft is malformed or secure randomness is unavailable' });
    expect(h.calls).toEqual([]);
  });

  it('fails closed if secure randomness is unavailable', async () => {
    const h = harness({ cryptoImpl: null });
    expect(await h.dispatcher.propose(draft(), intent)).toMatchObject({ ok: false });
    expect(h.calls).toEqual([]);
  });

  it.each(HOST_INTENT_KINDS)('proposes one pinned block then runs once for %s', async kind => {
    const h = harness();
    const original = draft();
    const result = await h.dispatcher.propose(original, { ...intent, kind });
    expect(result).toEqual({ ok: true, nonce: expect.stringMatching(/^[0-9a-f]{24}$/) });
    expect(h.calls.map(([name]) => name)).toEqual(['proposal', 'run']);
    expect(h.bridge.proposal).toHaveBeenCalledTimes(1);
    expect(h.bridge.run).toHaveBeenCalledTimes(1);
    expect(h.calls[0][1]).toEqual({ ...original, document: { ...original.document, blocks: [{
      id: 'host-intent-1', registry: { id: HOST_INTENT_ID, version: '1.0.0',
        code_sha256: intentManifest().version.code_sha256 },
      settings: { kind, engine_id: intent.engine_id, nonce: result.nonce },
    }] } });
    expect(original).toEqual(draft());
  });

  it('replaces existing settings, retains the block id and mints fresh nonces', async () => {
    const h = harness();
    const original = draft();
    original.document.blocks = [placed('existing', { kind: 'engine.probe', engine_id: 'old', nonce: 'old' })];
    const first = await h.dispatcher.propose(original, intent);
    const second = await h.dispatcher.propose(original, intent);
    expect(first).toEqual({ ok: true, nonce: expect.stringMatching(/^[0-9a-f]{24}$/) });
    expect(second).toEqual({ ok: true, nonce: expect.stringMatching(/^[0-9a-f]{24}$/) });
    expect(first.nonce).not.toBe(second.nonce);
    for (const [, value] of h.calls.filter(([name]) => name === 'proposal')) {
      expect(value.document.blocks).toHaveLength(1);
      expect(value.document.blocks[0]).toMatchObject({ id: 'existing', settings: intent });
      expect(Object.keys(value.document.blocks[0].settings).sort()).toEqual(['engine_id', 'kind', 'nonce']);
    }
    expect(original.document.blocks[0].settings.nonce).toBe('old');
  });

  it('awaits proposal success before sending the signed request', async () => {
    const h = harness();
    let accept;
    h.bridge.proposal.mockImplementation(() => new Promise(resolve => { accept = resolve; }));
    const pending = h.dispatcher.propose(draft(), intent);
    expect(h.bridge.run).not.toHaveBeenCalled();
    accept(true);
    expect(await pending).toMatchObject({ ok: true });
    expect(h.bridge.run).toHaveBeenCalledTimes(1);
  });

  it('never runs a refused proposal', async () => {
    const h = harness();
    h.bridge.proposal.mockResolvedValue(false);
    expect(await h.dispatcher.propose(draft(), intent)).toEqual({ ok: false, reason: 'Ops did not take the proposal' });
    expect(h.bridge.proposal).toHaveBeenCalledTimes(1);
    expect(h.bridge.run).not.toHaveBeenCalled();
  });

  it('returns the wire-size refusal without running', async () => {
    const h = harness();
    h.bridge.proposal.mockImplementation(async () => {
      h.bridge.state = { status: 'too_large', bytes: 5000 };
      return false;
    });
    expect(await h.dispatcher.propose(draft(), intent)).toEqual({ ok: false,
      reason: 'proposal exceeds the 4096-byte wire limit (5000 bytes)' });
    expect(h.bridge.run).not.toHaveBeenCalled();
  });

  it('reports a refused run without retrying', async () => {
    const h = harness();
    h.bridge.run.mockResolvedValue(false);
    expect(await h.dispatcher.propose(draft(), intent)).toEqual({ ok: false, reason: 'Ops did not take the proposal' });
    expect(h.bridge.proposal).toHaveBeenCalledTimes(1);
    expect(h.bridge.run).toHaveBeenCalledTimes(1);
  });

  it('schedules no timer at construction, after a click, or after refusal', async () => {
    vi.useFakeTimers();
    const h = harness();
    expect(vi.getTimerCount()).toBe(0);
    expect(h.calls).toEqual([]);
    await h.dispatcher.propose(draft(), intent);
    await h.dispatcher.propose(draft(), { ...intent, kind: 'unknown' });
    expect(vi.getTimerCount()).toBe(0);
    vi.runAllTimers();
    expect(h.calls.map(([name]) => name)).toEqual(['proposal', 'run']);
  });
});

describe('settings-only host intent block', () => {
  it('has no inputs, one ready output and a closed schema with exactly the trusted intent kinds', () => {
    const manifest = intentManifest();
    expect(manifest).toMatchObject({ id: HOST_INTENT_ID, kind: 'settings', effect: 'pure',
      lane: 'lifecycle', residency: 'tenant', execution_target: 'runner', permissions: ['host.intent'],
      inputs: {}, outputs: { ready: { schema: { type: 'boolean' } } },
      sample_input: {}, sample_output: { ready: true },
      settings_schema: { type: 'object', additionalProperties: false,
        required: ['kind', 'engine_id', 'nonce'], properties: {
          kind: { type: 'string', enum: ['engine.halt', 'engine.probe', 'engine.assign'] },
          engine_id: { type: 'string', minLength: 1, maxLength: 32 },
          nonce: { type: 'string', minLength: 16, maxLength: 64 },
        } },
    });
    expect(Object.keys(manifest.inputs)).toEqual([]);
    expect(Object.keys(manifest.outputs)).toEqual(['ready']);
    expect(Object.isFrozen(HOST_INTENT_KINDS)).toBe(true);
    expect(Object.isFrozen(manifest)).toBe(true);
  });

  it('refuses browser execution on every rerun without proposing, running or scheduling timers', () => {
    vi.useFakeTimers();
    const h = harness();
    const entries = { 'host-intent@1.0.0': intentManifest() };
    const document = { ...emptyGraph(), blocks: [placed('intent', {
      ...intent, nonce: 'a'.repeat(24),
    })] };
    for (let i = 0; i < 2; i += 1) {
      const result = evaluateGraph(document, entries);
      expect(result.values.intent.ready).toMatchObject({
        data: null, error: { code: 'browser-execution-refused' },
      });
      expect(result.outputs).toEqual({});
    }
    expect(h.calls).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  });
});
