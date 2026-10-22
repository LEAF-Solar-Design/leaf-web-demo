import ELK from 'elkjs/lib/elk.bundled.js'

function releaseWorker(elk) {
  const worker = elk.worker?.worker
  if (worker && typeof worker.terminate !== 'function') {
    // The bundled in-process worker has no terminate method. Drop its result
    // handler and our reference; its remaining work can then be collected.
    worker.onmessage = () => {}
    elk.worker.worker = null
  } else elk.terminateWorker()
}

// Each canvas owns its runner. Cancellation rejects the dropped request and
// releases ELK's worker instead of merely ignoring its eventual result.
export function createDependencyLayoutRunner(createElk = () => new ELK()) {
  let active = null
  const cancel = () => {
    if (!active) return
    const job = active
    active = null
    releaseWorker(job.elk)
    job.reject(new DOMException('Layout cancelled', 'AbortError'))
  }
  return {
    cancel,
    run(document, dimensions = {}) {
      cancel()
      const elk = createElk()
      return new Promise((resolve, reject) => {
        const job = { elk, reject }
        active = job
        const finish = (settle, value) => {
          if (active !== job) return
          active = null
          releaseWorker(elk)
          settle(value)
        }
        layoutWithElk(document, dimensions, elk).then(
          result => finish(resolve, result), error => finish(reject, error))
      })
    },
  }
}

export function dependencyRanks(document) {
  const ids = document.blocks.map(block => block.id).sort()
  const ranks = Object.fromEntries(ids.map(id => [id, 0]))
  const incoming = new Map(ids.map(id => [id, new Set()]))
  const outgoing = new Map(ids.map(id => [id, new Set()]))
  for (const pipe of document.pipes) {
    if (!incoming.has(pipe.to.node) || !outgoing.has(pipe.from.node)) throw new Error('Block missing')
    incoming.get(pipe.to.node).add(pipe.from.node)
    outgoing.get(pipe.from.node).add(pipe.to.node)
  }
  const queue = ids.filter(id => !incoming.get(id).size)
  let visited = 0
  while (queue.length) {
    const id = queue.shift(); visited += 1
    for (const target of [...outgoing.get(id)].sort()) {
      ranks[target] = Math.max(ranks[target], ranks[id] + 1)
      incoming.get(target).delete(id)
      if (!incoming.get(target).size) { queue.push(target); queue.sort() }
    }
  }
  if (visited !== ids.length) throw new Error('Cycles are refused')
  return ranks
}

export function dependencyLayout(document, dimensions = {}) {
  return createDependencyLayoutRunner().run(document, dimensions)
}

async function layoutWithElk(document, dimensions, elk) {
  const ranks = dependencyRanks(document)
  if (!document.blocks.length) return {}
  const ids = document.blocks.map(block => block.id).sort()
  const height = id => dimensions[id]?.height || 360
  const width = id => dimensions[id]?.width || 310
  const result = await elk.layout({ id: 'dependency',
    layoutOptions: { 'elk.algorithm': 'layered', 'elk.direction': 'DOWN',
      'elk.randomSeed': '1', 'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
      'elk.spacing.nodeNode': '60', 'elk.layered.spacing.nodeNodeBetweenLayers': '100' },
    children: ids.map(id => ({ id, width: width(id), height: height(id) })),
    edges: document.pipes.map(pipe => ({ id: `${pipe.from.node}/${pipe.from.port}>${pipe.to.node}/${pipe.to.port}`,
      sources: [pipe.from.node], targets: [pipe.to.node] })).sort((a, b) => a.id.localeCompare(b.id)),
  })
  // ELK packs disconnected components separately. Align their ranks globally so only
  // drawn dependencies affect vertical order, never Lane, insertion order or settings.
  const rankY = [30]
  const maxRank = Math.max(...Object.values(ranks))
  for (let rank = 0; rank < maxRank; rank += 1) {
    rankY.push(rankY[rank] + Math.max(...ids.filter(id => ranks[id] === rank).map(height)) + 100)
  }
  const positions = {}
  for (let rank = 0; rank <= maxRank; rank += 1) {
    const peers = result.children.filter(node => ranks[node.id] === rank).sort((a, b) => a.x - b.x || a.id.localeCompare(b.id))
    let right = 30
    for (const node of peers) {
      const x = Math.max(right, node.x)
      positions[node.id] = { x, y: rankY[rank] }
      right = x + width(node.id) + 60
    }
  }
  return positions
}
