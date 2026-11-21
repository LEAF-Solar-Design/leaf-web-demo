// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import React, { useState } from 'react'
import { readFileSync } from 'node:fs'
import { parseExpression } from '@babel/parser'
import { AgentPanel, CheckoutChip, CockpitStatus, DrawingTabLabel, ShellDiagnostics, ShellKpis, StatusToggles } from './DrawingCockpit.jsx'
import DraftingRibbon from './DraftingRibbon.jsx'
import DrawingNavigationTools, { DrawingObjectsMount } from './DrawingNavigationTools.jsx'
import { projectSolarOpenStatus } from '../solar/solarProjectAppModel.js'
import { ShellAccountMenu } from '../App.jsx'
import CostTransparencyPanel from '../components/CostTransparencyPanel.jsx'
import ClaudeAccountPanel from '../components/ClaudeAccountPanel.jsx'
import RoutePanel from '../components/RoutePanel.jsx'
import { isCustomerMode } from '../lib/customerMode.js'
import { ServiceStatus } from '../components/DetailsDrawer.jsx'

// Exercise App's actual JSX expressions with explicit bindings, without
// mounting its backend/session orchestration.
const appSource = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
function customerBindings(studioShell, tier = 'free') {
  const bindings = { studioShell, isCustomerMode, session: { session: { role: 'user' } }, tier }
  const definition = appSource.match(/const studioCustomerMode = ([^\r\n]+)/)[1]
  return { ...bindings, studioCustomerMode: appExpression(definition, bindings) }
}
function appExpression(code, bindings) {
  const tree = parseExpression(code, { plugins: ['jsx'] })
  function evaluate(node) {
    if (node.type === 'JSXText') return node.value
    if (node.type === 'JSXExpressionContainer') return evaluate(node.expression)
    if (node.type === 'JSXFragment') return node.children.map(evaluate)
    if (node.type === 'LogicalExpression' && node.operator === '&&') return evaluate(node.left) && evaluate(node.right)
    if (node.type === 'ConditionalExpression') return evaluate(node.test) ? evaluate(node.consequent) : evaluate(node.alternate)
    if (node.type === 'JSXElement') {
      const opening = node.openingElement
      const name = code.slice(opening.name.start, opening.name.end)
      const component = /^[a-z]/.test(name) ? name : new Function(...Object.keys(bindings), `return ${name}`)(...Object.values(bindings))
      const props = Object.fromEntries(opening.attributes.map(attr => [attr.name.name, attr.value ? evaluate(attr.value) : true]))
      return React.createElement(component, props, ...node.children.map(evaluate))
    }
    return new Function(...Object.keys(bindings), `return (${code.slice(node.start, node.end)})`)(...Object.values(bindings))
  }
  return evaluate(tree)
}

const navigationFixture = vi.hoisted(() => ({
  index: { drawingKey: 'test', records: [], byId: new Map(), byHandle: new Map(), intake: { source: 'engine' } },
  focusId: null,
}))
vi.mock('./DrawingObjectsContext.jsx', async (original) => ({ ...await original(), useDrawingObjects: () => navigationFixture }))
vi.mock('../cadedit/EngineSessionProvider.jsx', async (original) => ({ ...await original(), useEngineSessionOptional: () => null }))
vi.mock('./SurfaceFrame.jsx', async (original) => ({ ...await original(), useSurfaceFrame: () => ({ contract: { ground: 'drawing' } }) }))
vi.mock('./studioGround.js', async (original) => ({ ...await original(), useStudioGround: () => ({}) }))

afterEach(cleanup)

describe('quiet Studio shell', () => {
  it('defines customer mode once behind the Studio shell gate', () => {
    expect(appSource.match(/\bisCustomerMode\s*\(/g)).toHaveLength(1)
    expect(appSource).toMatch(/const studioCustomerMode = studioShell && isCustomerMode\(\{ \.\.\.session\.session, tier \}\)/)
  })

  it('keeps service, build, tool and plan details visible for a non-Studio pro user', () => {
    const bindings = { ...customerBindings(false, 'pro'), ServiceStatus, ShellDiagnostics,
      mock: false, health: { aps_live: true }, healthError: null, authRequired: false,
      studioGround: null, capCount: 63, gateTier: 'pro', usage: { today: { usd_est: 0 } }, __BUILD_HASH__: 'test-build' }
    const service = appSource.split('\n').find(line => line.includes('&& <ServiceStatus ')).trim().slice(1, -1)
    const build = appSource.split('\n').find(line => line.includes('>Build <span')).trim().slice(1, -1)
    const diagnostics = appSource.match(/<ShellDiagnostics\b[\s\S]*?\/>/)[0]
    render(<footer>{appExpression(service, bindings)}{appExpression(diagnostics, bindings)}{appExpression(build, bindings)}</footer>)
    expect(screen.getByText('Service live')).toBeTruthy()
    expect(screen.getByText('Build')).toBeTruthy()
    expect(screen.getByText('test-build')).toBeTruthy()
    expect(screen.getByText('Tools: 63 available · Plan: pro')).toBeTruthy()
  })

  it.each([false, true])('keeps cost and Claude panels reachable for customer mode (Studio %s)', (studioShell) => {
    const bindings = { ...customerBindings(studioShell),
      mock: false, knownGuest: true, signedOut: true, grant: null, grantLoading: false,
      grantBusy: false, grantErr: null, claudeOpen: false, setClaudeOpen: vi.fn(), onLinkClaude: vi.fn(), onUnlinkClaude: vi.fn(),
      // Main's headerChromeFor decides account-chip visibility (tested with App); this proves the shell keeps
      // both panels reachable whenever that gate shows them.
      headerChrome: { cost: true, accounts: true },
      CostTransparencyPanel, ClaudeAccountPanel }
    const panelsCode = appSource.match(/const accountPanels = ([\s\S]*?)\n  <\/>/)[1] + '\n  </>'
    bindings.accountPanels = appExpression(panelsCode, bindings)
    const inline = appSource.split('\n').find(line => line.includes('&& accountPanels}')).trim().slice(1, -1)
    const menu = appSource.match(/customerPanels=\{([^\n]+)\}>/)[1]
    const { container } = render(<ShellAccountMenu studioShell={studioShell} customerPanels={appExpression(menu, bindings)}>
      <div className="who">{appExpression(inline, bindings)}</div>
    </ShellAccountMenu>)
    if (studioShell) fireEvent.click(screen.getByText('Account'))
    expect(container.querySelector('.cost-transparency > button')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Claude accounts/ })).toBeTruthy()
    expect(container.querySelectorAll('.claude-acct')).toHaveLength(1)
    expect(!!container.querySelector('.shell-avatar-menu')).toBe(studioShell)
    if (!studioShell) expect(container.querySelector('.who > .claude-acct')).toBeTruthy()
  })

  it('keeps the CAD cockpit working controls mounted for customer mode', () => {
    const line = appSource.split('\n').find(line => line.includes('<SurfaceFrame.Cockpit'))
    const expression = line.trim().replace(/^\{([\s\S]*)\}$/, '$1')
    render(appExpression(expression, { studioShell: true, isCustomerMode, session: { session: {} }, tier: 'free', SurfaceFrame: { Cockpit: StatusToggles } }))
    expect(screen.getByRole('button', { name: /^Ortho mode/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /^Object snap/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Toggle fullscreen' })).toBeTruthy()
  })

  it.each(['project', 'cad', 'agent'])('keeps the generic placeholder for Studio profile %s', (profile) => {
    const expression = appSource.match(/placeholder=\{([^\n]+)\}/)[1]
    expect(appExpression(expression, { studioShell: true, surfaceSlots: { toolbar: { profile } } })).toBeUndefined()
    expect(appExpression(expression, { studioShell: true, surfaceSlots: { toolbar: { profile: 'solar' } } })).toBe('String every roof to the coldest-day Voc')
    expect(appExpression(expression, { studioShell: false, surfaceSlots: { toolbar: { profile: 'solar' } } })).toBeUndefined()
  })

  it.each([false, true])('limits customer cockpit diagnostic suppression to Studio (%s)', (studioShell) => {
    const expression = appSource.match(/<CockpitStatus customerMode=\{([^\n]+?)\} ground=/)[1]
    const customerMode = appExpression(expression, customerBindings(studioShell))
    const { container } = render(<CockpitStatus customerMode={customerMode} ground={null} viewerRef={{ current: null }} shown={{}} />)
    expect(!!container.querySelector('.cockpit-scale')).toBe(!studioShell)
    expect(!!container.querySelector('.cockpit-count')).toBe(!studioShell)
  })
  it('renders account controls directly outside Studio', () => {
    const { container } = render(<header><ShellAccountMenu studioShell={false}>
      <div className="who"><button>Customize</button><button>Details</button><button>Sign out</button><button>Add-ons</button></div>
    </ShellAccountMenu></header>)
    expect(container.querySelector('details')).toBeNull()
    expect(container.querySelector('header > .who')).toBeTruthy()
    for (const name of ['Customize', 'Details', 'Sign out', 'Add-ons']) {
      expect(screen.getByRole('button', { name })).toBeTruthy()
    }
  })
  it('names the avatar summary Account with a person glyph', () => {
    const { container } = render(<ShellAccountMenu studioShell><div className="who"><button>Details</button></div></ShellAccountMenu>)
    const summary = screen.getByText('Account').closest('summary')
    expect(summary).toHaveAccessibleName('Account')
    expect(summary.textContent).toBe('Account')
    expect(summary.querySelector('svg[aria-hidden="true"]')).toBeTruthy()
    expect(container.querySelector('details').open).toBe(false)
  })
  it('closes the avatar menu on Escape and returns focus to Account', () => {
    const { container } = render(<ShellAccountMenu studioShell><div className="who"><button>Details</button></div></ShellAccountMenu>)
    const menu = container.querySelector('details')
    fireEvent.click(menu.querySelector('summary'))
    expect(menu.open).toBe(true)
    const details = screen.getByRole('button', { name: 'Details' })
    details.focus()
    fireEvent.keyDown(details, { key: 'Escape' })
    expect(menu.open).toBe(false)
    expect(document.activeElement).toBe(menu.querySelector('summary'))
  })
  it('keeps the avatar menu open for internal focus and closes it when focus leaves', () => {
    const { container } = render(<><ShellAccountMenu studioShell><div className="who"><button>Details</button></div></ShellAccountMenu><button>Outside</button></>)
    const menu = container.querySelector('details')
    const summary = menu.querySelector('summary')
    fireEvent.click(summary)
    summary.focus()
    screen.getByRole('button', { name: 'Details' }).focus()
    expect(menu.open).toBe(true)
    screen.getByRole('button', { name: 'Outside' }).focus()
    expect(menu.open).toBe(false)
  })
  it('seats Objects beside Find and Layers in the same initially collapsed rail', () => {
    const { container } = render(<>
      <DrawingObjectsMount><details className="drawing-objects-panel"><summary>Objects</summary><p>Object list</p></details></DrawingObjectsMount>
      <DrawingNavigationTools navigation={{ syncScope: vi.fn() }} />
    </>)
    const rail = container.querySelector('.drawing-left-rail')
    expect(rail.querySelector('.drawing-objects-panel')).toBeTruthy()
    expect(rail.querySelectorAll('details').length).toBe(3)
    expect(Array.from(rail.querySelectorAll('details')).every((menu) => !menu.open)).toBe(true)
    expect(container.querySelectorAll('.drawing-objects-panel').length).toBe(1)
  })
  it('leaves the CAD command dock unwrapped without claiming Escape', () => {
    const { container } = render(<AgentPanel enabled={false}><input aria-label="CAD command" /></AgentPanel>)
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })
    expect(container.querySelector('.studio-agent-panel')).toBeNull()
    expect(screen.getByRole('textbox')).toBeTruthy()
  })
  it('starts Find and Layers closed in the left rail', () => {
    const { container } = render(<DrawingNavigationTools navigation={{ syncScope: vi.fn() }} />)
    expect(container.querySelector('.drawing-left-rail').dataset.collapsed).toBe('true')
    expect(Array.from(container.querySelectorAll('details')).every((menu) => !menu.open)).toBe(true)
    expect(screen.queryByRole('combobox')).toBeNull()
    fireEvent.click(container.querySelector('.drawing-rail-find > summary'))
    expect(screen.getByRole('combobox')).toBeTruthy()
  })
  it('collapses the agent panel to its pill on Escape and preserves its children', () => {
    const { container } = render(<AgentPanel><p>String every roof</p><input aria-label="Ask Leaf" /></AgentPanel>)
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })
    expect(container.querySelector('.studio-agent-panel').dataset.collapsed).toBe('true')
    expect(screen.getByRole('button', { name: 'Ask Leaf' }).getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(screen.getByRole('button', { name: 'Ask Leaf' }))
    expect(screen.getByText('String every roof')).toBeTruthy()
    expect(container.querySelector('.studio-agent-panel').dataset.collapsed).toBe('false')
    expect(screen.queryByRole('list', { name: 'Message history' })).toBeNull()
  })
  it('dismisses an open proposal on Escape before collapsing the agent panel', () => {
    const dismiss = vi.fn()
    const tool = { name: 'solar-string', title: 'String the roof', capabilities: ['drawing.read'], params: { properties: {} } }
    function WithProposal() {
      const [route, setRoute] = useState({ lane: 'run', confidence: .99, tool: tool.name, runIntent: { tool: tool.name, params: {} } })
      return <AgentPanel><input aria-label="Ask Leaf" /><RoutePanel route={route} tools={[tool]} running={false}
        onConfirmIntent={vi.fn()} onDismiss={() => { dismiss(); setRoute(null) }} /></AgentPanel>
    }
    const { container } = render(<WithProposal />)
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })
    expect(dismiss).toHaveBeenCalledOnce()
    expect(container.querySelector('.studio-agent-panel').dataset.collapsed).toBe('false')
    expect(screen.getByRole('button', { name: 'Ask Leaf' })).toHaveAttribute('aria-expanded', 'true')
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })
    expect(container.querySelector('.studio-agent-panel').dataset.collapsed).toBe('true')
  })
  it('shows coordinates and selection while withholding scale and render diagnostics', () => {
    const viewerRef = { current: null }
    const { container, rerender } = render(<CockpitStatus ground={null} viewerRef={viewerRef} shown={{}} />)
    expect(screen.getByText('X')).toBeTruthy()
    expect(screen.getByText('Y')).toBeTruthy()
    expect(screen.getByText('no selection')).toBeTruthy()
    expect(container.querySelector('.cockpit-scale')).toBeNull()
    expect(container.querySelector('.cockpit-count')).toBeNull()
    rerender(<CockpitStatus ground={null} viewerRef={viewerRef} shown={{}} customerMode={false} />)
    expect(container.querySelector('.cockpit-scale')).toBeTruthy()
    expect(container.querySelector('.cockpit-count')).toBeTruthy()
  })
  it('gates plan, tool count and daily spend while retaining coordinates', () => {
    const status = (customerMode) => <footer>
      <ShellDiagnostics customerMode={customerMode} plan="admin" toolCount={63} costToday={0} />
      <CockpitStatus customerMode={customerMode} ground={null} viewerRef={{ current: null }} />
    </footer>
    const { rerender } = render(status(true))
    expect(screen.queryByText(/Plan:/)).toBeNull()
    expect(screen.queryByText(/Tools:/)).toBeNull()
    expect(screen.queryByText(/today/)).toBeNull()
    expect(screen.getByText('X')).toBeTruthy()
    rerender(status(false))
    expect(screen.getByText('Tools: 63 available · Plan: admin')).toBeTruthy()
    expect(screen.getByText('$0.000 today')).toBeTruthy()
    expect(screen.getByText('X')).toBeTruthy()
  })
  it('uses the drawing name and puts the short id behind a separated tooltip', () => {
    render(<DrawingTabLabel name="Roof plan" shortId="abc123" />)
    expect(screen.getByText('Roof plan').title).toBe('Roof plan · abc123')
    expect(screen.queryByText('abc123')).toBeNull()
  })
  it('omits absent KPI values without omitting a real zero', () => {
    render(<ShellKpis items={[{ label: 'Panels', value: 0 }, { label: 'Power', value: null }, { label: 'Version' }]} />)
    expect(screen.getByText('0')).toBeTruthy()
    expect(screen.queryByText('Power')).toBeNull()
    expect(screen.queryByText('Version')).toBeNull()
  })
  it('keeps checkout actions behind a quiet chip in both checkout states', () => {
    const take = vi.fn()
    const { container, rerender } = render(<CheckoutChip><button onClick={take}>Take</button></CheckoutChip>)
    expect(screen.getByText('View only')).toBeTruthy()
    expect(container.querySelector('details').open).toBe(false)
    fireEvent.click(screen.getByText('View only'))
    fireEvent.click(screen.getByText('Take'))
    expect(take).toHaveBeenCalledOnce()
    rerender(<CheckoutChip checkedOut><button>Release</button></CheckoutChip>)
    expect(screen.getByText('Checked out').className).toBe('checked-out')
  })
  it('reserves full checkout refusal copy for attempted writes', () => {
    expect(projectSolarOpenStatus('checkout_required')).toBeNull()
    expect(projectSolarOpenStatus('checkout_required', { attemptedWrite: true })).toBe('Take the project checkout before changing this drawing.')
  })
  it('starts with the drafting tools collapsed behind Draw in the Studio shell', () => {
    const { container } = render(<div className="app" data-studio-shell="cockpit" data-surface="solar"><DraftingRibbon clusters={[]} /></div>)
    expect(container.querySelector('.drafting-ribbon').dataset.collapsed).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: 'Draw' }))
    expect(container.querySelector('.drafting-ribbon').dataset.collapsed).toBeUndefined()
  })
})
