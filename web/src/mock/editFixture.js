// Synthetic edit fixture — a small intake (CONTRACT §1 shape) that exercises
// the entity types the golden rooftop_demo sample cannot: INSERT/block refs
// and 3DFACE surfaces, alongside a few LWPOLYLINEs. Every entity carries a
// unique DWG `handle` so click-picking has a stable id to return, and each
// entity type sits on its own layer so the Legend/color-by-layer path is
// exercised too.
//
// Loaded in mock mode via `?fixture=edit` (see App.jsx). This is the test
// oracle for insert/face rendering + picking + the pending/version flow.
//
// NOTE on units: insert `rot` is in DEGREES (DXF/DWG convention); the viewer
// converts to radians. `scale` is [sx, sy, sz].

export const editFixture = {
  dwg: 'edit_fixture.dwg',
  layers: ['Panels', 'Modules', 'Blocks', 'Surfaces'],
  polylines: [
    { layer: 'Panels', closed: true, handle: 'PL1',
      pts: [[10, 10, 0], [40, 10, 0], [40, 30, 0], [10, 30, 0]], xdata: null },
    { layer: 'Panels', closed: true, handle: 'PL2',
      pts: [[50, 10, 0], [80, 10, 0], [80, 30, 0], [50, 30, 0]], xdata: null },
    { layer: 'Modules', closed: true, handle: 'PL3',
      pts: [[10, 40, 0], [40, 40, 0], [40, 60, 0], [10, 60, 0]], xdata: null },
  ],
  inserts: [
    { name: 'PV-COMBINER', layer: 'Blocks', handle: 'INS1',
      pt: [98, 20, 0], rot: 0, scale: [1, 1, 1] },
    { name: 'INVERTER', layer: 'Blocks', handle: 'INS2',
      pt: [98, 52, 0], rot: 30, scale: [1.7, 1.7, 1] },
  ],
  faces3d: [
    { layer: 'Surfaces', handle: 'F1',
      p1: [50, 40, 0], p2: [82, 40, 0], p3: [82, 62, 0], p4: [50, 62, 0] },
    { layer: 'Surfaces', handle: 'F2',
      p1: [95, 70, 0], p2: [120, 70, 0], p3: [120, 82, 0], p4: [95, 82, 0] },
  ],
  blockdefs: {},
  geodata: null,
  images: [],
  imageNames: [],
}

// A proposed, not-yet-committed edit for the optimistic pending overlay.
// `added` entries are intake-shaped (polyline-shaped here); `removed` is a
// list of handles the edit would delete. The viewer renders `added` as a
// dashed ghost and `removed` as a dashed "struck-out" outline.
export const pendingEditDemo = {
  added: [
    { layer: 'Panels', closed: true, handle: 'PL4-ghost',
      pts: [[10, 66, 0], [40, 66, 0], [40, 82, 0], [10, 82, 0]] },
  ],
  removed: ['PL2'],
}

// The result of applying `pendingEditDemo`: PL2 removed, the ghost promoted to
// a real panel (PL4), plus a second new panel (PL5) so the rendered polyline
// count visibly changes (3 -> 4) — crisp numeric proof that applyVersion()
// rebuilt scene geometry from the new intake. Same layer set as the base so
// color-by-layer stays consistent.
export const editFixtureV2 = {
  dwg: 'edit_fixture.dwg',
  layers: ['Panels', 'Modules', 'Blocks', 'Surfaces'],
  polylines: [
    { layer: 'Panels', closed: true, handle: 'PL1',
      pts: [[10, 10, 0], [40, 10, 0], [40, 30, 0], [10, 30, 0]], xdata: null },
    { layer: 'Modules', closed: true, handle: 'PL3',
      pts: [[10, 40, 0], [40, 40, 0], [40, 60, 0], [10, 60, 0]], xdata: null },
    { layer: 'Panels', closed: true, handle: 'PL4',
      pts: [[10, 66, 0], [40, 66, 0], [40, 82, 0], [10, 82, 0]], xdata: null },
    { layer: 'Panels', closed: true, handle: 'PL5',
      pts: [[50, 66, 0], [80, 66, 0], [80, 82, 0], [50, 82, 0]], xdata: null },
  ],
  inserts: [
    { name: 'PV-COMBINER', layer: 'Blocks', handle: 'INS1',
      pt: [98, 20, 0], rot: 0, scale: [1, 1, 1] },
    { name: 'INVERTER', layer: 'Blocks', handle: 'INS2',
      pt: [98, 52, 0], rot: 30, scale: [1.7, 1.7, 1] },
  ],
  faces3d: [
    { layer: 'Surfaces', handle: 'F1',
      p1: [50, 40, 0], p2: [82, 40, 0], p3: [82, 62, 0], p4: [50, 62, 0] },
    { layer: 'Surfaces', handle: 'F2',
      p1: [95, 70, 0], p2: [120, 70, 0], p3: [120, 82, 0], p4: [95, 82, 0] },
  ],
  blockdefs: {},
  geodata: null,
  images: [],
  imageNames: [],
}

export default editFixture
