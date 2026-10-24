import { canonicalJSON } from '../contract.js';
import { addBlock } from '../canvas/graphDoc.js';
import { contract } from '../canvas/registry.js';
import { HOST_INTENT_ID, HOST_INTENT_KINDS, intentManifest } from './hostProvider.js';

const refused = reason => ({ ok: false, reason });
const enginePattern = /^[a-z0-9][a-z0-9-]{0,31}$/;
const noncePattern = /^[A-Za-z0-9_-]{16,64}$/;
const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';

// The only caller is an explicit click handler; evaluation, polling and reruns never reach this.
export function createHostIntentDispatcher({ bridge, framed, intentsEnabled, cryptoImpl = globalThis.crypto }) {
  const proposalRefusal = () => refused(bridge.state?.status === 'too_large'
    ? bridge.state.reason || `proposal exceeds the 4096-byte wire limit (${bridge.state.bytes} bytes)`
    : 'Ops did not take the proposal');
  return {
    async propose(draft, intent) {
      if (!framed) return refused('Open Lab inside Ops to send a host intent');
      if (!bridge?.admitted) return refused('Ops has not accepted this Lab session');
      if (typeof intentsEnabled !== 'function' || !intentsEnabled()) return refused('Lab intents are off');
      if (!intent || Object.keys(intent).sort().join(',') !== 'engine_id,kind'
        || !HOST_INTENT_KINDS.includes(intent.kind)
        || typeof intent.engine_id !== 'string' || !enginePattern.test(intent.engine_id)) {
        return refused('Host intent is not a supported kind or engine');
      }
      const blocks = draft?.document?.blocks;
      if (Array.isArray(blocks) && blocks.filter(block => block?.registry?.id === HOST_INTENT_ID).length > 1) {
        return refused('A graph can hold one host intent');
      }
      let nextDraft;
      let nonce;
      try {
        // Snapshot before awaiting the bridge so caller edits cannot change this click.
        nextDraft = JSON.parse(canonicalJSON(draft));
        contract.schema(nextDraft.document, 'lab-graph.v1.schema.json');
        if (typeof nextDraft.ready !== 'boolean' || !Array.isArray(nextDraft.problems)
          || !nextDraft.problems.every(problem => typeof problem === 'string')) throw new Error();
        const bytes = cryptoImpl.getRandomValues(new Uint8Array(18));
        nonce = '';
        for (let i = 0; i < bytes.length; i += 3) {
          const value = (bytes[i] << 16) | (bytes[i + 1] << 8) | bytes[i + 2];
          nonce += alphabet[(value >>> 18) & 63] + alphabet[(value >>> 12) & 63]
            + alphabet[(value >>> 6) & 63] + alphabet[value & 63];
        }
        if (!noncePattern.test(nonce)) return refused('Host intent is not a supported kind or engine');
        if (!blocks.some(block => block.registry.id === HOST_INTENT_ID)) {
          nextDraft.document = addBlock(nextDraft.document, intentManifest());
        }
        nextDraft.document.blocks = nextDraft.document.blocks.map(block => block.registry.id === HOST_INTENT_ID
          ? { ...block, settings: { kind: intent.kind, engine_id: intent.engine_id, nonce } } : block);
        contract.schema(nextDraft.document, 'lab-graph.v1.schema.json');
      } catch {
        return refused('Host intent draft is malformed or secure randomness is unavailable');
      }
      try {
        const proposed = await bridge.proposal(nextDraft);
        if (proposed !== true) return proposalRefusal();
        if (await bridge.run() !== true) return proposalRefusal();
        return { ok: true, nonce };
      } catch {
        return proposalRefusal();
      }
    },
  };
}
