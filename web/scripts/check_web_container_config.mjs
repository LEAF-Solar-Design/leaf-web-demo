import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const dockerfile = readFileSync('../deploy/Dockerfile.web', 'utf8')
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

console.log('web container config: page assets, API, and Auth0 SPA values are pinned')
