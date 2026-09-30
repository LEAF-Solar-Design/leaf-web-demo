// Bottom occluders include the footer, phone drawer headings, and command line's fixed-height well plus a constant 50 px reserve for the prompt's two rows, so an armed prompt never covers fitted geometry and arming or disarming a command still never moves the drawing.
export const STUDIO_DRAWING_OCCLUDERS = Object.freeze([
  ['header.top', 'top'], ['#drafting-ribbon', 'top'], ['.viewer-toolbar', 'top'],
  ['[data-testid="cockpit-view"]', 'top'], ['.properties-dock', 'left'],
  ['.bar.bar-command-line', 'bottom', Object.freeze({ reserve: 50 })],
  ['footer.foot-bar', 'bottom'], ['.studio-drawer-tabs', 'bottom'], ['.rail-stack', 'nearest'],
  ['[data-nav-find]', 'top'],
  ['[data-cad-overview]', 'right'],
  ['[data-nav-objects]:not([open])', 'top'],
  ['[data-nav-objects][open]', 'left'],
])
