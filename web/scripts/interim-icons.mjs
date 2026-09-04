// W4e slice G, the INTERIM hand set: the cockpit icon keys the design
// backend's ribbon take did not cover, drawn here as plain SVG on a 24px grid
// (stroke 1.5, round caps, currentColor). Original work for this project,
// replaced wholesale by the icons8 sprite when scripts/fetch_icons8.mjs runs.
// Each value is the symbol's inner markup; the builder wraps it in the shared
// stroke group and the 0 0 24 24 viewBox.
export const INTERIM_ICONS = Object.freeze({
  // W4g-4 EXPLODE: a polyline bursting into its segments (drawn here until
  // the icons8 fetch pins one; fetch_icons8.mjs --merge-interim adds it to
  // the served sprite beside the icons8 set).
  explode: '<path d="M8 15l3-6 3 4 3-5"/><path d="M4 8l2 2M20 5l-2 2M3 17l2-1M21 19l-2-1M12 3v2M12 19v2"/>',
  // W4g-5: a source segment and its parallel copy, with the gap marked.
  offset: '<path d="M4 8h16"/><path d="M4 16h16"/><path d="M12 8v8"/><path d="M10 10l2-2 2 2M10 14l2 2 2-2"/>',
  // W4g-5b: a rectangular array, the source and its copies in a grid.
  array: '<rect x="4" y="4" width="6" height="6"/><rect x="14" y="4" width="6" height="6"/><rect x="4" y="14" width="6" height="6"/><rect x="14" y="14" width="6" height="6"/>',
  // W4g-5b: a polar array, copies swept about a centre.
  'array-polar': '<circle cx="12" cy="12" r="1.5"/><rect x="10" y="3" width="4" height="4"/><rect x="17" y="14" width="4" height="4"/><rect x="3" y="14" width="4" height="4"/><path d="M12 9a3 3 0 0 1 3 3"/>',
  'new-file': '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><path d="M12 11v6M9 14h6"/>',
  open: '<path d="M3 6h6l2 2h10v11H3z"/><path d="M3 10h18"/>',
  save: '<path d="M4 4h13l3 3v13H4z"/><path d="M8 4v5h7V4"/><path d="M8 20v-6h8v6"/>',
  print: '<path d="M7 9V4h10v5"/><path d="M5 9h14a2 2 0 0 1 2 2v5h-4v4H7v-4H3v-5a2 2 0 0 1 2-2z"/><path d="M7 15h10"/>',
  undo: '<path d="M9 7L4 11l5 4"/><path d="M4 11h10a5 5 0 0 1 0 10h-3"/>',
  redo: '<path d="M15 7l5 4-5 4"/><path d="M20 11H10a5 5 0 0 0 0 10h3"/>',
  history: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/><path d="M4 12H2M6.3 6.3L5 5"/>',
  delete: '<path d="M5 7h14"/><path d="M9 7V4h6v3"/><path d="M7 7l1 13h8l1-13"/><path d="M10 11v6M14 11v6"/>',
  'move-vertex': '<path d="M3 18l6-9 5 6 7-11"/><circle cx="9" cy="9" r="2.2"/><path d="M13 3h4v4"/><path d="M17 3l-4 4"/>',
  'add-vertex': '<path d="M3 18l6-9 5 6 7-11"/><circle cx="14" cy="15" r="2.2"/><path d="M18 18h5M20.5 15.5v5"/>',
  'delete-vertex': '<path d="M3 18l6-9 5 6 7-11"/><circle cx="14" cy="15" r="2.2"/><path d="M18 18h5"/>',
  'set-layer': '<path d="M12 4l8 4-8 4-8-4z"/><path d="M4 12l8 4 8-4"/><path d="M4 16l8 4 8-4"/><path d="M12 11v9"/>',
  layers: '<path d="M12 4l8 4-8 4-8-4z"/><path d="M4 12l8 4 8-4"/><path d="M4 16l8 4 8-4"/>',
  freeze: '<path d="M12 3v18M4 7.5l16 9M4 16.5l16-9"/><path d="M12 3l-2 2M12 3l2 2M12 21l-2-2M12 21l2-2"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="1.5"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/><circle cx="12" cy="16" r="1.2"/>',
  import: '<path d="M12 3v11"/><path d="M8 10l4 4 4-4"/><path d="M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/>',
  fit: '<path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/><rect x="9" y="9" width="6" height="6"/>',
  'zoom-in': '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21"/><path d="M10.5 7.5v6M7.5 10.5h6"/>',
  'zoom-out': '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21"/><path d="M7.5 10.5h6"/>',
  wand: '<path d="M4 20L15 9"/><path d="M13 7l2 2"/><path d="M17 3v3M15.5 4.5h3M20 8l1.5 1.5M19 11h2"/>',
  sidebar: '<rect x="3" y="4" width="18" height="16" rx="1.5"/><path d="M9 4v16"/><path d="M5 8h2M5 11h2M5 14h2"/>',
  toolbox: '<rect x="3" y="8" width="18" height="12" rx="1.5"/><path d="M8 8V5h8v3"/><path d="M3 13h18"/><path d="M10 13v3h4v-3"/>',
  grid: '<circle cx="6" cy="6" r="1"/><circle cx="12" cy="6" r="1"/><circle cx="18" cy="6" r="1"/><circle cx="6" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="18" cy="12" r="1"/><circle cx="6" cy="18" r="1"/><circle cx="12" cy="18" r="1"/><circle cx="18" cy="18" r="1"/>',
  snap: '<path d="M6 4v8a6 6 0 0 0 12 0V4"/><path d="M6 4h4v8M14 4h4v8"/><path d="M6 8h4M14 8h4"/>',
  ortho: '<path d="M5 19V5"/><path d="M5 19h14"/><path d="M5 11h8v8"/>',
  polar: '<circle cx="12" cy="12" r="8"/><path d="M12 4v16M4 12h16"/><path d="M12 12l4-6"/><circle cx="12" cy="12" r="1.2"/>',
  osnap: '<circle cx="12" cy="12" r="7"/><path d="M12 2v5M12 17v5M2 12h5M17 12h5"/><circle cx="12" cy="12" r="1.2"/>',
  fullscreen: '<path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/><path d="M4 4l5 5M20 4l-5 5M20 20l-5-5M4 20l5-5"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  pin: '<path d="M9 3h6l-1 6 3 3H7l3-3z"/><path d="M12 12v9"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  split: '<rect x="3" y="4" width="18" height="16" rx="1.5"/><path d="M12 4v16"/>',
  wireframe: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12L4 7.5M12 12v9"/>',
  hatch: '<rect x="4" y="4" width="16" height="16"/><path d="M4 12l8-8M4 20L20 4M12 20l8-8"/>',
  trim: '<path d="M4 6l12 12"/><path d="M4 18L16 6"/><circle cx="6" cy="18" r="2"/><circle cx="6" cy="6" r="2"/><path d="M20 12h-6"/>',
  text: '<path d="M5 5h14"/><path d="M12 5v14"/><path d="M8 19h8"/>',
  dimension: '<path d="M4 15V9M20 15V9"/><path d="M4 12h16"/><path d="M7 10l-3 2 3 2M17 10l3 2-3 2"/><path d="M9 6h6"/>',
  match: '<path d="M14 4l6 6-9 9H5v-6z"/><path d="M11 7l6 6"/><path d="M4 20h4"/>',
  bulb: '<path d="M9 18h6"/><path d="M10 21h4"/><path d="M8.5 14a5.5 5.5 0 1 1 7 0c-.8.7-1.5 1.6-1.5 2.5h-4c0-.9-.7-1.8-1.5-2.5z"/>',
})
