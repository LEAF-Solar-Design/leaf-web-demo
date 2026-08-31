// Bytes-in/bytes-out CLI over the compiled acadrust wasm build (card F-1).
//
// Contract: DXF bytes on stdin -> parse -> write -> round-tripped DXF bytes
// on stdout, exit 0. Any failure (missing compiled build, parse error, write
// error, oversized input) exits nonzero with ONE stderr line and writes
// nothing to stdout — the python corpus adapter treats a nonzero exit as a
// refused round trip and folds it into the receipt, never a crash.
//
// Lives inside vendor/acadrust-worker/ deliberately: this is the license
// fence's allowed prefix, so naming the engine here is legal, and the
// compiled pkg-node/ build it imports is produced by the documented wasm-pack
// command in worker-entry.mjs (day-3 spike). A tree that never ran that build
// exits 3 here instead of half-working.
//
// Input bound: 32 MiB. The corpus fixtures are bytes-scale; the bound exists
// so a misuse of this CLI cannot buffer unbounded stdin.

const MAX_INPUT_BYTES = 32 * 1024 * 1024

async function readStdin(limit) {
  const chunks = []
  let total = 0
  for await (const chunk of process.stdin) {
    total += chunk.length
    if (total > limit) {
      throw new Error(`stdin exceeds the ${limit}-byte bound`)
    }
    chunks.push(chunk)
  }
  return Buffer.concat(chunks, total)
}

async function main() {
  let engine
  try {
    engine = await import('./pkg-node/acadrust_worker.js')
  } catch (error) {
    process.stderr.write(`no compiled engine build (run the documented wasm-pack build first): ${error.message}\n`)
    return 3
  }
  let input
  try {
    input = await readStdin(MAX_INPUT_BYTES)
  } catch (error) {
    process.stderr.write(`${error.message}\n`)
    return 4
  }
  try {
    const parsed = engine.parseDxf(new Uint8Array(input))
    const written = engine.writeDxf(parsed)
    process.stdout.write(Buffer.from(written))
    return 0
  } catch (error) {
    process.stderr.write(`round_trip_failed: ${error instanceof Error ? error.message : String(error)}\n`)
    return 5
  }
}

main().then((code) => { process.exitCode = code })
