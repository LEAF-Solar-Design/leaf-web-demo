import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const dockerfile = readFileSync('../deploy/Dockerfile.web', 'utf8')
const workflow = readFileSync('../.github/workflows/build-platform-images.yml', 'utf8')
const index = readFileSync('index.html', 'utf8')
const api = readFileSync('src/api.js', 'utf8')
const intake = readFileSync('src/site/intakeCache.js', 'utf8')

assert.doesNotMatch(index, /https:\/\/fonts\.googleapis\.com/,
  'the deployable page must not fetch an external font stylesheet')
assert.match(api, /const API_BASE = import\.meta\.env\??\.VITE_API_BASE \?\? ''/,
  'the browser API must default to the current origin')
assert.match(intake, /const API_BASE = import\.meta\.env\??\.VITE_API_BASE \?\? ''/,
  'stage startup requests must default to the current origin')
assert.match(dockerfile, /^ARG VITE_API_BASE=$/m,
  'the deployable web image must default to same-origin API requests')
assert.match(dockerfile, /^ARG VITE_AUTH0_DOMAIN=leafautomation\.us\.auth0\.com$/m)
assert.match(dockerfile, /^ARG VITE_AUTH0_CLIENT_ID=zkJjr0ZFtcyQjyJ8e4zdkdgzoMaVWt5O$/m)
assert.match(dockerfile, /^ARG VITE_AUTH0_AUDIENCE=https:\/\/api\.leafdesign\.ai$/m)
assert.match(dockerfile, /^ARG VITE_LIFECYCLE_UI=1$/m,
  'the deployable web image must bake the reviewed lifecycle UI selector on')
// cad_edit flipped ON 2026-08-30 by operator direction (leaf-web-demo #823).
// One shared web image serves both staging and production, so this pin is the
// documented all-environments enable; rollback = 0 here AND in the Dockerfile
// AND both build-workflow env blocks together.
assert.match(dockerfile, /^ARG VITE_CAD_EDIT=1$/m,
  'the deployable web image must bake the operator-enabled CAD editing surface on')
assert.match(dockerfile, /VITE_AUTH0_DOMAIN=\$\{VITE_AUTH0_DOMAIN\}/)
assert.match(dockerfile, /VITE_AUTH0_CLIENT_ID=\$\{VITE_AUTH0_CLIENT_ID\}/)
assert.match(dockerfile, /VITE_AUTH0_AUDIENCE=\$\{VITE_AUTH0_AUDIENCE\}/)
assert.match(dockerfile, /VITE_LIFECYCLE_UI=\$\{VITE_LIFECYCLE_UI\}/)
// The ARG is inert unless the ENV block forwards it into the vite build.
assert.match(dockerfile, /VITE_CAD_EDIT=\$\{VITE_CAD_EDIT\}/,
  'the VITE_CAD_EDIT build ARG must reach the vite build via the ENV block')
assert.match(dockerfile, /^ARG VITE_SOLAR_FLOW_RAIL=1$/m,
  'the VITE_SOLAR_FLOW_RAIL build ARG must exist and enable the shipped Solar flow rail')
assert.match(dockerfile, /^ARG VITE_SOLAR_SETTINGS_FORM=1$/m,
  'the VITE_SOLAR_SETTINGS_FORM build ARG must exist and enable the shipped Solar settings form')
assert.match(dockerfile, /VITE_SOLAR_FLOW_RAIL=\$\{VITE_SOLAR_FLOW_RAIL\}/,
  'the VITE_SOLAR_FLOW_RAIL build ARG must reach the vite build via the ENV block')
assert.match(dockerfile, /VITE_SOLAR_SETTINGS_FORM=\$\{VITE_SOLAR_SETTINGS_FORM\}/,
  'the VITE_SOLAR_SETTINGS_FORM build ARG must reach the vite build via the ENV block')
assert.equal((workflow.match(/VITE_SOLAR_FLOW_RAIL: "1"/g) || []).length, 2,
  'both web-build env blocks must enable VITE_SOLAR_FLOW_RAIL')
assert.equal((workflow.match(/VITE_SOLAR_SETTINGS_FORM: "1"/g) || []).length, 2,
  'both web-build env blocks must enable VITE_SOLAR_SETTINGS_FORM')

// Integration origin must agree across Docker, reuse fingerprints, and the
// prebuilt static artifacts. Scope each check to its producer role.
const webBuildStage = dockerfile.match(/^FROM node:22-slim AS build\n([\s\S]*?)(?=^FROM )/m)?.[1]
assert.ok(webBuildStage, 'the web Docker build stage must exist')
assert.match(webBuildStage, /^ARG VITE_INTEGRATIONS_API_ORIGIN=$/m,
  'an unset integration origin must stay disabled by default')
const viteEnv = webBuildStage.match(/^ENV VITE_API_BASE=[^\n]*(?:\n[^\n]*)*?(?=\n\n)/m)?.[0]
assert.ok(viteEnv, 'the Vite build ENV block must exist')
assert.match(viteEnv, /^    VITE_INTEGRATIONS_API_ORIGIN=\$\{VITE_INTEGRATIONS_API_ORIGIN\}(?: \\)?$/m,
  'the integration origin ARG must reach Vite through the build ENV')

const workflowSteps = workflow.split(/^      - name: /m).slice(1)
function stepById(id) {
  const matches = workflowSteps.filter(step => step.split('\n').includes(`        id: ${id}`))
  assert.equal(matches.length, 1, `exactly one ${id} step must exist`)
  return matches[0]
}
function stepEnv(step) {
  const env = step.match(/^        env:\n((?:^          [^\n]*\n)+)/m)?.[1]
  assert.ok(env, 'the producer step must have an env block')
  return env
}
const originEnv = '          VITE_INTEGRATIONS_API_ORIGIN: ${{ vars.VITE_INTEGRATIONS_API_ORIGIN }}'
for (const id of ['build-image', 'spec-build-image']) {
  const step = stepById(id)
  assert.match(step, /^        uses: docker\/build-push-action@v6$/m)
  assert.match(step, /^          file: deploy\/Dockerfile\.\$\{\{ matrix\.image \}\}$/m)
  const args = step.match(/^          build-args: \|\n((?:^            [^\n]*\n)+)/m)?.[1]
  assert.ok(args, `${id} must pass Docker build arguments`)
  assert.ok(args.split('\n').includes("            ${{ matrix.image == 'web' && format('VITE_INTEGRATIONS_API_ORIGIN={0}', vars.VITE_INTEGRATIONS_API_ORIGIN) || '' }}"),
    `${id} must pass the repository integration origin only to web`)
}
for (const id of ['surface', 'spec-surface']) {
  const step = stepById(id)
  assert.ok(stepEnv(step).split('\n').includes(originEnv),
    `${id} must forward the repository integration origin through step env`)
  const run = step.split('        run: |\n')[1]
  assert.ok(run, `${id} must calculate a surface fingerprint`)
  const webArgs = run.split('if [ "${{ matrix.image }}" = "web" ]; then')[1]?.split('\n          fi')[0]
  assert.ok(webArgs?.includes('build_args+=(--build-arg "VITE_INTEGRATIONS_API_ORIGIN=$VITE_INTEGRATIONS_API_ORIGIN")'),
    `${id} must fingerprint the same integration origin build argument for web`)
  const fingerprint = run.match(/python scripts\/platform_release_manifest\.py surface-fingerprint \\\n(?:[^\n]*\\\n)*[^\n]*/)?.[0]
  assert.ok(fingerprint?.includes('"${build_args[@]}"'),
    `${id} must pass the web build arguments into the fingerprint calculation`)
}
for (const id of ['surface-web', 'spec-web']) {
  const step = stepById(id)
  assert.match(step, /^        working-directory: web$/m)
  assert.match(step, /^        if: matrix\.image == 'web' &&/m)
  assert.ok(stepEnv(step).split('\n').includes(originEnv),
    `${id} must bake the same repository integration origin into the static artifact`)
  assert.match(step, /^          npm run build$/m)
}

// W7 (docs/convergence/ACCEPTANCE.md, Version 3): the old shell and its
// runtime rail are deleted, so the app always renders the studio shell. The
// image must not install the flags entrypoint drop-in, and the page must not
// load a flags file: nothing at boot may select a shell. (The patterns match
// the separator loosely so this file names no deleted path verbatim.)
assert.doesNotMatch(dockerfile, /write.runtime.flags/,
  'the web image must not install the deleted runtime flags entrypoint drop-in')
assert.doesNotMatch(dockerfile, /VITE_ONE_SHELL/,
  'no build-time VITE fence may select a shell either')
assert.doesNotMatch(index, /\/runtime.flags\.js/,
  'index.html must not load a runtime flags file')
assert.match(index, /<script type="module" src="\/src\/main\.jsx">/,
  'index.html must still load the bundle')

console.log('web container config: page assets, API, integration origin producers and fingerprints, Auth0 SPA values, Solar flow flags, and the absence of a shell rail are pinned')
