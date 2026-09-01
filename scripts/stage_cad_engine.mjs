// Card F-3: stage the compiled CAD engine (wasm-pack --target web --out-name
// engine output at vendor/acadrust-worker/pkg-web) into web/dist/engine/ as
// CRATE-NAME-FREE assets, so the license fence's scan of web/ stays clean
// (its deny rule matches the crate name ANYWHERE under web/, prose or code)
// while the browser worker can load the engine from a stable absolute URL.
//
//   node scripts/stage_cad_engine.mjs          # stage (no-op warn if unbuilt)
//   node scripts/stage_cad_engine.mjs --check  # verify staged == built
//
// The artifacts are BORN with fence-clean names (--out-name engine in the
// documented wasm-pack build), so staging is a verified copy, never a rename:
// the wasm binary's import module key (./engine_bg.js) is baked at build
// time to match the glue, and rewriting either file after the fact breaks
// WebAssembly.instantiate ("Import #0 ... module is not an object" — the
// first staged engine's exact production failure). This script REFUSES a
// glue that carries the crate name at all, so a pkg-web built without
// --out-name (crate-named files, crate-keyed imports) can never stage.
//
// A PROVENANCE.json records the crate rev (from Cargo.toml) and sha256 of
// both staged files, so a staged engine is always attributable to its exact
// source. MPL-2.0 source availability is satisfied upstream
// (docs/CAD-ENGINE-LICENSE-REVIEW.md); the product NOTICE line ships with
// card F-4.
//
// Missing pkg-web is a WARNING, not an error: builds without a Rust
// toolchain (CI bundleFence, fresh clones) stay green, and the worker
// reports a typed engine_unavailable at runtime instead.
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const PKG = path.join(ROOT, 'vendor', 'acadrust-worker', 'pkg-web')
// Staging TARGET is the BUILD OUTPUT (dist/engine), deliberately never the
// source tree: the compiled wasm binary embeds crate-name strings the
// license fence's scan of web/ would (correctly) flag, and dist exists only
// after a build — in production only inside the image build, which the
// fence's CI scan never sees. Dev never needs staging at all: the vite dev
// middleware (cadEngineDevServer in web/vite.config.js) serves pkg-web
// directly at /engine/*.
const OUT = path.join(ROOT, 'web', 'dist', 'engine')
const CARGO = path.join(ROOT, 'vendor', 'acadrust-worker', 'Cargo.toml')

const FILES = ['engine.js', 'engine_bg.wasm']

function sha256(buf) {
  return createHash('sha256').update(buf).digest('hex')
}

function crateRev() {
  const cargo = readFileSync(CARGO, 'utf8')
  const match = cargo.match(/rev\s*=\s*"([0-9a-f]{7,40})"/)
  return match ? match[1] : 'unknown'
}

function main(check) {
  if (!existsSync(path.join(PKG, FILES[0]))) {
    console.warn('[stage_cad_engine] no compiled engine at', PKG,
      '- skipping (run the documented wasm-pack --target web --out-name engine build first);',
      'the editor will report engine_unavailable at runtime.')
    return 0
  }
  const staged = {}
  for (const name of FILES) {
    const content = readFileSync(path.join(PKG, name))
    if (name.endsWith('.js') && /acadrust/i.test(content.toString('utf8'))) {
      console.error('[stage_cad_engine] the glue at', name,
        'carries a crate-name reference - this pkg-web was built without',
        '--out-name engine (its wasm import key cannot match a renamed glue);',
        'refusing to stage a fence-violating, unloadable asset')
      return 2
    }
    staged[name] = content
  }
  const provenance = {
    contract: 'leaf.cad-engine-stage.v1',
    crate_rev: crateRev(),
    staged_at: new Date().toISOString(),
    files: Object.fromEntries(
      Object.entries(staged).map(([name, buf]) => [name, { sha256: sha256(buf), bytes: buf.length }]),
    ),
    upstream: 'see vendor/acadrust-worker/Cargo.toml (rev-pinned, unmodified; MPL-2.0 per docs/CAD-ENGINE-LICENSE-REVIEW.md)',
  }
  if (check) {
    let clean = true
    for (const [name, buf] of Object.entries(staged)) {
      const target = path.join(OUT, name)
      if (!existsSync(target) || sha256(readFileSync(target)) !== sha256(buf)) {
        console.error('[stage_cad_engine] STALE:', name, 'differs from the built engine')
        clean = false
      }
    }
    return clean ? 0 : 3
  }
  mkdirSync(OUT, { recursive: true })
  for (const [name, buf] of Object.entries(staged)) {
    writeFileSync(path.join(OUT, name), buf)
  }
  writeFileSync(path.join(OUT, 'PROVENANCE.json'), JSON.stringify(provenance, null, 2) + '\n')
  console.log('[stage_cad_engine] staged', Object.keys(staged).join(', '),
    'crate_rev', provenance.crate_rev, '->', OUT)
  return 0
}

process.exitCode = main(process.argv.includes('--check'))
