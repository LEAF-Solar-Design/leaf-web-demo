import ELK from 'elkjs/lib/elk.bundled.js'

const elk = new ELK()
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

export async function dependencyLayout(document, dimensions = {}) {
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
