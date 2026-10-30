import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

// Actual Next.js rendering with fixture-backed APIs. No live account mutations.
const CORS = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' }
// Match the standalone catalog's leaf.solar-tool-view.v1 metadata and the
// eleven Rooftop positions in solarFlowModel.test.js's live catalog.
const ROOFTOP_ROWS = [
  ['solar-settings', 'stringing', 1, 10],
  ['solar-panels-from-drawing', 'placement', 1, 15],
  ['solar-size-strings', 'stringing', 1, 20],
  ['solar-panel-groups', 'stringing', 1, 30],
  ['solar-solve-proposal', 'stringing', 1, 40],
  ['solar-commit-solve', 'stringing', 1, 50],
  ['solar-correct-string', 'stringing', 1, 60],
  ['solar-assign-equipment', 'stringing', 1, 70],
  ['solar-string-conductors', 'stringing', 2, 85],
  ['solar-homeruns', 'stringing', 1, 80],
  ['solar-schedule', 'stringing', 1, 90],
]
const SOLAR_FAMILIES = ['stringing', 'placement'].map(family => ({
  family_id: family, label: family, description: 'Solar design steps for this drawing.',
  capabilities: ROOFTOP_ROWS.filter(([, f]) => f === family).map(([name, , wave, order]) => {
    const ready = ['solar-settings', 'solar-solve-proposal'].includes(name)
    return {
      name, version: '1.0.0', description: name, kind: 'builtin',
      capabilities: [name === 'solar-solve-proposal' ? 'drawing.read' : 'drawing.write'],
      params: { type: 'object', properties: {} },
      solar: { schema: 'leaf.solar-tool-view.v1', name, family, wave, order,
        entitlement: name === 'solar-solve-proposal' ? 'solve' : 'run_write', interaction: { mode: 'form' } },
      availability: { entitled: true, implemented: true, engine_ready: true,
        input_ready: ready, refusal_reasons: ready ? [] : ['persisted_graph_unavailable'] },
    }
  }),
}))

async function backend(page) {
  const state = makeCatProofState()
  const mutations = []
  await page.addInitScript(() => {
    localStorage.setItem('leaf.jwt', 'fixture-token')
    localStorage.setItem('leaf.coach.dismissed.v1', '1')
  })
  await page.route('http://leaf-proof.invalid/api/**', async route => {
    const req = route.request(), url = new URL(req.url())
    if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: CORS })
    if (!['GET', 'HEAD'].includes(req.method()) && url.pathname !== '/api/telemetry') {
      mutations.push(`${req.method()} ${url.pathname}`)
    }
    const reply = catProofResponse({ method: req.method(), path: url.pathname,
      query: Object.fromEntries(url.searchParams), body: req.postData() ? req.postDataJSON() : {} }, state)
    // Extend only this spec's catalog, keeping the real cat intake and all
    // existing routes. The session drawing has no project binding.
    if (req.method() === 'GET' && url.pathname === '/api/capabilities' && reply.status === 200) {
      reply.body = { ...reply.body, families: [...reply.body.families, ...SOLAR_FAMILIES] }
    }
    await route.fulfill({ status: reply.status, headers: CORS, contentType: 'application/json', body: JSON.stringify(reply.body ?? null) })
  })
  return mutations
}

async function expectCenterHit(control) {
  await expect(control).toBeVisible()
  await expect.poll(() => control.evaluate(el => {
    const b = el.getBoundingClientRect()
    const hit = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2)
    // Probe the edges as well: a full-size box can be clipped by its row.
    // Hit testing also respects fixed descendants that escape ancestor clips.
    const edges = [[b.x + b.width / 2, b.top + 1], [b.x + b.width / 2, b.bottom - 1],
      [b.left + 1, b.y + b.height / 2], [b.right - 1, b.y + b.height / 2]]
    if (!edges.every(([x, y]) => {
      const edgeHit = document.elementFromPoint(x, y)
      return edgeHit === el || el.contains(edgeHit)
    })) return false
    return b.width > 0 && b.height > 0 && b.x >= 0 && b.y >= 0
      && b.right <= innerWidth && b.bottom <= innerHeight && (hit === el || el.contains(hit))
  })).toBe(true)
}

async function expectPopulatedSolar(page, info, phone) {
  const rail = page.locator('.solar-flow-host [data-testid="solar-flow-rail"]')
  await expect(rail).toHaveAttribute('data-flow', 'rooftop')
  await expect(rail).not.toHaveAttribute('data-demo', 'true')
  await expect(rail.locator('.solar-flow-step')).toHaveCount(11)
  await expect(rail.locator('.solar-flow-label')).toHaveText(ROOFTOP_ROWS.map(([name]) => name))
  await expect(rail.locator('.solar-flow-step[data-status="ready"]')).toHaveCount(2)
  const blocked = rail.locator('.solar-flow-step[data-status="blocked"]')
  await expect(blocked).toHaveCount(9)
  for (const row of await blocked.all()) {
    const button = row.locator('button')
    await expect(button).toBeDisabled()
    await expect(button).toHaveAccessibleDescription('This drawing has no saved solar design yet')
  }
  if (!phone) return
  await expect(rail).toBeVisible()
  await expect(page.locator('[data-testid="project-solar-flow-reason"]')).toHaveCount(0)
  const host = page.locator('.solar-flow-host')
  const seat = page.locator('.solar-flow-seat')
  const scroll = await seat.count() ? seat : host
  const initial = await scroll.evaluate(el => el.scrollTop)
  await expect.poll(() => scroll.evaluate(el => {
    const s = getComputedStyle(el)
    return ['auto', 'scroll'].includes(s.overflowY) && el.clientHeight > 0 && el.scrollHeight > el.clientHeight
  })).toBe(true)
  // Scroll the owning region, never the page; disabled steps remain readable.
  const expectInside = async row => {
    await expect.poll(() => row.evaluate(el => {
      const owner = el.closest('.solar-flow-seat') || el.closest('.solar-flow-host')
      const b = el.getBoundingClientRect(), p = owner.getBoundingClientRect()
      const top = p.top + owner.clientTop
      return b.top >= top && b.bottom <= top + owner.clientHeight
        && b.top >= 0 && b.bottom <= innerHeight
    })).toBe(true)
  }
  const first = rail.locator('.solar-flow-step').first()
  const last = rail.locator('.solar-flow-step').last()
  await first.evaluate(el => {
    const owner = el.closest('.solar-flow-seat') || el.closest('.solar-flow-host')
    owner.scrollTop += el.getBoundingClientRect().top - (owner.getBoundingClientRect().top + owner.clientTop)
  })
  await expectInside(first)
  await scroll.evaluate(el => { el.scrollTop = el.scrollHeight })
  // Workspace tools follow the rail, so align the last row within the viewport.
  await last.evaluate(el => {
    const owner = el.closest('.solar-flow-seat') || el.closest('.solar-flow-host')
    owner.scrollTop += el.getBoundingClientRect().bottom
      - (owner.getBoundingClientRect().top + owner.clientTop + owner.clientHeight)
  })
  await expectInside(last)
  await info.attach('solar-phone-flow-scroll', { body:JSON.stringify(await scroll.evaluate(el => ({
    height:el.clientHeight, contentHeight:el.scrollHeight, scrollTop:el.scrollTop,
    first:el.querySelector('.solar-flow-step').textContent,
    last:[...el.querySelectorAll('.solar-flow-step')].at(-1).textContent,
  })), null, 2), contentType:'application/json' })
  await scroll.evaluate((el, top) => { el.scrollTop = top }, initial)
  await expect.poll(() => scroll.evaluate(el => el.scrollTop)).toBe(initial)
}

async function expectPhoneViewControls(page, info, phase) {
  const measure = () => page.evaluate(() => {
    const rect = selector => {
      const b = document.querySelector(selector).getBoundingClientRect()
      return { x:b.x, y:b.y, right:b.right, bottom:b.bottom, width:b.width, height:b.height }
    }
    const canvas = rect('.viewer-canvas'), viewer = rect('.viewer-wrap'), command = rect('.bar-dock')
    return { width:innerWidth, height:innerHeight, scroll:document.documentElement.scrollWidth,
      profile:rect('.environment-strip'), view:rect('.cockpit-view'), find:rect('.drawing-navigation'),
      overview:rect('.cad-overview'),
      canvas, viewer, canvasVisibleHeight:Math.max(0,
        Math.min(canvas.bottom, viewer.bottom, command.y, innerHeight) - Math.max(canvas.y, viewer.y, 0)),
      solarFlow:document.querySelector('.solar-flow-host') ? {
        host:rect('.solar-flow-host'),
        viewport:rect(document.querySelector('.solar-flow-seat') ? '.solar-flow-seat' : '.solar-flow-host'),
      } : null,
      command, input:rect('.bar-command-line .bar-field'),
      commandControls:rect('.bar-command-line .bar-controls') }
  })
  await info.attach(`${phase}-phone-view-initial-geometry`, { body:JSON.stringify(await measure(), null, 2), contentType:'application/json' })
  await expect.poll(async () => {
    const g = await measure()
    return g.view.y >= g.profile.bottom && g.find.y >= g.view.bottom
      && g.find.bottom <= g.overview.y
      && g.overview.bottom <= g.command.y
  }).toBe(true)
  const geometry = await measure()
  await info.attach(`${phase}-phone-view-geometry`, { body:JSON.stringify(geometry, null, 2), contentType:'application/json' })
  expect(geometry.scroll).toBeLessThanOrEqual(geometry.width)
  expect(geometry.canvas.height).toBeGreaterThanOrEqual(100)
  expect(geometry.canvasVisibleHeight).toBeGreaterThanOrEqual(100)
  expect(geometry.canvas.width).toBeGreaterThan(0)
  for (const b of [geometry.view, geometry.find, geometry.overview, geometry.command, geometry.input, geometry.commandControls]) {
    expect(b.width).toBeGreaterThan(0)
    expect(b.height).toBeGreaterThan(0)
    expect(b.x).toBeGreaterThanOrEqual(0)
    expect(b.y).toBeGreaterThanOrEqual(0)
    expect(b.right).toBeLessThanOrEqual(geometry.width + 1)
    expect(b.bottom).toBeLessThanOrEqual(geometry.height + 1)
  }
  expect(geometry.input.bottom).toBeLessThanOrEqual(geometry.commandControls.y)
  const view = page.getByRole('toolbar', { name:'View', exact:true })
  const zoom = view.getByRole('button', { name:'Zoom in', exact:true })
  const zoomOut = view.getByRole('button', { name:'Zoom out', exact:true })
  const fit = view.getByRole('button', { name:'Fit drawing to view', exact:true })
  const find = page.getByRole('combobox', { name:'Find in drawing', exact:true })
  const submit = page.locator('.drawing-find-submit')
  // These centers used to hit Project and Workspace profile in the dock.
  for (const control of [zoom, zoomOut, fit]) {
    await expectCenterHit(control)
    await control.click()
  }
  for (const control of [find, submit]) await expectCenterHit(control)
  await find.click()
  await expect(find).toBeFocused()
  await submit.click()
  await expectCenterHit(page.locator('.bar-command-line .bar-field'))
  const overview = page.locator('.cad-overview')
  const toggle = overview.locator('.cad-overview-toggle')
  await expectCenterHit(toggle)
  if (await overview.getAttribute('data-collapsed') === 'false') {
    const map = overview.getByRole('button', { name:'Drawing overview', exact:true })
    await expect(overview.locator('.cad-overview-key')).toBeVisible()
    await expect(overview.locator('.cad-overview-key')).toContainText('selected')
    await expectCenterHit(map)
    await map.click()
    await expect(map).toBeFocused()
  }
  const state = await overview.getAttribute('data-collapsed') === 'true' ? 'collapsed' : 'expanded'
  await page.screenshot({ path:info.outputPath(`${phase}-phone-overview-${state}.png`) })
}

async function exercisePhoneOverview(page, info, phase) {
  const overview = page.locator('.cad-overview')
  const toggle = overview.locator('.cad-overview-toggle')
  await expect(overview).toBeVisible()
  // Deliberately visit both states; do not depend on the session's saved state.
  if (await overview.getAttribute('data-collapsed') === 'true') {
    await expectCenterHit(toggle)
    await toggle.click()
  }
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  await expectPhoneViewControls(page, info, `${phase}-expanded`)
  await expectCenterHit(toggle)
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await expect(overview.getByRole('button', { name:'Drawing overview', exact:true })).toBeHidden()
  await expectPhoneViewControls(page, info, `${phase}-collapsed`)
}

async function exercisePhoneViewControls(page, info, phase) {
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg')
  await exercisePhoneOverview(page, info, phase)
  const toolRail = page.getByRole('button', { name:'Tool rail', exact:true })
  await expect(toolRail).toHaveAttribute('aria-expanded', 'false')
  await toolRail.click()
  await expect(toolRail).toHaveAttribute('aria-expanded', 'true')
  await toolRail.click()
  await expect(toolRail).toHaveAttribute('aria-expanded', 'false')
  await exercisePhoneOverview(page, info, `${phase}-rail-closed`)
  await page.getByRole('navigation', { name:'Workspace controls', exact:true })
    .getByRole('button', { name:'Studio', exact:true }).click()
  const studio = page.getByRole('region', { name:'Studio sidebar', exact:true })
  await expect(studio).toBeVisible()
  await studio.getByRole('button', { name:'Close Studio', exact:true }).click()
  await expect(studio).toBeHidden()
  await exercisePhoneOverview(page, info, `${phase}-studio-closed`)
}

async function expectNaturalPanelFit(ribbon, info) {
  const measure = () => ribbon.evaluate(el => {
    const band = el.getBoundingClientRect()
    const host = el.querySelector('.ribbon-panels')
    return { tab:el.dataset.tab, left:band.left, right:band.right, top:band.top, bottom:band.bottom,
      clientWidth:host.clientWidth, scrollWidth:host.scrollWidth,
      panels:[...host.querySelectorAll('.ribbon-cluster')].map(panel => {
        const b = panel.getBoundingClientRect()
        return { name:panel.getAttribute('aria-label'), hidden:panel.hidden,
          left:b.left, right:b.right, top:b.top, bottom:b.bottom, width:b.width }
      }) }
  })
  await expect(ribbon.getByRole('button', { name:'More panels', exact:true })).toBeHidden()
  await expect.poll(async () => {
    const g = await measure()
    return g.panels.length > 0 && g.scrollWidth <= g.clientWidth + 1
      && g.panels.every(p => !p.hidden && p.width > 0 && p.left >= g.left
        && p.right <= g.right + 1 && p.top >= g.top && p.bottom <= g.bottom + 1)
  }).toBe(true)
  const geometry = await measure()
  await info.attach(`cad-${geometry.tab}-natural-fit`, { body:JSON.stringify(geometry, null, 2), contentType:'application/json' })
}

async function expectDraftingBands(page, info, phase) {
  const measure = () => page.evaluate(() => {
    const rect = selector => {
      const el = document.querySelector(selector)
      if (!el) throw new Error(`Missing geometry target: ${selector}`)
      const b = el.getBoundingClientRect()
      return { x:b.x, y:b.y, width:b.width, height:b.height, right:b.right, bottom:b.bottom }
    }
    return { width:innerWidth, height:innerHeight, scroll:document.documentElement.scrollWidth,
      header:rect('header.top'), ribbon:rect('#drafting-ribbon'), toolbar:rect('.viewer-toolbar'),
      controls:rect('.environment-strip'), command:rect('.bar-dock') }
  })
  // The ribbon and toolbar flow below the measured, potentially wrapped header.
  // Navigation clearing the toolbar alone does not prove those bands are usable.
  await info.attach(`${phase}-initial-geometry`, { body:JSON.stringify(await measure(), null, 2), contentType:'application/json' })
  // Wait for the existing ResizeObservers to publish the active profile's band.
  await expect.poll(async () => {
    const g = await measure()
    return g.ribbon.y >= g.header.bottom && g.toolbar.y >= g.ribbon.bottom
      && g.controls.y >= Math.max(g.header.bottom, g.ribbon.bottom, g.toolbar.bottom)
      && g.controls.bottom <= g.command.y
  }).toBe(true)
  const geometry = await measure()
  await info.attach(`${phase}-geometry`, { body:JSON.stringify(geometry, null, 2), contentType:'application/json' })
  expect(geometry.scroll).toBeLessThanOrEqual(geometry.width)
  expect(geometry.controls.x).toBe(0)
  expect(geometry.controls.width).toBe(geometry.width)
  expect(geometry.ribbon.y).toBeGreaterThanOrEqual(geometry.header.bottom)
  expect(geometry.toolbar.y).toBeGreaterThanOrEqual(geometry.ribbon.bottom)
  for (const b of [geometry.header, geometry.ribbon, geometry.toolbar, geometry.controls, geometry.command]) {
    expect(b.width).toBeGreaterThan(0)
    expect(b.height).toBeGreaterThan(0)
    expect(b.x).toBeGreaterThanOrEqual(0)
    expect(b.y).toBeGreaterThanOrEqual(0)
    expect(b.right).toBeLessThanOrEqual(geometry.width + 1)
    expect(b.bottom).toBeLessThanOrEqual(geometry.height + 1)
  }
  const controls = page.getByRole('navigation', { name:'Workspace controls', exact:true }).locator('button, select')
  for (const control of await controls.all()) await expectCenterHit(control)
  await expectCenterHit(page.locator('.bar-command-line .bar-field'))
}

for (const [size, viewport] of [['desktop', { width: 1440, height: 709 }], ['phone', { width: 390, height: 844 }]]) {
  test(`fresh workspace header opens and dismisses Projects at ${size}`, async ({ page }, info) => {
    page.setDefaultTimeout(15_000)
    await page.setViewportSize(viewport)
    const mutations = await backend(page)
    await page.goto('/app?surface=browser')
    const environment = page.locator('.workspace-environment')
    const chooser = page.locator('.environment-chooser')
    const projects = page.getByRole('button', { name:/^Projects: change project\./ })
    const picker = page.locator('.proj-menu[role="menu"]')
    const attachGeometry = async phase => {
      const geometry = await page.evaluate(() => {
        const rect = selector => {
          const el = document.querySelector(selector)
          if (!el) return null
          const b = el.getBoundingClientRect()
          return { x:b.x, y:b.y, width:b.width, height:b.height, right:b.right, bottom:b.bottom }
        }
        const trigger = document.querySelector('.proj-chip[aria-haspopup="menu"]')
        const b = trigger.getBoundingClientRect()
        const hit = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2)
        return { width:innerWidth, height:innerHeight,
          scroll:Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
          entered:document.querySelector('.workspace-environment').dataset.entered,
          drawerTop:getComputedStyle(document.documentElement).getPropertyValue('--drawer-top'),
          header:rect('header.top'), backdrop:rect('.environment-backdrop'),
          projects:rect('.proj-chip[aria-haspopup="menu"]'), picker:rect('.proj-menu'),
          chooser:rect('.environment-chooser'),
          projectsOwnsCenter:hit === trigger || trigger.contains(hit),
          centerHit:hit ? { tag:hit.tagName, className:hit.className } : null }
      })
      await info.attach(`${size}-${phase}-geometry`, { body:JSON.stringify(geometry, null, 2), contentType:'application/json' })
      await info.attach(`${size}-${phase}`, { body:await page.screenshot(), contentType:'image/png' })
      expect(geometry.width).toBe(viewport.width)
      expect(geometry.height).toBe(viewport.height)
      expect(geometry.scroll).toBeLessThanOrEqual(geometry.width)
      return geometry
    }
    // Playwright's page fixture has a fresh context. Do not enter Studio to
    // uncover the header: this is the chooser's initial, intercepting state.
    await expect(environment).toHaveAttribute('data-entered', 'false')
    await expect(chooser).toBeVisible()
    await expect(projects).toBeEnabled()
    const initial = await attachGeometry('initial')
    expect(initial.backdrop.y).toBeGreaterThanOrEqual(initial.header.bottom)
    await expectCenterHit(projects)
    const projectLabel = await projects.textContent()
    await projects.click()
    await expect(projects).toHaveAttribute('aria-expanded', 'true')
    await expect(picker).toBeVisible()
    await expect(environment).toHaveAttribute('data-entered', 'false')
    // Focus a real picker control without creating or opening a project.
    const draft = picker.getByRole('textbox', { name:'New project', exact:true })
    await expectCenterHit(draft)
    await draft.click()
    await expect(draft).toBeFocused()
    await attachGeometry('picker-open')
    await expectCenterHit(projects)
    await projects.click()
    await expect(projects).toHaveAttribute('aria-expanded', 'false')
    await expect(picker).toBeHidden()
    await expect(projects).toHaveText(projectLabel)
    await expect(environment).toHaveAttribute('data-entered', 'false')
    // The backdrop still protects the workspace, while the chooser can enter it.
    const open = chooser.getByRole('button', { name:'Open project or material', exact:true })
    await expectCenterHit(open)
    await open.click()
    await expect(environment).toHaveAttribute('data-entered', 'true')
    const sidebar = page.getByRole('region', { name:'Project sidebar', exact:true })
    await expect(sidebar).toBeVisible()
    await sidebar.getByRole('button', { name:'Close Project', exact:true }).click()
    await expect(sidebar).toBeHidden()
    await page.getByRole('navigation', { name:'Workspace controls', exact:true })
      .getByRole('button', { name:'Choose', exact:true }).click()
    await expect(environment).toHaveAttribute('data-entered', 'false')
    await expectCenterHit(open)
    await expectCenterHit(projects)
    await attachGeometry('chooser-restored')
    await expect(projects).toHaveText(projectLabel)
    expect(mutations).toEqual([])
  })
}

for (const [size, viewport] of [['desktop', { width: 1440, height: 900 }], ['phone', { width: 390, height: 844 }]]) {
  test(`Studio monitor dismissal at ${size}`, async ({ page }, info) => {
    test.setTimeout(60_000)
    page.setDefaultTimeout(15_000)
    await page.setViewportSize(viewport)
    const mutations = await backend(page)
    await page.goto('/app?surface=browser')
    const opener = page.getByRole('navigation', { name:'Workspace controls', exact:true })
      .getByRole('button', { name:'Studio', exact:true })
    const sidebar = page.getByRole('region', { name:'Studio sidebar', exact:true })
    const monitor = sidebar.getByRole('heading', { name:/^Job monitor/ })
    const expectNoOverflow = async () => {
      await expect.poll(() => page.evaluate(() =>
        Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) <= innerWidth
      )).toBe(true)
    }
    const expectOpenMonitor = async phase => {
      await expect(sidebar).toBeVisible()
      await expect(monitor).toBeVisible()
      await expect(sidebar.getByRole('button', { name:'Collapse the job monitor', exact:true })).toHaveCount(0)
      await expectNoOverflow()
      await info.attach(`studio-monitor-${size}-${phase}`, { body:await page.screenshot(), contentType:'image/png' })
    }
    await opener.click()
    await expectOpenMonitor('open')
    const close = sidebar.getByRole('button', { name:'Close Studio', exact:true })
    await expectCenterHit(close)
    await close.click()
    await expect(sidebar).toBeHidden()
    await expect(monitor).toBeHidden()
    await expect(opener).toBeFocused()
    await expectNoOverflow()
    await opener.click()
    await expectOpenMonitor('reopened')
    expect(mutations).toEqual([])
  })
}

for (const [size, viewport] of [['desktop', { width: 1440, height: 900 }], ['wide-desktop', { width: 1600, height: 1000 }], ['phone', { width: 390, height: 844 }]]) {
  test(`docked workspace chrome remains usable at ${size}`, async ({ page }, info) => {
    test.setTimeout(120_000)
    page.setDefaultTimeout(15_000)
    await page.setViewportSize(viewport)
    const mutations = await backend(page)
    await page.goto('/app?surface=browser')
    const controls = page.getByRole('navigation', { name: 'Workspace controls' })
    await controls.getByRole('button', { name: 'Studio', exact: true }).click()
    const rail = page.getByRole('region', { name: 'Studio sidebar', exact: true })
    await expect(rail).toBeVisible()
    await expect(page.locator('nav.tc-product-nav')).toBeHidden()
    await expect(page.locator('.environment-backdrop')).toHaveCSS('opacity', '0')
    await expect(page.locator('.environment-chooser')).toHaveCSS('opacity', '0')
    await page.screenshot({ path: info.outputPath(`docked-${size}-open.png`) })
    const geometry = await page.evaluate(() => {
      const rect = selector => {
        const el = document.querySelector(selector)
        if (!el) throw new Error(`Missing geometry target: ${selector}`)
        const b = el.getBoundingClientRect(), s = getComputedStyle(el)
        return { x:b.x, y:b.y, width:b.width, height:b.height, right:b.right, bottom:b.bottom, font:s.fontFamily, background:s.backgroundColor }
      }
      return { width:innerWidth, scroll:document.documentElement.scrollWidth,
        header:rect('header.top'), panel:rect('#project-workspace-activity'), command:rect('.bar-dock'), controls:rect('.environment-strip') }
    })
    await info.attach('geometry', { body: JSON.stringify(geometry, null, 2), contentType:'application/json' })
    expect(geometry.scroll).toBeLessThanOrEqual(viewport.width)
    expect(geometry.header.x).toBe(0)
    expect(geometry.header.width).toBeGreaterThanOrEqual(viewport.width - 1)
    for (const rect of [geometry.panel, geometry.command, geometry.controls]) {
      expect(rect.x).toBeGreaterThanOrEqual(0)
      expect(rect.y).toBeGreaterThanOrEqual(0)
      expect(rect.right).toBeLessThanOrEqual(viewport.width + 1)
      expect(rect.bottom).toBeLessThanOrEqual(viewport.height + 1)
    }
    await rail.getByRole('button', { name:'Close Studio', exact:true }).click()
    await expect(rail).toBeHidden()
    await expect(controls.getByRole('button', { name:'Studio', exact:true })).toBeFocused()
    await expect(page.locator('header.top')).toBeVisible()
    await expect(page.locator('.bar-dock')).toBeVisible()
    expect(await page.locator('.app[data-studio-shell]').getAttribute('inert')).toBeNull()
    await controls.getByRole('button', { name:'Project', exact:true }).click()
    await expect(page.getByRole('region', { name:'Project sidebar', exact:true })).toBeVisible()
    await page.getByRole('button', { name:'Close Project', exact:true }).click()
    await controls.getByRole('combobox', { name:'Workspace profile', exact:true }).selectOption('solar')
    await controls.getByRole('button', { name:'Studio', exact:true }).click()
    await expect(page.locator('.workspace-environment')).toHaveAttribute('data-profile', 'solar')
    await expect(page.locator('.workspace-environment')).toHaveAttribute('data-entered', 'true')
    await expect(page.locator('.app[data-studio-shell]')).toHaveAttribute('data-surface', 'solar')
    await expect(page.locator('nav.tc-product-nav')).toBeHidden()
    await page.screenshot({ path:info.outputPath(`docked-${size}-solar.png`) })
    if (await rail.isVisible()) {
      const close = rail.getByRole('button', { name:'Close Studio', exact:true })
      await expectCenterHit(close)
      await close.click()
    }
    await expectDraftingBands(page, info, 'solar')
    await expectPopulatedSolar(page, info, size === 'phone')
    if (size === 'phone') await exercisePhoneViewControls(page, info, 'solar')
    await controls.getByRole('combobox', { name:'Workspace profile', exact:true }).selectOption('cad')
    await controls.getByRole('button', { name:'Studio', exact:true }).click()
    const environment = page.locator('.workspace-environment')
    await expect(environment).toHaveAttribute('data-profile', 'cad')
    await expect(environment).toHaveAttribute('data-entered', 'true')
    if (await rail.isVisible()) {
      const close = rail.getByRole('button', { name:'Close Studio', exact:true })
      await expectCenterHit(close)
      await close.click()
    }
    await expect(rail).toBeHidden()
    const ribbon = page.locator('#drafting-ribbon')
    await expect(ribbon).toBeVisible()
    const manage = page.getByRole('tablist', { name:'Ribbon', exact:true }).getByRole('tab', { name:'Manage', exact:true })
    await manage.click()
    await expectCenterHit(manage)
    await expect(manage).toHaveAttribute('aria-selected', 'true')
    await expect(environment).toHaveAttribute('data-entered', 'true')
    await expectDraftingBands(page, info, 'cad-manage')
    if (size === 'phone') await exercisePhoneViewControls(page, info, 'cad-manage')
    await page.screenshot({ path:info.outputPath(`docked-${size}-cad-manage.png`) })
    const more = ribbon.getByRole('button', { name:'More panels', exact:true })
    const hiddenPanels = ribbon.locator('.ribbon-cluster[hidden]')
    // Manage can fit naturally on desktop. Probe the other real, enabled
    // drafting tabs for overflow without resizing or changing their contents.
    if (size === 'phone') await expect(more).toBeVisible()
    if (!(await more.isVisible())) {
      await expectNaturalPanelFit(ribbon, info)
      const tabs = page.getByRole('tablist', { name:'Ribbon', exact:true }).getByRole('tab')
      for (const tab of await tabs.all()) {
        if (await tab.isDisabled() || await tab.getAttribute('aria-selected') === 'true') continue
        await tab.click()
        await expectCenterHit(tab)
        await expect(tab).toHaveAttribute('aria-selected', 'true')
        await expect(environment).toHaveAttribute('data-entered', 'true')
        await expectDraftingBands(page, info, `cad-${await ribbon.getAttribute('data-tab')}`)
        if (await more.isVisible()) break
        await expectNaturalPanelFit(ribbon, info)
      }
    }
    if (await more.isVisible()) {
      await expect(more).toHaveAttribute('aria-expanded', 'false')
      await expect.poll(() => hiddenPanels.count()).toBeGreaterThan(0)
      await expectCenterHit(more)
      await more.click()
      await expect(more).toHaveAttribute('aria-expanded', 'true')
      await expect(ribbon).toHaveAttribute('data-overflow-open', 'true')
      await expect(hiddenPanels).toHaveCount(0)
      const panels = page.locator('#drafting-ribbon-panels')
      await expect(panels.locator('.ribbon-cluster').first()).toBeVisible()
      await expectDraftingBands(page, info, 'cad-expanded')
      const expandedGeometry = await panels.evaluate(el => {
        const b = el.getBoundingClientRect()
        const controls = document.querySelector('.environment-strip').getBoundingClientRect()
        const command = document.querySelector('.bar-dock').getBoundingClientRect()
        return { x:b.x, y:b.y, width:b.width, height:b.height, right:b.right, bottom:b.bottom,
          controlsBottom:controls.bottom, commandTop:command.top }
      })
      await info.attach('cad-expanded-panels-geometry', { body:JSON.stringify(expandedGeometry, null, 2), contentType:'application/json' })
      expect(expandedGeometry.height).toBeGreaterThan(0)
      expect(expandedGeometry.y).toBeGreaterThanOrEqual(expandedGeometry.controlsBottom)
      expect(expandedGeometry.bottom).toBeLessThanOrEqual(expandedGeometry.commandTop + 1)
      await page.screenshot({ path:info.outputPath(`docked-${size}-cad-expanded.png`) })
      await expectCenterHit(more)
      await more.click()
      await expect(more).toHaveAttribute('aria-expanded', 'false')
      await expect(ribbon).not.toHaveAttribute('data-overflow-open', 'true')
      await expect.poll(() => hiddenPanels.count()).toBeGreaterThan(0)
      await expectCenterHit(more)
    }
    await manage.click()
    await expect(manage).toHaveAttribute('aria-selected', 'true')
    await expectCenterHit(manage)
    await expect(environment).toHaveAttribute('data-entered', 'true')
    await expectDraftingBands(page, info, 'cad-collapsed')
    if (size === 'phone') await exercisePhoneOverview(page, info, 'cad-collapsed')
    await page.screenshot({ path:info.outputPath(`docked-${size}-cad.png`) })
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width)
    expect(mutations.filter(request => /\/api\/(run|drawings\/.*\/(undo|redo|intake)|projects\/.*\/versions)/.test(request))).toEqual([])
  })
}
