import { canonicalJSON } from '../contract.js';
import { registry, resolveManifest } from '../canvas/registry.js';
import { runtimeValue, sourceAge } from '../runtime/evaluate.js';

export const HOST_READ_IDS = Object.freeze(['host-source', 'studio-jobs', 'studio-capabilities']);
const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const plain = value => value !== null && typeof value === 'object'
  && [Object.prototype, null].includes(Object.getPrototypeOf(value));
const freeze = value => {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
};
const pin = (schema, nullable = false) => ({
  mode: 'scalar', schema, unit: 'none', nullable, required: true, has_default: false,
});
const primitiveSchema = () => ({ type: ['string', 'number', 'boolean', 'null'] });
export const isSourceName = value => typeof value === 'string' && /^[a-z][a-z0-9-]{0,31}$/.test(value);

// Schemas contain observed kinds and field names, never observed values.
function observedSchema(values) {
  if (!values.length) return primitiveSchema();
  const kinds = [...new Set(values.map(value => value === null ? 'null'
    : Array.isArray(value) ? 'array' : typeof value))].sort();
  const schema = { type: kinds.length === 1 && ['object', 'array'].includes(kinds[0]) ? kinds[0] : kinds };
  if (kinds.includes('object')) {
    const objects = values.filter(plain);
    const keys = [...new Set(objects.flatMap(Object.keys))].sort();
    schema.additionalProperties = false;
    schema.properties = Object.fromEntries(keys.map(key => [key,
      observedSchema(objects.filter(value => own(value, key)).map(value => value[key])),
    ]));
  }
  if (kinds.includes('array')) schema.items = observedSchema(values.filter(Array.isArray).flat());
  return schema;
}
const emptyRowSchema = () => ({ type: 'object', additionalProperties: false, properties: {} });

// A synchronous registry fingerprint, not a cryptographic integrity claim.
export function fingerprint(manifest) {
  const settings = manifest.settings_schema;
  const settingsSchema = manifest.id === 'host-source' && settings.properties?.source ? { ...settings, properties: {
    ...settings.properties, source: { ...settings.properties.source, enum: ['*'] },
  } } : settings;
  const text = canonicalJSON({ id: manifest.id, semver: manifest.version.semver,
    settings_schema: settingsSchema });
  let hash = 0x811c9dc5;
  for (const byte of new TextEncoder().encode(text)) hash = Math.imul(hash ^ byte, 0x01000193) >>> 0;
  return hash.toString(16).padStart(8, '0').repeat(8);
}

function manifest(fields) {
  const value = {
    schema_version: 1, profile: 'lab-registry', version: { semver: '1.0.0' },
    execution_target: 'runner', residency: 'tenant', permissions: [], ui_schema: {},
    latency_budget_us: { profile: 'legacy-us-to-std4-ns-ms-v1', p50: 1000, p99: 5000 },
    state: 'stateless', tests: ['sample'], ...fields,
  };
  value.version.code_sha256 = fingerprint(value);
  return freeze(value);
}

export function readManifest(id, title, { sourceNames = [], rowSchema = emptyRowSchema() } = {}) {
  if (!HOST_READ_IDS.includes(id) || (id === 'host-source'
    && (!Array.isArray(sourceNames) || !sourceNames.length || !sourceNames.every(isSourceName)))) {
    throw new Error('Test: source unavailable');
  }
  return manifest({ id, title, kind: 'settings', effect: 'read', lane: 'observation', inputs: {},
    outputs: { rows: pin({ type: 'array', items: rowSchema }) },
    settings_schema: { type: 'object', additionalProperties: false,
      ...(id === 'host-source' ? { required: ['source'], properties: {
        source: { type: 'string', enum: [...new Set(sourceNames)].sort() },
      } } : { properties: {} }) },
    sample_input: {}, sample_output: { rows: [] },
  });
}

// Field and select outputs and filter comparisons are limited to primitives.
export function transformManifests(rowSchema = emptyRowSchema()) {
  const rowArraySchema = { type: 'array', items: rowSchema };
  const titles = { count: 'Count', latest: 'Latest', select: 'Pick field', filter: 'Filter',
    sort: 'Sort', take: 'Take', sum: 'Sum', mean: 'Average', min: 'Smallest', max: 'Largest', field: 'Field' };
  return freeze(Object.fromEntries(Object.entries(titles).map(([id, title]) => {
    const properties = {};
    const required = [];
    if (['select', 'field', 'sort', 'filter', 'sum', 'mean', 'min', 'max'].includes(id)) {
      properties.field = { type: 'string' };
      if (['select', 'field', 'sort', 'filter'].includes(id)) required.push('field');
    }
    if (id === 'filter') {
      properties.comparison = { type: 'string', enum: ['eq', 'ne', 'gt', 'gte', 'lt', 'lte'] };
      properties.value = { type: ['null', 'string', 'number', 'boolean'] };
      required.push('comparison', 'value');
    }
    if (id === 'sort') properties.direction = { type: 'string', enum: ['asc', 'desc'] };
    if (id === 'take') properties.count = { type: 'integer', minimum: 0, maximum: 5000 };
    const nullable = ['mean', 'min', 'max', 'latest', 'field'].includes(id);
    const outputSchema = id === 'select' ? { type: 'array', items: primitiveSchema() }
      : id === 'field' ? primitiveSchema()
      : id === 'latest' ? rowSchema : ['filter', 'sort', 'take'].includes(id) ? rowArraySchema : { type: 'number' };
    const value = manifest({ id, title, kind: 'builtin', effect: 'pure', lane: 'processing',
      inputs: { value: pin(id === 'field' ? rowSchema : rowArraySchema, id === 'field') },
      outputs: { result: pin(outputSchema, nullable) },
      settings_schema: { type: 'object', additionalProperties: false, properties, ...(required.length ? { required } : {}) },
      sample_input: { value: id === 'field' ? {} : [] },
      sample_output: { result: nullable ? null : outputSchema.type === 'array' ? [] : 0 },
    });
    return [`${id}@1.0.0`, value];
  })));
}

export function createHostProvider({ win = globalThis.window, framed = Boolean(win) && win.parent !== win,
  now = Date.now, setTimer = globalThis.setTimeout, clearTimer = globalThis.clearTimeout,
  timeoutMs = 2000, operator = false, samplePack = null, onChange = () => {} } = {}) {
  const stored = new Map();
  const studio = new Map();
  const sampled = new Set();
  const rowSchema = () => {
    const rows = [...stored.values()].flatMap(values => values.map(value => value.data));
    for (const value of studio.values()) rows.push(...(Array.isArray(value.data) ? value.data : [value.data]));
    if (operator === true && samplePack) {
      for (const id of ['studio-jobs', 'studio-capabilities']) {
        if (!studio.has(id) && own(samplePack.rows || {}, id)) rows.push(...samplePack.rows[id]);
      }
    }
    const objects = rows.filter(plain);
    return objects.length ? observedSchema(objects) : emptyRowSchema();
  };
  let announced = false;
  let receivedAt = null;
  let reason = null;
  let dropped = 0;
  let started = false;
  let timer = null;
  const state = () => Object.freeze({ framed, announced, reason,
    sources: Object.freeze([...stored.keys()].sort()), receivedAt, dropped, sample: sampled.size > 0 });
  const clear = () => {
    if (timer !== null) clearTimer(timer);
    timer = null;
  };
  const entries = () => {
    const schema = rowSchema();
    return Object.freeze({ ...transformManifests(schema),
      ...(framed ? announced ? { 'host-source@1.0.0': readManifest('host-source', 'Dashboard source', {
        sourceNames: [...stored.keys()], rowSchema: schema,
      }) } : {} : {
        'studio-jobs@1.0.0': readManifest('studio-jobs', 'Jobs', { rowSchema: schema }),
        'studio-capabilities@1.0.0': readManifest('studio-capabilities', 'Capabilities', { rowSchema: schema }),
      }),
    });
  };
  return Object.freeze({
    get state() { return state(); },
    start() {
      if (started) return;
      started = true;
      if (framed && !announced) timer = setTimer(() => {
        timer = null;
        if (!announced) { reason = 'Sample'; onChange(state()); }
      }, timeoutMs);
    },
    stop() { clear(); started = false; },
    announce(rows) {
      const groups = new Map();
      for (const row of Array.isArray(rows) ? rows : []) {
        if (!row || !isSourceName(row.source) || !plain(row.data)) {
          dropped += 1; continue;
        }
        let copy;
        try {
          // runtimeValue makes a deeply frozen, JSON-only snapshot. Handles are
          // unnecessary for evaluation and never retained by this provider.
          copy = runtimeValue(row.data, { as_of: Number.isSafeInteger(row.as_of) && row.as_of >= 0 ? row.as_of : null,
            simulated: row.simulated, stale: row.stale, truncated: row.truncated });
        } catch { dropped += 1; continue; }
        if (!groups.has(row.source)) groups.set(row.source, []);
        groups.get(row.source).push(copy);
      }
      for (const [name, values] of groups) stored.set(name, Object.freeze(values));
      receivedAt = now();
      if ([...stored.values()].some(values => values.length > 0)) {
        announced = true; reason = null; clear();
      }
      onChange(state());
    },
    entries,
    sources(document, suppliedEntries) {
      const merged = suppliedEntries ?? { ...registry, ...entries() };
      const values = [];
      sampled.clear();
      for (const block of document.blocks) {
        const { manifest: resolved, problem } = resolveManifest(block, merged);
        if (problem || resolved.effect !== 'read' || !HOST_READ_IDS.includes(resolved.id)) continue;
        let value;
        if (resolved.id === 'host-source') {
          const rows = stored.get(block.settings.source);
          if (!rows) continue;
          value = runtimeValue(rows.map(row => row.data), { host_tainted: true,
            as_of: rows.length && rows.every(row => row.as_of !== null) ? Math.min(...rows.map(row => row.as_of)) : null,
            simulated: rows.some(row => row.simulated), stale: rows.some(row => row.stale),
            truncated: rows.length >= 200 || rows.some(row => row.truncated),
          });
        } else if (studio.has(resolved.id)) {
          const real = studio.get(resolved.id);
          value = runtimeValue(real.data, { ...real, host_tainted: true });
        } else if (operator === true && samplePack && own(samplePack.rows || {}, resolved.id)) {
          value = runtimeValue(samplePack.rows[resolved.id], { host_tainted: true, simulated: true });
          sampled.add(resolved.id);
        }
        if (value) values.push([block.id, value]);
      }
      return Object.freeze(Object.fromEntries(values));
    },
    setStudioValue(id, value) {
      if (!['studio-jobs', 'studio-capabilities'].includes(id)) throw new Error('Test: source unavailable');
      try { sourceAge(value); } catch { throw new Error('Test: source shape refused'); }
      if (value.mode !== 'scalar' || !Array.isArray(value.data) || !value.data.every(plain)) {
        throw new Error('Test: source shape refused');
      }
      studio.set(id, value); onChange(state());
    },
    serialize() { return { ...state(), entries: Object.values(entries()).map(value => value.id).sort() }; },
  });
}
