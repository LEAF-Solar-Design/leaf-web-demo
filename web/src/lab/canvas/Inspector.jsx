import BlockPanel from './BlockPanel.jsx'
import { manifestFor } from './registry.js'

export default function Inspector({ block, document, onSettings, onConnect, onDisconnect, onRemove, onClose, entries, upstream }) {
  return <section className="lab-inspector" aria-label="Block inspector"><h2>Block inspector</h2>
    {block ? <>
      <button type="button" onClick={onClose}>Close inspector</button>
      <h3>{manifestFor(block, entries).title}</h3><p>{block.id}</p>
      <BlockPanel key={block.id} block={block} document={document} entries={entries} upstream={upstream}
        onSettings={onSettings} onConnect={onConnect} onDisconnect={onDisconnect} />
      <button type="button" onClick={() => onRemove(block.id)}>Remove block</button>
    </> : <p>Select a card to open Setup, Configure, Test and Code.</p>}
  </section>
}
