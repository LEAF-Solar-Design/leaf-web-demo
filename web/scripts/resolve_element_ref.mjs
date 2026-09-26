import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { ELEMENT_REF_PATTERN, resolveElementRefs } from '../vite-plugins/elementSourceStamp.js'

const usage = 'Usage: node scripts/resolve_element_ref.mjs [--root <webRoot>] (--list | <ref> [<ref> ...])'

function main(args) {
  let root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
  let list = false
  const refs = []
  for (let i = 0; i < args.length; i++) {
    const arg = args[i]
    if (arg === '--root') {
      if (!args[i + 1] || args[i + 1].startsWith('--')) throw new Error('--root requires a webRoot')
      root = path.resolve(args[++i])
    } else if (arg === '--list') {
      list = true
    } else if (arg.startsWith('-')) {
      throw new Error(`unknown flag ${arg}`)
    } else {
      if (!ELEMENT_REF_PATTERN.test(arg)) throw new Error(`malformed ref ${arg}`)
      refs.push(arg)
    }
  }
  if ((!list && !refs.length) || (list && refs.length)) throw new Error(usage)
  if (refs.length > 256) throw new Error('at most 256 refs per call')
  const { matches, table } = resolveElementRefs(root, refs)
  if (list) {
    const rows = [...table].flatMap(([ref, stamps]) => stamps.map((stamp) => ({ ref, stamp })))
    rows.sort((a, b) => a.stamp < b.stamp ? -1 : a.stamp > b.stamp ? 1 : a.ref < b.ref ? -1 : a.ref > b.ref ? 1 : 0)
    for (const { ref, stamp } of rows) console.log(`${ref}\t${stamp}`)
    return 0
  }
  let unresolved = false
  for (const ref of refs) {
    const stamps = matches.get(ref)
    if (!stamps.length) {
      console.log(`${ref}\tUNRESOLVED`)
      unresolved = true
    } else {
      for (const stamp of stamps) console.log(`${ref}\t${stamp}`)
    }
  }
  return unresolved ? 1 : 0
}

try {
  process.exitCode = main(process.argv.slice(2))
} catch (error) {
  console.error(error.message)
  process.exitCode = 2
}
