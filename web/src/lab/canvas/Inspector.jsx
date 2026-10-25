import BlockPanel from './BlockPanel.jsx'
import { resolveManifest } from './registry.js'

export default function Inspector({ block, document, onSettings, onConnect, onDisconnect, onRemove, onClose, entries, upstream, codeEvidence }) {
  const resolution = block && resolveManifest(block, entries)
  // BlockPanel enumerates every block's outputs to build its Source options.
  const resolvedDocument = { ...document, blocks: document.blocks.filter(node => !resolveManifest(node, entries).problem) }
  return <section className="lab-inspector" aria-label="Block inspector"><h2>Block inspector</h2>
    {block ? <>
      {!resolution.problem && <button type="button" onClick={onClose}>Close inspector</button>}
      <h3>{resolution.manifest.title}</h3><p>{block.id}</p>
      {resolution.problem ? <p role="alert">{resolution.problem}</p> : <>
        <BlockPanel key={block.id} block={block} document={resolvedDocument} entries={entries} upstream={upstream} codeEvidence={codeEvidence}
          onSettings={onSettings} onConnect={onConnect} onDisconnect={onDisconnect} />
      </>}
      <button type="button" onClick={() => onRemove(block.id)}>Remove block</button>
    </> : <p>Select a card to open Setup, Configure, Test and Code.</p>}
  </section>
}
