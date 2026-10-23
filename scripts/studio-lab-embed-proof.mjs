import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, readdir, rm, stat, writeFile, utimes } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';

const root = new URL('../', import.meta.url);
const sha = value => typeof value === 'string' && /^[a-f0-9]{7,40}$/i.test(value);
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const requireThat = (condition, message) => {
  if (!condition) throw Object.assign(new Error(message), { proofEvidence: message });
};
// Signed-out middleware headers differ from the authenticated route headers.
// CSP directives are optional; when supplied they must allow the exact origin.
const walkPairs = [
  ['Dashboard source dragged', 'Dashboard source is on the canvas'],
  ['Transform wired', 'Transform has a connected input'],
  ['Value or Chart card shown', 'Value or Chart card shows as of and p50 badges'],
];

function options(args) {
  const result = { maxAgeHours: 24, allowMissingReceipt: false, json: false, selfTest: false };
  const values = { '--studio': 'studio', '--ops': 'ops', '--receipt-dir': 'receiptDir', '--max-age-hours': 'maxAgeHours' };
  const flags = { '--allow-missing-receipt': 'allowMissingReceipt', '--json': 'json', '--self-test': 'selfTest' };
  const seen = new Set();
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    requireThat(!seen.has(arg), 'duplicate option'); seen.add(arg);
    if (flags[arg]) result[flags[arg]] = true;
    else if (values[arg]) {
      requireThat(args[i + 1] && !args[i + 1].startsWith('--'), 'option requires a value');
      result[values[arg]] = args[++i];
    } else throw new Error('unknown option');
  }
  result.maxAgeHours = Number(result.maxAgeHours);
  requireThat(Number.isFinite(result.maxAgeHours) && result.maxAgeHours > 0, 'max age must be positive');
  if (!result.selfTest) {
    for (const key of ['studio', 'ops']) {
      const url = new URL(result[key]);
      requireThat(['http:', 'https:'].includes(url.protocol) && !url.username && !url.password
        && url.pathname === '/' && !url.search && !url.hash, 'studio and ops must be origins');
      result[key] = url.origin;
    }
    requireThat(typeof result.receiptDir === 'string' && result.receiptDir.length > 0, 'receipt directory required');
  }
  return result;
}

function includesOrigin(policy, directive, origin) {
  // Every policy and every duplicate directive must permit the exact origin.
  return Boolean(policy) && policy.split(',').every(part => {
    const directives = part.split(';').map(item => item.trim().split(/\s+/)).filter(tokens => tokens[0] === directive);
    return directives.length > 0 && directives.every(tokens => tokens.slice(1).includes(origin) && !tokens.includes("'none'"));
  });
}

function framePolicy(policy, directive, origin) {
  const policies = (policy || '').split(',').filter(part => part.split(';')
    .some(item => item.trim().split(/\s+/)[0] === directive));
  if (!policies.length) return 'absent';
  requireThat(policies.every(part => includesOrigin(part, directive, origin)), `${directive} origin refused`);
  return 'includes-origin';
}

async function get(origin, path) {
  // Manual redirects prevent leaving the selected origin. Never send authority.
  return fetch(new URL(path, origin), {
    method: 'GET', redirect: 'manual', credentials: 'omit',
    headers: { accept: 'text/html, application/json', 'user-agent': 'Leaf-Lab-Proof/1.0' },
    signal: AbortSignal.timeout(20_000),
  });
}

async function protocol() {
  const base = new URL('web/src/lab/contract/', root);
  const pin = JSON.parse(await readFile(new URL('PIN.json', base), 'utf8'));
  const bundle = {};
  for (const [name, entry] of Object.entries(pin.files)) {
    if (!name.endsWith('.schema.json')) continue;
    const bytes = await readFile(new URL(name, base));
    requireThat(hash(bytes) === entry.sha256, 'schema PIN mismatch');
    bundle[name] = JSON.parse(bytes);
    if (name === 'leaf-block-manifest.v1.schema.json') {
      bundle['fixtures/lab/leaf-block-manifest.v1.schema.json'] = bundle[name];
    }
  }
  for (const name of ['lab-embed.v1.schema.json', 'lab-graph.v1.schema.json']) requireThat(bundle[name], 'required schema missing');
  const { createContract } = await import(new URL('web/src/lab/contract.js', root));
  const contract = createContract(bundle);
  const bytes = await readFile(new URL('contract/fixtures/lab/lab-embed.v1.golden.json', root));
  const golden = JSON.parse(bytes);
  requireThat(Array.isArray(golden.messages) && golden.messages.length > 0, 'golden messages missing');
  for (const message of golden.messages) {
    contract.schema(message, 'lab-embed.v1.schema.json');
    // Each golden is an independent message with sequence 1.
    contract.embed(message, golden.context, structuredClone(golden.state));
    if (message.kind === 'proposal') contract.schema(message.payload.document, 'lab-graph.v1.schema.json');
  }
  assert.throws(() => contract.schema({ ...golden.messages[0], kind: 'invalid' }, 'lab-embed.v1.schema.json'));
  return `schema PINs match; ${golden.messages.length} golden messages; sha256=${hash(bytes)}`;
}

function exactKeys(value, keys) {
  requireThat(value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key)), 'receipt fields refused');
}

async function newestReceipt(directory) {
  let names;
  try { names = await readdir(directory); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  const candidates = await Promise.all(names.filter(name => /^sel107-operator-.*\.json$/.test(name)).map(async name => {
    const path = join(directory, name);
    return { path, modified: (await stat(path)).mtimeMs };
  }));
  candidates.sort((a, b) => b.modified - a.modified || b.path.localeCompare(a.path));
  if (!candidates.length) return null;
  return { path: candidates[0].path, value: JSON.parse(await readFile(candidates[0].path, 'utf8')) };
}

async function validateReceipt(record, measured, maxAgeHours) {
  const receipt = record.value;
  exactKeys(receipt, ['schema', 'signed_by', 'signed_at', 'studio_sha', 'ops_sha', 'walk', 'screenshots', 'csp', 'hosts_relayed']);
  requireThat(receipt.schema === 'leaf.sel107-operator-receipt.v1', 'receipt schema refused');
  // Closed vocabulary prevents row contents, machine names, addresses and run
  // identifiers from hiding in free text. Only the operator handle is free text.
  requireThat(typeof receipt.signed_by === 'string' && /^[a-z][a-z0-9_]{1,31}$/i.test(receipt.signed_by), 'operator handle required');
  requireThat(typeof receipt.signed_at === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{3})?Z$/.test(receipt.signed_at), 'ISO signing time required');
  const age = Date.now() - Date.parse(receipt.signed_at);
  requireThat(Number.isFinite(age) && age >= 0 && age <= maxAgeHours * 3_600_000, 'receipt stale or future dated');
  requireThat(sha(receipt.studio_sha) && sha(receipt.ops_sha) && measured.studio_sha
    && receipt.studio_sha.toLowerCase() === measured.studio_sha, 'receipt Studio SHA stale');
  requireThat(!measured.ops_sha || receipt.ops_sha.toLowerCase() === measured.ops_sha, 'receipt dashboard SHA stale');
  requireThat(Array.isArray(receipt.walk) && receipt.walk.length >= 3, 'three walk steps required');
  for (const step of receipt.walk) {
    exactKeys(step, ['action', 'observed']);
    requireThat(walkPairs.some(([action, observed]) => step.action === action && step.observed === observed), 'walk text refused');
  }
  requireThat(walkPairs.every(([action]) => receipt.walk.some(step => step.action === action)), 'walk coverage missing');
  exactKeys(receipt.csp, ['frame_ancestors', 'frame_src', 'x_frame_options_ops', 'api_cache_control']);
  exactKeys(receipt.csp.api_cache_control, ['principal', 'relay']);
  for (const key of ['frame_ancestors', 'frame_src', 'x_frame_options_ops']) {
    requireThat(receipt.csp[key] === measured.csp[key], 'operator frame headers stale');
  }
  for (const key of ['principal', 'relay']) {
    requireThat(typeof measured.csp.api_cache_control[key] === 'string'
      && receipt.csp.api_cache_control[key] === measured.csp.api_cache_control[key], 'operator API cache headers stale');
  }
  requireThat(Number.isSafeInteger(receipt.hosts_relayed) && receipt.hosts_relayed >= 1, 'positive relay count required');
  requireThat(Array.isArray(receipt.screenshots) && new Set(receipt.screenshots).size >= 2, 'two screenshots required');
  for (const name of receipt.screenshots) {
    // Neutral basenames only. Keep screenshots beside the external receipt.
    requireThat(typeof name === 'string' && /^(studio|dashboard|walk)-[1-9]\d?\.png$/.test(name), 'neutral screenshot basename required');
    const info = await stat(resolve(dirname(record.path), name));
    requireThat(info.isFile() && info.size > 0, 'screenshot missing or empty');
  }
  // When public health is gated, this is explicitly the operator-read deployed
  // SHA, never x-vercel-id, a branch tip, or a hash invented from response data.
  return { signed_by: receipt.signed_by, signed_at: receipt.signed_at,
    studio_sha: receipt.studio_sha.toLowerCase(), ops_sha: receipt.ops_sha.toLowerCase(),
    ops_sha_source: measured.ops_sha ? 'health' : 'operator-deployment',
    hosts_relayed: receipt.hosts_relayed, screenshots: receipt.screenshots.length, csp: receipt.csp };
}

async function run(config, emit = () => {}) {
  const result = { studio_sha: null, ops_sha: null, checks: [], receipt: null, ok: false,
    csp: { frame_ancestors: null, frame_src: null, x_frame_options_ops: null, api_cache_control: {} } };
  const check = async (name, work) => {
    try {
      const evidence = await work(); result.checks.push({ check: name, status: 'ok', evidence }); emit(`ok ${name}: ${evidence}`);
    } catch (error) {
      // Never echo response bodies, private headers, receipt text or file paths.
      const evidence = error.proofEvidence || (error.code === 'ENOENT' ? 'required evidence file missing' : 'request, file or contract validation failed');
      result.checks.push({ check: name, status: 'FAIL', evidence }); emit(`FAIL ${name}: ${evidence}`);
    }
  };
  await check('studio-health', async () => {
    const response = await get(config.studio, '/api/health');
    requireThat(response.status === 200, 'health status');
    const health = await response.json(); requireThat(sha(health.source_sha), 'health SHA');
    result.studio_sha = health.source_sha.toLowerCase(); return `200; source_sha=${result.studio_sha}`;
  });
  await check('studio-embed', async () => {
    const response = await get(config.studio, '/app/lab?embed=1');
    requireThat(response.status === 200, 'embed status');
    result.csp.frame_ancestors = framePolicy(response.headers.get('content-security-policy'), 'frame-ancestors', config.ops);
    requireThat(!/\bDENY\b/i.test(response.headers.get('x-frame-options') || ''), 'frame denied');
    const body = await response.text();
    requireThat(/<div\b[^>]*\sid\s*=\s*(["'])root\1/i.test(body), 'SPA root required');
    const moduleScript = [...body.matchAll(/<script\b[^>]*>/gi)]
      .filter(([tag]) => /\stype\s*=\s*(["'])module\1/i.test(tag))
      .map(([tag]) => tag.match(/\ssrc\s*=\s*(["'])(\/assets\/[^"']+)\1/i)?.[2])
      .find(Boolean);
    requireThat(moduleScript, 'SPA asset module required');
    requireThat(!/<form\b[^>]*>[\s\S]*?<input\b[^>]*\stype\s*=\s*(?:"password"|'password'|password(?=[\s/>]))/i.test(body), 'sign-in form refused');
    result.embed = { shell: 'spa', module_script: moduleScript };
    return `200; frame_ancestors=${result.csp.frame_ancestors}; no DENY; shell=${result.embed.shell}; module_script=${moduleScript}`;
  });
  await check('ops-page', async () => {
    const response = await get(config.ops, '/studio-lab');
    result.csp.frame_src = framePolicy(response.headers.get('content-security-policy'), 'frame-src', config.studio);
    requireThat(/^DENY$/i.test(response.headers.get('x-frame-options') || ''), 'dashboard frame protection required');
    result.csp.x_frame_options_ops = 'DENY';
    if (response.status === 307) {
      const location = new URL(response.headers.get('location'), config.ops);
      requireThat(location.origin === config.ops && /\/(?:login|sign-?in|auth\/signin)(?:\/|$)/i.test(location.pathname), 'sign-in redirect required');
    } else {
      requireThat(response.status === 200 && /sign[ -]?in/i.test(await response.text()), 'signed-out page required');
    }
    return `signed-out page or 307 sign-in redirect; frame_src=${result.csp.frame_src}; X-Frame-Options: DENY`;
  });
  for (const name of ['principal', 'relay']) await check(`ops-${name}`, async () => {
    const response = await get(config.ops, `/api/studio-lab/${name}`);
    requireThat(response.status === 401, 'session gate required');
    const policy = response.headers.get('cache-control') || '';
    const tokens = policy.toLowerCase().split(',').map(token => token.trim());
    requireThat(tokens.includes('no-store') || tokens.some(token => /^max-age\s*=\s*"?0"?$/.test(token)), 'non-caching API policy required');
    result.csp.api_cache_control[name] = policy;
    return `401; Cache-Control: ${policy}`;
  });
  await check('ops-build', async () => {
    const response = await get(config.ops, '/api/health');
    if (response.status === 200) {
      const health = await response.json();
      const source = health.data?.build?.sourceSha ?? health.build?.sourceSha;
      if (sha(source)) result.ops_sha = source.toLowerCase();
    } else requireThat([401, 403, 404].includes(response.status), 'unexpected health status');
    return result.ops_sha ? `health build.sourceSha=${result.ops_sha}` : 'public SHA unavailable; operator deployment SHA required in receipt';
  });
  await check('protocol-goldens', protocol);
  await check('receipt', async () => {
    const record = await newestReceipt(config.receiptDir);
    if (!record && config.allowMissingReceipt) {
      emit('skip receipt: absent; operator proof gate remains pending');
      return 'missing receipt allowed for automation only';
    }
    requireThat(record, 'operator receipt missing');
    result.receipt = await validateReceipt(record, result, config.maxAgeHours);
    if (!result.ops_sha) result.ops_sha = result.receipt.ops_sha;
    return `fresh; both SHAs bound; dashboard SHA source=${result.receipt.ops_sha_source}; authenticated walk and CSP attested`;
  });
  result.ok = result.checks.every(item => item.status === 'ok');
  return result;
}

async function selfTest() {
  const directory = await mkdtemp(join(tmpdir(), 'leaf-lab-proof-'));
  let studio; let ops;
  let cspMode = 'absent';
  let opsFrameOptions = 'DENY';
  let apiCacheControl = 'public, max-age=0, must-revalidate';
  const spaBody = '<!doctype html><html><head><script type="module" crossorigin src="/assets/index-fixture.js"></script></head><body><div id="root"></div></body></html>';
  let embedBody = spaBody;
  const server = createServer((request, response) => {
    assert.equal(request.headers.cookie, undefined); assert.equal(request.headers.authorization, undefined);
    const isStudio = request.url.startsWith('/studio/');
    const path = request.url.replace(/^\/(studio|ops)/, '');
    response.setHeader('Cache-Control', apiCacheControl);
    if (!isStudio && opsFrameOptions) response.setHeader('X-Frame-Options', opsFrameOptions);
    if (cspMode !== 'absent') response.setHeader('Content-Security-Policy', isStudio
      ? `frame-ancestors ${cspMode === 'bad-studio' ? `${ops}.invalid` : ops}`
      : `frame-src ${cspMode === 'bad-ops' ? `${studio}.invalid` : studio}`);
    if (path === '/api/health') return response.end(JSON.stringify(isStudio ? { source_sha: 'a'.repeat(40) } : { data: { build: { sourceSha: 'b'.repeat(40) } } }));
    if (path === '/app/lab?embed=1') return response.end(embedBody);
    if (path === '/studio-lab') { response.statusCode = 307; response.setHeader('Location', '/auth/login'); return response.end(); }
    response.statusCode = 401; response.end('{}');
  });
  try {
    await new Promise(resolveListen => server.listen(0, '127.0.0.1', resolveListen));
    const origin = `http://127.0.0.1:${server.address().port}`;
    // Separate listeners below model the two origins without network access.
    studio = origin; ops = origin;
    const fresh = {
      schema: 'leaf.sel107-operator-receipt.v1', signed_by: 'operator', signed_at: new Date().toISOString(),
      studio_sha: 'a'.repeat(40), ops_sha: 'b'.repeat(40),
      walk: walkPairs.map(([action, observed]) => ({ action, observed })),
      screenshots: ['studio-1.png', 'dashboard-1.png'],
      csp: { frame_ancestors: 'absent', frame_src: 'absent', x_frame_options_ops: 'DENY',
        api_cache_control: { principal: apiCacheControl, relay: apiCacheControl } }, hosts_relayed: 1,
    };
    await Promise.all(fresh.screenshots.map(name => writeFile(join(directory, name), 'fixture screenshot')));
    const stale = { ...fresh, signed_at: new Date(Date.now() - 48 * 3_600_000).toISOString() };
    const stalePath = join(directory, 'sel107-operator-stale.json');
    const freshPath = join(directory, 'sel107-operator-fresh.json');
    await writeFile(stalePath, JSON.stringify(stale));
    await utimes(stalePath, new Date(0), new Date(0));
    await writeFile(freshPath, JSON.stringify(fresh));
    // Separate listeners use the same canned handler with a path prefix, so
    // production get(), redirect handling and exact origin checks all run.
    const handler = server.listeners('request')[0];
    server.removeAllListeners('request');
    server.on('request', (request, response) => { request.url = `/studio${request.url}`; handler(request, response); });
    const dashboard = createServer((request, response) => { request.url = `/ops${request.url}`; handler(request, response); });
    try {
      await new Promise(resolveListen => dashboard.listen(0, '127.0.0.1', resolveListen));
      ops = `http://127.0.0.1:${dashboard.address().port}`;
      const config = { studio, ops, receiptDir: directory, maxAgeHours: 24, allowMissingReceipt: false };
      const first = await run(config);
      requireThat(first.ok, `self-test fixture: ${first.checks.filter(item => item.status === 'FAIL').map(item => `${item.check}: ${item.evidence}`).join('; ')}`);
      assert.deepEqual(first.csp, fresh.csp);
      assert.deepEqual(first.receipt.csp, fresh.csp);
      assert.deepEqual(first.embed, { shell: 'spa', module_script: '/assets/index-fixture.js' });
      for (const body of ['<form><input type="password"></form>', `${spaBody}<form><input type="password"></form>`,
        spaBody.replace('<div id="root"></div>', ''), spaBody.replace('type="module"', 'type="text/javascript"'),
        spaBody.replace('/assets/', '/other/')]) {
        embedBody = body;
        assert.equal((await run(config)).checks.find(item => item.check === 'studio-embed').status, 'FAIL');
      }
      embedBody = spaBody;
      cspMode = 'bad-studio';
      assert.equal((await run(config)).checks.find(item => item.check === 'studio-embed').status, 'FAIL');
      cspMode = 'bad-ops';
      assert.equal((await run(config)).checks.find(item => item.check === 'ops-page').status, 'FAIL');
      cspMode = 'included';
      const withCsp = { ...fresh, csp: { ...fresh.csp, frame_ancestors: 'includes-origin', frame_src: 'includes-origin' } };
      await writeFile(freshPath, JSON.stringify(withCsp));
      assert.equal((await run(config)).ok, true);
      cspMode = 'absent';
      await writeFile(freshPath, JSON.stringify(fresh));
      opsFrameOptions = '';
      assert.equal((await run(config)).checks.find(item => item.check === 'ops-page').status, 'FAIL');
      opsFrameOptions = 'DENY';
      apiCacheControl = 'public, max-age=60';
      const caching = await run(config);
      for (const name of ['principal', 'relay']) assert.equal(caching.checks.find(item => item.check === `ops-${name}`).status, 'FAIL');
      apiCacheControl = 'private, no-store';
      const noStore = { ...fresh, csp: { ...fresh.csp, api_cache_control: { principal: apiCacheControl, relay: apiCacheControl } } };
      await writeFile(freshPath, JSON.stringify(noStore));
      assert.equal((await run(config)).ok, true);
      apiCacheControl = fresh.csp.api_cache_control.principal;
      for (const mutation of [stale, { ...fresh, ops_sha: 'c'.repeat(40) }, { ...fresh, studio_sha: 'c'.repeat(40) }, { ...fresh, hosts_relayed: 'private' },
        { ...fresh, walk: fresh.walk.slice(0, 2) }, { ...fresh, extra: 'refused' },
        withCsp, { ...fresh, csp: { ...fresh.csp, api_cache_control: {} } },
        { ...fresh, screenshots: ['studio-2.png', 'dashboard-2.png'] }]) {
        await writeFile(freshPath, JSON.stringify(mutation));
        assert.equal((await run({ ...config, allowMissingReceipt: true })).checks.find(item => item.check === 'receipt').status, 'FAIL');
      }
      await rm(freshPath); // Newest remaining receipt is stale, never bypassed.
      assert.equal((await run({ ...config, allowMissingReceipt: true })).ok, false);
      await rm(stalePath);
      assert.equal((await run(config)).ok, false);
      assert.equal((await run({ ...config, allowMissingReceipt: true })).ok, true);
      assert.equal(includesOrigin(`frame-src ${studio}.invalid`, 'frame-src', studio), false);
      assert.throws(() => options(['--max-age-hours', '0', '--self-test']));
      return { studio_sha: fresh.studio_sha, ops_sha: fresh.ops_sha, receipt: null, csp: first.csp,
        checks: [{ check: 'self-test', status: 'ok', evidence: 'live fixture, goldens, receipt freshness, binding, privacy and negative cases' }], ok: true };
    } finally { dashboard.closeAllConnections(); await new Promise(done => dashboard.close(done)); }
  } finally {
    server.closeAllConnections(); await new Promise(done => server.close(done));
    await rm(directory, { recursive: true, force: true });
  }
}

try {
  const config = options(process.argv.slice(2));
  const result = config.selfTest ? await selfTest() : await run(config, config.json ? () => {} : line => console.log(line));
  if (config.json) console.log(JSON.stringify(result));
  else if (config.selfTest) console.log(`ok self-test: ${result.checks[0].evidence}`);
  process.exitCode = result.ok ? 0 : 1;
} catch (error) {
  const evidence = error.proofEvidence || 'invalid usage or self-test failure';
  const result = { studio_sha: null, ops_sha: null, checks: [{ check: 'execution', status: 'FAIL', evidence }], receipt: null, ok: false };
  console.log(process.argv.includes('--json') ? JSON.stringify(result) : `FAIL execution: ${evidence}`);
  process.exitCode = 1;
}
