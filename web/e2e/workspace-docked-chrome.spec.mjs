import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

// Actual Next.js rendering with fixture-backed APIs. No live account mutations.
const CORS = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' }
async function backend(page) {
  const state = makeCatProofState()
  await page.addInitScript(() => {
    localStorage.setItem('leaf.jwt', 'fixture-token')
    localStorage.setItem('leaf.coach.dismissed.v1', '1')
  })
  await page.route('http://leaf-proof.invalid/api/**', async route => {
    const req = route.request(), url = new URL(req.url())
    if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: CORS })
    const reply = catProofResponse({ method: req.method(), path: url.pathname,
      query: Object.fromEntries(url.searchParams), body: req.postData() ? req.postDataJSON() : {} }, state)
    await route.fulfill({ status: reply.status, headers: CORS, contentType: 'application/json', body: JSON.stringify(reply.body ?? null) })
  })
}

async function expectCenterHit(control) {
  await expect(control).toBeVisible()
  await expect.poll(() => control.evaluate(el => {
    const b = el.getBoundingClientRect()
    const hit = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2)
    return b.width > 0 && b.height > 0 && b.x >= 0 && b.y >= 0
      && b.right <= innerWidth && b.bottom <= innerHeight && (hit === el || el.contains(hit))
  })).toBe(true)
}

async function expectPhoneViewControls(page, info, phase) {
  const measure = () => page.evaluate(() => {
    const rect = selector => {
      const b = document.querySelector(selector).getBoundingClientRect()
      return { x:b.x, y:b.y, right:b.right, bottom:b.bottom, width:b.width, height:b.height }
    }
    return { width:innerWidth, height:innerHeight, scroll:document.documentElement.scrollWidth,
      profile:rect('.environment-strip'), view:rect('.cockpit-view'), find:rect('.drawing-navigation'),
      command:rect('.bar-dock'), input:rect('.bar-command-line .bar-field'),
      commandControls:rect('.bar-command-line .bar-controls') }
  })
  await expect.poll(async () => {
    const g = await measure()
    return g.view.y >= g.profile.bottom && g.find.y >= g.view.bottom
      && g.find.bottom <= g.command.y
  }).toBe(true)
  const geometry = await measure()
  await info.attach(`${phase}-phone-view-geometry`, { body:JSON.stringify(geometry, null, 2), contentType:'application/json' })
  expect(geometry.scroll).toBeLessThanOrEqual(geometry.width)
  for (const b of [geometry.view, geometry.find, geometry.command, geometry.input, geometry.commandControls]) {
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
  const fit = view.getByRole('button', { name:'Fit drawing to view', exact:true })
  const find = page.getByRole('combobox', { name:'Find in drawing', exact:true })
  const submit = page.locator('.drawing-find-submit')
  // These centers used to hit Project and Workspace profile in the dock.
  for (const control of [zoom, fit, find, submit]) await expectCenterHit(control)
  await zoom.click()
  await fit.click()
  await find.click()
  await expect(find).toBeFocused()
  await submit.click()
  await expectCenterHit(page.locator('.bar-command-line .bar-field'))
}

async function exercisePhoneViewControls(page, info, phase) {
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg')
  await expectPhoneViewControls(page, info, phase)
  const toolRail = page.getByRole('button', { name:'Tool rail', exact:true })
  await expect(toolRail).toHaveAttribute('aria-expanded', 'false')
  await toolRail.click()
  await expect(toolRail).toHaveAttribute('aria-expanded', 'true')
  await toolRail.click()
  await expect(toolRail).toHaveAttribute('aria-expanded', 'false')
  await expectPhoneViewControls(page, info, `${phase}-rail-closed`)
  await page.getByRole('navigation', { name:'Workspace controls', exact:true })
    .getByRole('button', { name:'Studio', exact:true }).click()
  const studio = page.getByRole('region', { name:'Studio sidebar', exact:true })
  await expect(studio).toBeVisible()
  await studio.getByRole('button', { name:'Close Studio', exact:true }).click()
  await expect(studio).toBeHidden()
  await expectPhoneViewControls(page, info, `${phase}-studio-closed`)
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

for (const [size, viewport] of [['desktop', { width: 1440, height: 900 }], ['wide-desktop', { width: 1600, height: 1000 }], ['phone', { width: 390, height: 844 }]]) {
  test(`docked workspace chrome remains usable at ${size}`, async ({ page }, info) => {
    test.setTimeout(120_000)
    page.setDefaultTimeout(15_000)
    await page.setViewportSize(viewport)
    await backend(page)
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
    if (size === 'phone') await expectPhoneViewControls(page, info, 'cad-collapsed')
    await page.screenshot({ path:info.outputPath(`docked-${size}-cad.png`) })
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width)
  })
}
