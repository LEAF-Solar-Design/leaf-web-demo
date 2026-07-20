// M5 — the guided-tour script.
//
// PURE data: no `import.meta`, no React, no JSON import (node imports this
// directly in scripts/check_tourscript.mjs).
//
// Every beat with a `prompt` is auto-typed into the REAL command bar and
// dispatched through the REAL nl-prompt router + mock engine — the tour never
// fabricates a result, it just drives the same handlers a human would. The
// numbers the audience sees are produced live by the engine on the sample
// rooftop, which is why no body text below quotes a hard number.
//
// Shape of a step:
//   id      unique slug
//   title   short coach-mark headline
//   body    <= ~35 words, plain language (no jargon, no invented numbers)
//   target  CSS selector to spotlight, or null for a whole-app beat
//   prompt  optional canned text auto-typed into the command bar
//   action  optional 'run' | 'author' | 'version' | 'exit'
//   tool    optional registry tool this beat is about (asserted by the oracle)
//
// No beat may route to the `solve` lane — the cloud solver is not wired into
// this demo and a dead end on stage is the one thing we cannot afford.

export const TOUR_STEPS = [
  {
    id: 'welcome',
    title: 'Guided demo — sample rooftop',
    body:
      'This is a real solar rooftop drawing. Type plain English; Leaf picks a tool, ' +
      'shows you what it will do, and runs it on the drawing. Nothing runs until you confirm.',
    target: '.app',
    action: null,
  },
  {
    id: 'viewer',
    title: 'Your drawing, live',
    body:
      'Here is the drawing itself — panels, roof edges and layers, drawn from the ' +
      'real file. Every result you are about to see lands back on this canvas.',
    target: '.viewer-canvas, .workspace-card',
    action: null,
  },
  {
    id: 'count',
    title: 'Ask a question in English',
    body:
      'Watch the command bar. We ask how many entities sit on each layer, and Leaf ' +
      'routes it to a safe read-only tool before running anything.',
    target: '.bar-well, .workspace-card',
    prompt: 'count panels per layer',
    action: 'run',
    tool: 'count-by-layer',
  },
  {
    id: 'edge',
    title: 'A real setback check',
    body:
      'Same bar, harder question: which panels sit within sixty inches of the roof ' +
      'edge. Leaf reads the distance out of your sentence and highlights the ring.',
    target: '.bar-well, .workspace-card',
    prompt: 'highlight panels within 60in of the edge',
    action: 'run',
    tool: 'highlight-panels-near-edge',
  },
  {
    id: 'measure',
    title: 'Measure the array',
    body:
      'Now the total panel area, with the largest module marked on the canvas. ' +
      'Same drawing, same loop, no cloud round trip.',
    target: '.bar-well, .workspace-card',
    prompt: 'measure the total area',
    action: 'run',
    tool: 'measure-panel-area',
  },
  {
    id: 'author',
    title: 'The part that is different',
    body:
      'Ask for something that does not exist yet and Leaf writes a new reusable ' +
      'tool — code you can read, keep and run again — not a one-off answer.',
    target: '.author-section, .workspace-card',
    prompt: 'build a tool that flags panels on the Panel Groups layer',
    action: 'author',
  },
  {
    id: 'version',
    title: 'Edits are versioned',
    body:
      'Deleting a panel is a real write. Leaf stages it as a new version you ' +
      'confirm, and Undo walks the drawing straight back. Nothing is destroyed.',
    target: '.workspace-card',
    prompt: 'delete panel 2A7',
    action: 'version',
    tool: 'delete-marked-panel',
  },
  {
    id: 'exit',
    title: 'Your turn',
    body:
      'That is the loop: ask, confirm, run, version, undo. Leave the tour and try ' +
      'your own question on the same drawing — everything stays in the demo.',
    target: null,
    action: 'exit',
  },
]

export default TOUR_STEPS
