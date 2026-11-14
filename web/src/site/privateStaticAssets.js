// Parent-side verified byte custody. Does not execute code, allocate Blob URLs,
// pass auth headers to a frame or claim to render a Next export.
const SHA = /^[0-9a-f]{64}$/
const UUID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/
const PATH = /^\/(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]+$/
const fail = () => { throw new Error('private_preview_unavailable') }
const hash = async bytes => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2, '0')).join('')
const current = (check, signal) => { if (signal?.aborted || check() !== true) fail() }
const safePath = p => typeof p === 'string' && p.length <= 2048 && !/[\u0000-\u001f\u007f]/.test(p) && PATH.test(p) && !p.split('/').some(x => x === '.' || x === '..')
const media = t => typeof t === 'string' && t.length <= 256 && /^[a-z0-9.+-]+\/[a-z0-9.+-]+(?:; charset=utf-8)?$/i.test(t)

export async function loadPrivateStaticAssets({ preview, requestAsset, isCurrent, signal }) {
  if (typeof requestAsset !== 'function' || typeof isCurrent !== 'function') fail()
  // Snapshot all untrusted/caller-owned metadata before the first await.
  let p
  try { p = JSON.parse(JSON.stringify(preview)) } catch { fail() }
  if (!p || !UUID.test(p.jobId) || !UUID.test(p.artifactId) ||
      !/^[0-9a-f]{40}$/.test(p.sourceCommit) || !/^[0-9a-f]{40}$/.test(p.sourceTree) ||
      !SHA.test(p.manifestSha256) || !SHA.test(p.bundleSha256) ||
      !UUID.test(p.bindingId) || !SHA.test(p.toolchainSha256)) fail()
  const m = p.manifest
  if (!m || m.bundleSha256 !== p.bundleSha256 || m.bindingId !== p.bindingId ||
      m.toolchainSha256 !== p.toolchainSha256 || !SHA.test(m.requestDigest) ||
      !Array.isArray(m.files) || !m.files.length || m.files.length > 4096) fail()
  let total = 0
  const paths = new Set()
  for (const f of m.files) {
    if (!f || !safePath(f.path) || paths.has(f.path) || !SHA.test(f.sha256) ||
        !media(f.contentType) || !Number.isSafeInteger(f.sizeBytes) || f.sizeBytes < 0 || f.sizeBytes > 2 * 1024 * 1024) fail()
    total += f.sizeBytes
    if (total > 32 * 1024 * 1024) fail()
    paths.add(f.path)
  }
  if (!paths.has('/index.html')) fail()
  const serialized = JSON.stringify([m.requestDigest, m.bundleSha256, m.bindingId, m.toolchainSha256,
    m.files.map(f => [f.path, f.sha256, f.sizeBytes, f.contentType])])
  current(isCurrent, signal)
  if (await hash(new TextEncoder().encode(serialized)) !== p.manifestSha256) fail()
  current(isCurrent, signal)
  const loaded = []
  for (const f of m.files) {
    current(isCurrent, signal)
    // The caller owns authenticated transport; only path and immutable witnesses
    // cross this callback. Returned asset bytes contain no caller credentials.
    const raw = await requestAsset(Object.freeze({ jobId: p.jobId, sourceCommit: p.sourceCommit,
      sourceTree: p.sourceTree, artifactId: p.artifactId, manifestSha256: p.manifestSha256, path: f.path }), signal)
    current(isCurrent, signal)
    let r
    try { r = JSON.parse(JSON.stringify(raw)) } catch { fail() }
    if (!r || r.schema !== 'leaf.private-static-asset.v1' || r.job_id !== p.jobId ||
        r.source_commit !== p.sourceCommit || r.source_tree !== p.sourceTree ||
        r.artifact_id !== p.artifactId || r.manifest_sha256 !== p.manifestSha256 ||
        r.path !== f.path || r.content_type !== f.contentType || r.size_bytes !== f.sizeBytes || r.sha256 !== f.sha256 ||
        typeof r.body_base64 !== 'string' || r.body_base64.length !== 4 * Math.ceil(f.sizeBytes / 3)) fail()
    let binary
    try { binary = atob(r.body_base64) } catch { fail() }
    if (btoa(binary) !== r.body_base64 || binary.length !== f.sizeBytes) fail()
    const bytes = Uint8Array.from(binary, c => c.charCodeAt(0))
    if (await hash(bytes) !== f.sha256) fail()
    current(isCurrent, signal)
    loaded.push(Object.freeze({ path: f.path, contentType: f.contentType, bytes }))
  }
  current(isCurrent, signal)
  return Object.freeze({ jobId: p.jobId, artifactId: p.artifactId, manifestSha256: p.manifestSha256,
    files: Object.freeze(loaded) })
}
