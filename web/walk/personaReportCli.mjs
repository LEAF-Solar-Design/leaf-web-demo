import { createHash } from 'node:crypto'
import { lstatSync, readFileSync, realpathSync, statSync, writeFileSync } from 'node:fs'
import { basename, dirname, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { artifactReference, boundInput, collectPersonaRuns, evidenceFor, MAX_INPUT_BYTES, MAX_RECEIPT_BYTES, redact } from './report.mjs'
import { buildPersonaReport } from './personaReport.mjs'

const usage = 'Usage: --manifest <json> --receipt <json> --out <json>'

function checkedPath(value, base, output = false, origin = base) {
  if (typeof value !== 'string' || !value || /[\x00-\x1f\\%?#]/.test(value)
    || value.split('/').includes('..')) throw new Error('Invalid path: traversal is forbidden')
  const path = resolve(origin, value)
  const root = realpathSync(base)
  let missing = false
  if (output) {
    try { lstatSync(path) } catch (error) {
      if (error.code !== 'ENOENT') throw error
      missing = true
    }
  }
  // lstat distinguishes a new output from a dangling symlink, which must fail.
  const actual = missing ? resolve(realpathSync(dirname(path)), basename(path)) : realpathSync(path)
  if (actual !== root && !actual.startsWith(root + sep)) throw new Error('Path escapes input root')
  return actual
}

function readJson(path, limit = MAX_INPUT_BYTES) {
  if (!statSync(path).isFile() || statSync(path).size > limit) throw new Error('JSON input exceeds byte bound or is not a file')
  const bytes = readFileSync(path)
  if (bytes.length > limit) throw new Error('JSON input exceeds byte bound')
  let value
  try { value = JSON.parse(bytes.toString('utf8')) } catch { throw new Error('Input is not JSON') }
  boundInput(value)
  return value
}

export function personaReportCli(args) {
  const flags = new Map()
  for (let i = 0; i < args.length; i += 2) {
    const key = args[i], value = args[i + 1]
    if (!['--manifest', '--receipt', '--out'].includes(key) || !value || value.startsWith('--') || flags.has(key)) throw new Error(usage)
    flags.set(key, value)
  }
  if (flags.size !== 3) throw new Error(usage)
  const manifestArgument = flags.get('--manifest')
  // An absolute manifest can locate a portable input bundle outside the checkout.
  // Its lexical parent is the trust boundary; symlinks cannot relocate the file.
  const base = dirname(resolve(manifestArgument))
  const manifest = readJson(checkedPath(manifestArgument, base, false, process.cwd()))
  if (!manifest || manifest.schema !== 'leaf.persona-inputs.v1' || !Array.isArray(manifest.inputs)
    || manifest.inputs.length < 1 || manifest.inputs.length > 16) throw new Error('Invalid persona inputs manifest')
  const ids = new Set()
  for (const input of manifest.inputs) {
    if (!input || typeof input.id !== 'string' || !/^[a-z0-9-]{1,64}$/.test(input.id) || ids.has(input.id)) throw new Error('Invalid or duplicate input id')
    ids.add(input.id)
  }
  const featureMap = readJson(checkedPath(manifest.feature_map, base), 8 * MAX_RECEIPT_BYTES)
  const walkReceipt = readJson(checkedPath(flags.get('--receipt'), base, false, process.cwd()))
  const outputPath = checkedPath(flags.get('--out'), base, true, process.cwd())
  const runs = []
  let evidenceBytes = 0
  for (const input of manifest.inputs) {
    const playwrightReport = readJson(checkedPath(input.report, base))
    const artifactsRoot = checkedPath(input.artifacts_root, base)
    if (!statSync(artifactsRoot).isDirectory()) throw new Error('artifacts_root must be a directory')
    const decodeEvidence = (result) => {
      const attachment = result.attachments.find((item) => item.name === 'walk-evidence')
      const reference = artifactReference(attachment.path)
      evidenceBytes += typeof attachment.body === 'string' ? Buffer.byteLength(attachment.body, 'base64')
        : statSync(checkedPath(reference, artifactsRoot)).size
      if (evidenceBytes > MAX_INPUT_BYTES) throw new Error('Total decoded evidence exceeds byte bound')
      const evidence = evidenceFor(result, artifactsRoot)
      return typeof attachment.body === 'string' ? { ...evidence,
        evidence_ref: `inline/${createHash('sha256').update(attachment.body).digest('hex')}.json` } : evidence
    }
    for (const run of collectPersonaRuns({ playwrightReport, featureMap, identity: input.identity, decodeEvidence })) {
      runs.push({ ...run, evidence_ref: `persona-sources/${input.id}/${run.evidence_ref}` })
    }
  }
  const output = JSON.stringify(buildPersonaReport({ featureMap, walkReceipt, runs })) + '\n'
  if (Buffer.byteLength(output) > MAX_RECEIPT_BYTES) throw new Error('Persona output exceeds 1 MiB bound')
  writeFileSync(outputPath, output, 'utf8')
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { personaReportCli(process.argv.slice(2)) }
  catch (error) {
    process.stderr.write(redact(error.message, 300) + '\n')
    process.exitCode = 2
  }
}
