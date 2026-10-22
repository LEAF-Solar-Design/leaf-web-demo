import { useEffect, useMemo, useRef, useState } from 'react'
import { Background, Controls, ReactFlow, ReactFlowProvider, useReactFlow } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import Palette, { BLOCK_MIME } from './Palette.jsx'
import Inspector from './Inspector.jsx'
import NodeCard from './NodeCard.jsx'
import LatencyPipe from './LatencyPipe.jsx'
import { contract, manifests, manifestFor, registry } from './registry.js'
import { addBlock, connectPipe, disconnectPipe, emptyGraph, missingInputs, pipeId, removeBlock, roundTrip, updateSettings, validateGraph } from './graphDoc.js'
import { connectionStatus } from './portCompat.js'
import { dependencyLayout } from './dependencyLayout.js'

const nodeTypes = { block: NodeCard }
const edgeTypes = { latency: LatencyPipe }
const asPipe = connection => ({ from: { node: connection.source, port: connection.sourceHandle }, to: { node: connection.target, port: connection.targetHandle } })

function Editor({ initialDocument, onChange, entries, measurements, upstream }) {
  const [document, setDocument] = useState(() => roundTrip(initialDocument || emptyGraph()))
  const current = useRef(document)
  const [positions, setPositions] = useState({})
  const [selected, setSelected] = useState(null)
  const [pending, setPending] = useState(null)
  const [message, setMessage] = useState('')
  const [layoutMs, setLayoutMs] = useState(null)
  const [layoutBusy, setLayoutBusy] = useState(false)
  const request = useRef(0)
  const lastRefusal = useRef('')
  const canvas = useRef(null)
  const flow = useReactFlow()
  const validation = useMemo(() => validateGraph(document, entries), [document, entries])
  const commit = next => {
    // Drafts may have required inputs missing, but never gain presentation fields.
    contract.schema(next, 'lab-graph.v1.schema.json')
    current.current = roundTrip(next)
    request.current += 1; setLayoutBusy(false)
    setDocument(current.current)
    onChange?.(roundTrip(current.current))
  }
  const connect = pipe => { commit(connectPipe(current.current, pipe, entries)); setMessage('Pipe connected') }
  const disconnect = id => { commit(disconnectPipe(current.current, id)); setMessage('Pipe disconnected') }
  const remove = id => { commit(removeBlock(current.current, id)); setSelected(null) }
  const add = (manifest, position) => {
    try {
      const bounds = canvas.current.getBoundingClientRect()
      const centre = position || flow.screenToFlowPosition({ x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 })
      const next = addBlock(current.current, manifest)
      const id = next.blocks.at(-1).id
      setPositions(previous => ({ ...previous, [id]: centre }))
      commit(next); setSelected(id); setMessage(`${manifest.title} added`)
    } catch (error) { setMessage(error.message) }
  }
  const relayout = async () => {
    const token = ++request.current
    setLayoutBusy(true)
    const started = performance.now()
    try {
      const dimensions = Object.fromEntries(flow.getNodes().map(node => [node.id, node.measured || {}]))
      const next = await dependencyLayout(current.current, dimensions)
      if (token !== request.current) return
      setPositions(next); setLayoutMs(performance.now() - started); setMessage('Layout updated')
    } catch (error) { if (token === request.current) setMessage(error.message) }
    finally { if (token === request.current) setLayoutBusy(false) }
  }
  useEffect(() => {
    relayout()
    return () => { request.current += 1 }
    // Initial layout only. Editing never discards user positions.
  }, [])

  const nodes = document.blocks.map((block, index) => {
    const manifest = manifestFor(block, entries)
    let settingsError = ''
    try { contract.validate(block.settings, manifest.settings_schema) } catch (error) { settingsError = error.message }
    const handleAllowed = (direction, name) => {
      if (!pending) return { ok: true, reason: '' }
      if (pending.type === 'source' && direction === 'inputs') return connectionStatus(document,
        { from: { node: pending.node, port: pending.port }, to: { node: block.id, port: name } }, entries)
      if (pending.type === 'target' && direction === 'outputs') return connectionStatus(document,
        { from: { node: block.id, port: name }, to: { node: pending.node, port: pending.port } }, entries)
      return { ok: false, reason: 'Connect an output to an input' }
    }
    return { id: block.id, type: 'block', position: positions[block.id] || { x: index * 370, y: 30 }, selected: selected === block.id,
      data: { manifest, missing: missingInputs(document, block, entries), settingsError,
        measurement: measurements.blocks?.[block.id], pending, handleAllowed } }
  })
  const edges = document.pipes.map(pipe => ({ id: pipeId(pipe), type: 'latency',
    source: pipe.from.node, sourceHandle: pipe.from.port, target: pipe.to.node, targetHandle: pipe.to.port,
    data: { measurement: measurements.pipes?.[pipeId(pipe)], budget: manifestFor(document.blocks.find(block => block.id === pipe.from.node), entries).latency_budget_us, disconnect } }))
  return <div className="lab-home lab-canvas-editor" data-canvas-editor>
    <Palette onAdd={add} />
    <section className="lab-canvas-region" aria-label="Dependency canvas">
      <header><h2>Dependency canvas</h2><button type="button" disabled={layoutBusy} onClick={relayout}>Relayout</button>
        <button type="button" onClick={() => flow.fitView({ padding: 0.2 })}>Fit cards</button></header>
      <div className="lab-flow" ref={canvas} onDragOver={event => { event.preventDefault(); event.dataTransfer.dropEffect = 'copy' }}
        onDrop={event => {
          event.preventDefault()
          const manifest = manifests.find(item => item.id === event.dataTransfer.getData(BLOCK_MIME))
          if (manifest) add(manifest, flow.screenToFlowPosition({ x: event.clientX, y: event.clientY }))
        }}>
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes}
          minZoom={0.1} maxZoom={3} fitView nodesConnectable edgesReconnectable={false}
          onNodeClick={(_, node) => setSelected(node.id)} onPaneClick={() => setSelected(null)}
          onNodesChange={changes => {
            const removed = changes.filter(change => change.type === 'remove').map(change => change.id)
            if (removed.length) {
              commit(removed.reduce((next, id) => removeBlock(next, id), current.current)); setSelected(null)
            }
            const moved = changes.filter(change => change.type === 'position' && change.position)
            if (moved.length) {
              request.current += 1; setLayoutBusy(false)
              setPositions(previous => ({ ...previous, ...Object.fromEntries(moved.map(change => [change.id, change.position])) }))
            }
            for (const change of changes) if (change.type === 'select' && change.selected) setSelected(change.id)
          }}
          onEdgesChange={changes => {
            const removed = new Set(changes.filter(change => change.type === 'remove').map(change => change.id))
            if (removed.size) commit({ ...current.current, pipes: current.current.pipes.filter(pipe => !removed.has(pipeId(pipe))) })
          }}
          onConnectStart={(_, params) => { lastRefusal.current = ''; setPending({ node: params.nodeId, port: params.handleId, type: params.handleType }) }}
          onConnectEnd={() => { setPending(null); if (lastRefusal.current) setMessage(lastRefusal.current) }}
          isValidConnection={connection => {
            const status = connectionStatus(document, asPipe(connection), entries)
            lastRefusal.current = status.reason
            return status.ok
          }}
          onConnect={connection => { try { connect(asPipe(connection)) } catch (error) { setMessage(error.message) } }}>
          <Background color="var(--leaf-color-border)" /><Controls />
        </ReactFlow>
      </div>
      {!document.blocks.length && <p>Add a block from the palette to begin.</p>}
      <footer><p role="status">{message}</p><p>{validation.ready ? 'Ready' : `Not ready: ${validation.message}`}</p>
        <span className="leaf-timing">Layout {layoutMs === null ? 'not measured' : `${layoutMs.toFixed(2)} ms`}</span>{' · '}
        <span className="leaf-timing">Validation {validation.ms.toFixed(2)} ms</span></footer>
    </section>
    <Inspector block={document.blocks.find(block => block.id === selected)} document={document} entries={entries} upstream={upstream}
      onSettings={settings => commit(updateSettings(current.current, selected, settings, entries))}
      onConnect={connect} onDisconnect={disconnect} onRemove={remove} onClose={() => setSelected(null)} />
  </div>
}

export default function LabCanvas({ initialDocument, onChange, entries = registry, measurements = {}, upstream = {} }) {
  return <ReactFlowProvider><Editor initialDocument={initialDocument} onChange={onChange} entries={entries} measurements={measurements} upstream={upstream} /></ReactFlowProvider>
}
