// Data-only Lab contracts. Supply the checked-in schema bundle, never document schemas.
const BASE = 'https://leaf.internal/contract/';
const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = message => { throw new Error(message); };
const equal = (a, b) => canonicalJSON(a) === canonicalJSON(b);
const bytes = value => new TextEncoder().encode(canonicalJSON(value)).length;
const nonnegativeInteger = value => Number.isSafeInteger(value) && value >= 0;

export function canonicalJSON(value) {
  const active = new Set();
  function visit(item) {
    if (item === null || typeof item === 'boolean') return JSON.stringify(item);
    if (typeof item === 'number') {
      if (!Number.isFinite(item)) fail('Test: finite number required');
      return JSON.stringify(item);
    }
    if (typeof item === 'string') {
      // JCS rejects lone UTF-16 surrogates instead of silently replacing them.
      for (let i = 0; i < item.length; i += 1) {
        const code = item.charCodeAt(i);
        if (code >= 0xd800 && code <= 0xdbff) {
          const next = item.charCodeAt(++i);
          if (!(next >= 0xdc00 && next <= 0xdfff)) fail('Test: Unicode required');
        } else if (code >= 0xdc00 && code <= 0xdfff) fail('Test: Unicode required');
      }
      return JSON.stringify(item);
    }
    if (!item || typeof item !== 'object' || active.has(item)) fail('Test: JSON required');
    if (!Array.isArray(item) && ![Object.prototype, null].includes(Object.getPrototypeOf(item))) fail('Test: JSON required');
    active.add(item);
    let result;
    if (Array.isArray(item)) {
      if (Object.keys(item).length !== item.length) fail('Test: JSON array required');
      result = `[${Array.from(item, visit).join(',')}]`;
    } else {
      result = `{${Object.keys(item).sort().map(key => `${visit(key)}:${visit(item[key])}`).join(',')}}`;
    }
    active.delete(item);
    return result;
  }
  return visit(value);
}

export async function digest(value) {
  const hash = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonicalJSON(value)));
  return Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
}

// Fail closed on schema vocabulary. This is intentionally not a general schema engine.
const vocabulary = new Set(['$schema', '$id', '$ref', '$defs', '$comment', 'title', 'description', 'type', 'const', 'enum', 'allOf', 'anyOf', 'not', 'if', 'then', 'else', 'required', 'properties', 'additionalProperties', 'propertyNames', 'minProperties', 'maxProperties', 'items', 'minItems', 'maxItems', 'uniqueItems', 'minimum', 'maximum', 'minLength', 'maxLength', 'pattern']);
const embeddedVocabulary = new Set(['type', 'const', 'enum', 'required', 'properties', 'additionalProperties', 'items', 'minItems', 'maxItems', 'minimum', 'maximum', 'minLength', 'maxLength', 'title', 'description']);

export function checkEmbedded(schema) {
  if (!object(schema) || !own(schema, 'type')) fail('Test: schema type required');
  for (const key of Object.keys(schema)) if (!embeddedVocabulary.has(key)) fail('Test: unsupported schema');
  const types = Array.isArray(schema.type) ? schema.type : [schema.type];
  if (!types.length || new Set(types).size !== types.length || types.some(type => !['null', 'boolean', 'number', 'integer', 'string', 'object', 'array'].includes(type))) fail('Test: schema type required');
  for (const key of ['minimum', 'maximum']) if (own(schema, key) && (typeof schema[key] !== 'number' || !Number.isFinite(schema[key]))) fail('Test: schema bounds refused');
  for (const key of ['minItems', 'maxItems', 'minLength', 'maxLength']) if (own(schema, key) && (!Number.isInteger(schema[key]) || schema[key] < 0)) fail('Test: schema bounds refused');
  if (own(schema, 'enum') && (!Array.isArray(schema.enum) || !schema.enum.length || new Set(schema.enum.map(canonicalJSON)).size !== schema.enum.length)) fail('Test: schema choices refused');
  if (own(schema, 'required') && (!Array.isArray(schema.required) || schema.required.some(key => typeof key !== 'string') || new Set(schema.required).size !== schema.required.length)) fail('Test: required setting refused');
  for (const key of ['title', 'description']) if (own(schema, key) && typeof schema[key] !== 'string') fail('Test: schema text refused');
  if (own(schema, 'additionalProperties') && typeof schema.additionalProperties !== 'boolean') fail('Test: closed settings required');
  if (own(schema, 'properties')) {
    if (!object(schema.properties)) fail('Test: schema settings refused');
    Object.values(schema.properties).forEach(checkEmbedded);
  }
  if (own(schema, 'items')) checkEmbedded(schema.items);
  if (types.includes('object')) {
    if (!object(schema.properties) || schema.additionalProperties !== false) fail('Test: closed settings required');
    if (schema.required && (!Array.isArray(schema.required) || schema.required.some(key => !own(schema.properties, key)))) fail('Test: required setting missing');
  }
  if (types.includes('array')) checkEmbedded(schema.items);
}

export function createContract(bundle) {
  const documents = new Map();
  for (const [name, schema] of Object.entries(bundle)) {
    documents.set(new URL(name, BASE).href, schema);
    if (schema.$id) documents.set(schema.$id, schema);
  }
  function validate(value, schema, base = BASE) {
    if (typeof schema === 'boolean') { if (!schema) fail('Test: schema refused'); return; }
    for (const key of Object.keys(schema)) if (!vocabulary.has(key)) fail('Test: unsupported schema');
    if (schema.$id) base = new URL(schema.$id, base).href;
    if (schema.$ref) {
      const url = new URL(schema.$ref, base);
      const pointer = url.hash.slice(1); url.hash = '';
      let target = documents.get(url.href);
      if (!target) fail('Test: schema missing');
      for (const segment of pointer.split('/').slice(1)) target = target[decodeURIComponent(segment).replace(/~1/g, '/').replace(/~0/g, '~')];
      validate(value, target, url.href);
    }
    const matches = part => { try { validate(value, part, base); return true; } catch { return false; } };
    if (schema.allOf) schema.allOf.forEach(part => validate(value, part, base));
    if (schema.anyOf && !schema.anyOf.some(matches)) fail('Test: schema refused');
    if (schema.not && matches(schema.not)) fail('Test: schema refused');
    if (schema.if) { const branch = matches(schema.if) ? schema.then : schema.else; if (branch) validate(value, branch, base); }
    if (own(schema, 'const') && !equal(value, schema.const)) fail('Test: constant refused');
    if (schema.enum && !schema.enum.some(item => equal(item, value))) fail('Test: choice refused');
    if (schema.type) {
      const types = Array.isArray(schema.type) ? schema.type : [schema.type];
      const ok = types.some(type => type === 'null' ? value === null : type === 'object' ? object(value) : type === 'array' ? Array.isArray(value) : type === 'integer' ? Number.isInteger(value) : type === 'number' ? typeof value === 'number' && Number.isFinite(value) : typeof value === type);
      if (!ok) fail('Test: type refused');
    }
    if (typeof value === 'number') {
      if (!Number.isFinite(value) || (own(schema, 'minimum') && value < schema.minimum) || (own(schema, 'maximum') && value > schema.maximum)) fail('Test: bounds refused');
    }
    if (typeof value === 'string') {
      const length = [...value].length;
      if ((own(schema, 'minLength') && length < schema.minLength) || (own(schema, 'maxLength') && length > schema.maxLength) || (schema.pattern && !new RegExp(schema.pattern, 'u').test(value))) fail('Test: text refused');
    }
    if (Array.isArray(value)) {
      if ((own(schema, 'minItems') && value.length < schema.minItems) || (own(schema, 'maxItems') && value.length > schema.maxItems)) fail('Test: array bounds refused');
      if (schema.uniqueItems && new Set(value.map(canonicalJSON)).size !== value.length) fail('Test: duplicate refused');
      if (schema.items !== undefined) value.forEach(item => validate(item, schema.items, base));
    }
    if (object(value)) {
      const keys = Object.keys(value);
      if ((schema.required || []).some(key => !own(value, key))) fail('Test: setting required');
      if ((own(schema, 'minProperties') && keys.length < schema.minProperties) || (own(schema, 'maxProperties') && keys.length > schema.maxProperties)) fail('Test: settings bounds refused');
      for (const key of keys) {
        if (schema.propertyNames) validate(key, schema.propertyNames, base);
        if (own(schema.properties || {}, key)) validate(value[key], schema.properties[key], base);
        else if (schema.additionalProperties !== undefined) validate(value[key], schema.additionalProperties, base);
      }
    }
  }
  function schema(value, name) {
    canonicalJSON(value);
    const root = documents.get(new URL(name, BASE).href);
    if (!root) fail('Test: schema missing');
    validate(value, root);
  }
  function block(manifest, trusted, previous = null) {
    schema(manifest, 'lab-block.v1.schema.json');
    if (!trusted || !equal(manifest, trusted)) fail('Test: registry mismatch');
    checkEmbedded(manifest.settings_schema);
    validate(manifest.sample_input, {type: 'object', additionalProperties: false, properties: Object.fromEntries(Object.entries(manifest.inputs).map(([key, pin]) => [key, pin.nullable ? {anyOf: [pin.schema, {type: 'null'}]} : pin.schema])), required: Object.keys(manifest.inputs).filter(key => manifest.inputs[key].required && !manifest.inputs[key].has_default)});
    validate(manifest.sample_output, {type: 'object', additionalProperties: false, properties: Object.fromEntries(Object.entries(manifest.outputs).map(([key, pin]) => [key, pin.nullable ? {anyOf: [pin.schema, {type: 'null'}]} : pin.schema])), required: Object.keys(manifest.outputs)});
    if (manifest.latency_budget_us.p50 > manifest.latency_budget_us.p99) fail('Test: latency bounds refused');
    for (const pin of [...Object.values(manifest.inputs), ...Object.values(manifest.outputs)]) {
      checkEmbedded(pin.schema);
      if (!pin.nullable && (Array.isArray(pin.schema.type) ? pin.schema.type : [pin.schema.type]).includes('null')) fail('Test: nullability mismatch');
      if (pin.has_default) {
        if (pin.default === null && pin.nullable) continue;
        validate(pin.default, pin.schema);
      }
    }
    if (!object(manifest.ui_schema) || Object.keys(manifest.ui_schema).some(key => !['order', 'labels'].includes(key))) fail('Test: presentation refused');
    if (previous && (previous.id !== manifest.id || Object.keys(previous.outputs).some(key => !own(manifest.outputs, key) || !equal(previous.outputs[key], manifest.outputs[key])))) fail('Test: output changed');
    return true;
  }
  function graph(document, registry, previous = null, wireBytes = bytes(document)) {
    schema(document, 'lab-graph.v1.schema.json');
    if (!nonnegativeInteger(wireBytes) || wireBytes > 262144 || bytes(document) > 262144) fail('Test: graph byte limit');
    const nodes = new Map();
    for (const node of document.blocks) {
      if (nodes.has(node.id)) fail('Test: duplicate block');
      const manifest = registry[`${node.registry.id}@${node.registry.version}`];
      if (!manifest || manifest.version.code_sha256 !== node.registry.code_sha256) fail('Test: registry mismatch');
      block(manifest, manifest);
      validate(node.settings, manifest.settings_schema);
      nodes.set(node.id, manifest);
    }
    const adjacency = new Map([...nodes.keys()].map(id => [id, []]));
    const attached = new Set();
    function pin(endpoint, direction) {
      const pins = nodes.get(endpoint.node)?.[direction];
      if (!pins || !own(pins, endpoint.port)) fail('Test: pin missing');
      return pins[endpoint.port];
    }
    for (const pipe of document.pipes) {
      if (!compatible(pin(pipe.from, 'outputs'), pin(pipe.to, 'inputs'))) fail('Test: incompatible pins');
      const target = `${pipe.to.node}/${pipe.to.port}`;
      if (attached.has(target)) fail('Test: duplicate input');
      attached.add(target);
      adjacency.get(pipe.from.node).push(pipe.to.node);
    }
    for (const [id, manifest] of nodes) for (const [name, input] of Object.entries(manifest.inputs)) {
      if (input.required && !input.has_default && !attached.has(`${id}/${name}`)) fail('Test: input required');
    }
    const done = new Set(); const stack = [];
    function visit(id) {
      const index = stack.indexOf(id);
      if (index !== -1) fail(`Test: cycle ${[...stack.slice(index), id].join(' -> ')}`);
      if (done.has(id)) return;
      stack.push(id); adjacency.get(id).forEach(visit); stack.pop(); done.add(id);
    }
    nodes.forEach((_, id) => visit(id));
    Object.values(document.outputs).forEach(endpoint => pin(endpoint, 'outputs'));
    if (previous && Object.entries(previous.outputs).some(([slug, endpoint]) => !own(document.outputs, slug) || !equal(endpoint, document.outputs[slug]))) fail('Test: output changed');
    return true;
  }
  function embed(message, context, state) {
    schema(message, 'lab-embed.v1.schema.json');
    if (context.authenticated !== true || !context.origin || context.origin === '*' || context.eventOrigin !== context.origin || !context.source || context.eventSource !== context.source) fail('Test: host mismatch');
    for (const key of ['channel', 'principal_proof', 'source_sha', 'proposal_revision', 'proposal_hash']) if (message[key] !== context[key]) fail('Test: host binding mismatch');
    const now = context.now_ms;
    if (!nonnegativeInteger(now) || !nonnegativeInteger(state.sequence) || !Array.isArray(state.accepted_at_ms) || state.accepted_at_ms.some(time => !nonnegativeInteger(time) || time > now) || message.sent_at_ms > now || message.expires_at_ms <= now || message.expires_at_ms <= message.sent_at_ms || message.expires_at_ms - message.sent_at_ms > 30000 || message.sequence <= state.sequence) fail('Test: replay refused');
    const limit = message.kind === 'source.value' ? 262144 : message.kind === 'widget.publish-request' ? 40960 : 4096;
    if (bytes(message) > limit || (context.wire_bytes !== undefined && (!nonnegativeInteger(context.wire_bytes) || context.wire_bytes > limit))) fail('Test: message byte limit');
    const recent = state.accepted_at_ms.filter(time => time > now - 1000);
    if (recent.length >= 20) fail('Test: message rate limit');
    // Commit replay state only after every check. Host data is never returned as telemetry.
    state.sequence = message.sequence; state.accepted_at_ms = [...recent, now];
    return true;
  }
  return {schema, validate, block, graph, embed};
}

export function compatible(output, input) {
  if (output.mode !== input.mode || output.unit !== input.unit || (output.nullable && !input.nullable)) return false;
  if (equal(output.schema, input.schema)) return true;
  const a = output.schema; const b = input.schema;
  const simple = new Set(['type', 'minimum', 'maximum', 'enum']);
  if ([...Object.keys(a), ...Object.keys(b)].some(key => !simple.has(key))) return false;
  if (!(a.type === b.type || (a.type === 'integer' && b.type === 'number'))) return false;
  if (own(b, 'minimum') && (!own(a, 'minimum') || a.minimum < b.minimum)) return false;
  if (own(b, 'maximum') && (!own(a, 'maximum') || a.maximum > b.maximum)) return false;
  if (b.enum && (!a.enum || a.enum.some(value => !b.enum.some(item => equal(value, item))))) return false;
  return true;
}
