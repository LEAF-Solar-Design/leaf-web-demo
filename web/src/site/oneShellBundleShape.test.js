import { bundleGraph } from '../../scripts/bundle-assets.mjs'
import { nextFenceDirectory, NEXT_FENCE_TIMEOUT } from '../../scripts/next-fence-fixtures.mjs'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { beforeAll, describe, expect, it } from 'vitest'

const WEB_ROOT = process.cwd()

function emittedJavaScript(directory) {
  const graph = bundleGraph(directory)
  const initial = [...graph.startup].map(file => graph.assets.get(file)).join('\n')
  expect(initial).not.toMatch(/__THREE__|three\.(?:module|core)|node_modules[/\\\\]three[/\\\\]/)
  return [...graph.assets].filter(([file]) => file.endsWith('.js')).map(([, code]) => code).join('\n')
}

// Every source file the bundle is built from, plus the container and CI
// surfaces a build-arg fence would have to touch (ACCEPTANCE flag matrix:
// "any build-arg change touches FOUR files").
function sourceFiles() {
  const out = []
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const p = join(dir, name)
      if (statSync(p).isDirectory()) { if (name !== 'node_modules') walk(p) }
      else if (/\.(jsx?|mjs|cjs|css|html)$/.test(name)) out.push(p)
    }
  }
  walk(join(WEB_ROOT, 'src'))
  if (existsSync(join(WEB_ROOT, 'index.html'))) out.push(join(WEB_ROOT, 'index.html'))
  const repo = join(WEB_ROOT, '..')
  for (const rel of ['deploy/Dockerfile.web', '.github/workflows/build-platform-images.yml']) {
    const p = join(repo, rel)
    if (existsSync(p)) out.push(p)
  }
  return out
}

describe('one studio shell bundle shape', () => {
  let js = ''
  beforeAll(async () => {
    js = emittedJavaScript(await nextFenceDirectory('enabled'))
  }, NEXT_FENCE_TIMEOUT)

  it('ships the studio shell — the host AND SiteRoot\'s ground node, not just App\'s portal wrapper', () => {
    expect(js).toContain('studio-shell')
    expect(js).toMatch(/studio-ground(?!-viewer)["'\s]/)
    expect(js).toContain('studio-ground-viewer')
  })

  it('carries no read of the legacy __LEAF_FLAGS global — nothing shipped can select another shell', () => {
    expect(js).not.toContain('__LEAF_FLAGS')
  })

  it('never grew a VITE-shaped one-shell flag (source scan — a defined flag is erased from dist)', () => {
    const self = join(WEB_ROOT, 'src', 'site', 'oneShellBundleShape.test.js')
    const hits = sourceFiles()
      .filter((p) => p !== self)
      .filter((p) => readFileSync(p, 'utf8').includes('VITE_ONE_SHELL'))
      .map((p) => relative(WEB_ROOT, p))
    expect(hits).toEqual([])
    expect(js).not.toContain('VITE_ONE_SHELL')
  })
})
