import { resolveManifest } from '../canvas/registry.js';
import { exportValue, sourceAge } from '../runtime/evaluate.js';
import { hasOperator } from '../runtime/ops.js';
import { HOST_READ_IDS } from './hostProvider.js';

export function residencyOf(manifest) {
  return manifest.effect === 'read' && HOST_READ_IDS.includes(manifest.id) ? 'host' : 'tenant';
}

function resolvedBlocks(document, entries) {
  return new Map(document.blocks.flatMap(block => {
    const { manifest, problem } = resolveManifest(block, entries);
    return problem ? [] : [[block.id, manifest]];
  }));
}

export function taintedBlocks(document, entries) {
  const manifests = resolvedBlocks(document, entries);
  const tainted = new Set([...manifests].filter(([, manifest]) => residencyOf(manifest) === 'host').map(([id]) => id));
  let changed = true;
  while (changed) {
    changed = false;
    for (const pipe of document.pipes) {
      if (manifests.has(pipe.from.node) && manifests.has(pipe.to.node)
        && tainted.has(pipe.from.node) && !tainted.has(pipe.to.node)) {
        tainted.add(pipe.to.node); changed = true;
      }
    }
  }
  return tainted;
}

const identifying = value => /[a-f0-9]{16,}/i.test(value)
  || /[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}/i.test(value)
  || /[^\s@]+@[^\s@]+\.[^\s@]+/.test(value) || /:[a-f0-9]{64}/i.test(value);

function identifyingSetting(value, seen = new Set(), depth = 0) {
  if (typeof value === 'string') return identifying(value);
  if (!value || typeof value !== 'object' || seen.has(value)) return false;
  if (depth > 32) return true;
  seen.add(value);
  return Object.values(value).some(child => identifyingSetting(child, seen, depth + 1));
}

export function validateResidency(document, entries) {
  const manifests = resolvedBlocks(document, entries);
  const tainted = taintedBlocks(document, entries);
  const problems = [];
  for (const pipe of document.pipes) {
    const target = manifests.get(pipe.to.node);
    if (tainted.has(pipe.from.node) && target
      && (target.effect !== 'pure' || target.kind !== 'builtin' || !hasOperator(target.id))) {
      problems.push(`host data cannot reach ${target.title} (${target.id}): it does not run in the browser`);
    }
  }
  for (const block of document.blocks) {
    const manifest = manifests.get(block.id);
    if (manifest && residencyOf(manifest) === 'host' && identifyingSetting(block.settings)) {
      problems.push(`host block ${block.id} settings must be non-identifying selectors`);
    }
  }
  return { ok: problems.length === 0, problems, tainted: [...tainted].sort() };
}

export function guardSink(name, sink) {
  return function (...args) {
    const seen = new Set();
    const refuse = () => { throw new Error(`Test: host data cannot leave the runtime (${name})`); };
    function visit(value, depth) {
      if (!value || !['object', 'function'].includes(typeof value) || seen.has(value)) return;
      // Fail closed when the bounded walk cannot establish a safe graph.
      if (depth > 32 || seen.size >= 10000) refuse();
      seen.add(value);
      // Inherited properties and overridden Map/Set iterators are outside this data-flow guard's scope.
      if ([Object.prototype, null].includes(Object.getPrototypeOf(value))
        && Object.getOwnPropertyDescriptor(value, 'host_tainted')?.value === true) refuse();
      let branded = false;
      try { sourceAge(value); branded = true; } catch { /* Ordinary container. */ }
      if (branded && value.host_tainted) {
        try { exportValue(value); } catch { refuse(); }
      }
      if (value instanceof Map) for (const [key, child] of value) { visit(key, depth + 1); visit(child, depth + 1); }
      if (value instanceof Set) for (const child of value) visit(child, depth + 1);
      for (const key of Reflect.ownKeys(value)) {
        const descriptor = Object.getOwnPropertyDescriptor(value, key);
        // A getter could return a different value when the sink later reads it.
        if (descriptor.get || descriptor.set) refuse();
        visit(descriptor.value, depth + 1);
      }
    }
    visit(this, 0);
    args.forEach(value => visit(value, 0));
    return sink.apply(this, args);
  };
}
