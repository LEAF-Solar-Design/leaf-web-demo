import { createServer } from 'node:http'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

const port = Number(process.env.LEAF_CAT_PROOF_API_PORT || 8165)
const state = makeCatProofState()

const server = createServer(async (request, response) => {
  const chunks = []
  for await (const chunk of request) chunks.push(chunk)
  let body = {}
  try { body = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}') } catch { body = {} }
  const url = new URL(request.url || '/', `http://127.0.0.1:${port}`)
  const result = catProofResponse({ method: request.method || 'GET', path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, state)
  response.statusCode = result.status
  response.setHeader('Access-Control-Allow-Origin', '*')
  response.setHeader('Access-Control-Allow-Headers', '*')
  if (result.body == null) return response.end()
  response.setHeader('Content-Type', 'application/json')
  response.end(JSON.stringify(result.body))
})

server.listen(port, '127.0.0.1', () => {
  process.stdout.write(`cat proof API listening on http://127.0.0.1:${port}\n`)
})
