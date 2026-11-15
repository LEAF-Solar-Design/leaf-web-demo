import { useState } from 'react'
import { deriveProjectReadResultState } from './solarReadResultModel.js'

const PAGE_ROWS = 100
const TOOL = 'solar-solaredge-tracking-read'
const SCHEMA = 'leaf.solar-project-solaredge-tracking-read.v1'
const integer = value => Number.isSafeInteger(value) && value >= 0

function verifiedPage(data, binding, catalogDigest) {
  const state = deriveProjectReadResultState({ binding, data, catalogDigest, current: false })
  if (state.mode !== 'ready' || data.tool !== TOOL) return null
  const output = data.output, page = output.page, items = output.items
  if (output.schema !== SCHEMA || output.section !== 'unassigned' ||
      output.accepted !== true || output.labels_only !== true ||
      !Array.isArray(items) || items.length > 1000 ||
      !items.every(ref => typeof ref === 'string' && ref.length > 0 && ref.length <= 100) ||
      new Set(items).size !== items.length || !page ||
      !integer(page.offset) || !integer(page.returned) || !integer(page.total) ||
      page.returned !== items.length || page.offset + items.length > page.total ||
      page.next_offset !== (page.offset + items.length < page.total ? page.offset + items.length : null) ||
      (page.next_offset !== null && items.length === 0)) return null
  return { items: [...items], offset: page.offset, total: page.total, nextOffset: page.next_offset }
}

export default function SolarEdgeTrackingReferences({ data, binding, catalogDigest }) {
  const [navigation, setNavigation] = useState({ data, index: 0 })
  const page = verifiedPage(data, binding, catalogDigest)
  if (!page) return null
  // A new read result resets immediately, before a prior page index can render.
  const index = navigation.data === data ? navigation.index : 0
  const start = index * PAGE_ROWS
  const end = Math.min(start + PAGE_ROWS, page.items.length)
  return <section aria-label="Unassigned panel references" className="solar-read-section">
    <h4>Unassigned panel references</h4>
    {page.items.length === 0 ? <p>No unassigned panel references on this saved page.</p> : <>
      <p aria-live="polite">Showing {page.offset + start + 1} to {page.offset + end} of {page.total} unassigned panels.</p>
      <table className="grid"><thead><tr><th scope="col">Panel reference</th></tr></thead>
        <tbody>{page.items.slice(start, end).map(ref => <tr key={ref}><td>{ref}</td></tr>)}</tbody>
      </table>
      <button type="button" disabled={index === 0}
        onClick={() => setNavigation({ data, index: index - 1 })}>Previous references</button>
      <button type="button" disabled={end === page.items.length}
        onClick={() => setNavigation({ data, index: index + 1 })}>Next references</button>
    </>}
    {page.nextOffset !== null && <p>More saved references remain. Set Offset to {page.nextOffset} and choose Read project output.</p>}
  </section>
}
