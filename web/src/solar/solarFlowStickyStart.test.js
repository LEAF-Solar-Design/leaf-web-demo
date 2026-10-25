// @vitest-environment node
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const css = readFileSync(new URL('./solarFlow.css', import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '');
const startSelector = '.studio-shell .workspace-card .solar-flow-seat [data-testid="solar-settings-form"] > form.params > button[type="submit"]';

function parseBlocks(source) {
  const blocks = [];
  let cursor = 0;
  while (cursor < source.length) {
    const open = source.indexOf('{', cursor);
    if (open === -1) break;
    let depth = 1;
    let end = open + 1;
    while (end < source.length && depth > 0) {
      if (source[end] === '{') depth += 1;
      if (source[end] === '}') depth -= 1;
      end += 1;
    }
    if (depth !== 0) throw new Error('Unbalanced CSS block');
    blocks.push({ header: source.slice(cursor, open).trim(), body: source.slice(open + 1, end - 1) });
    cursor = end;
  }
  return blocks;
}

function collectRules(source, desktop = false) {
  return parseBlocks(source).flatMap(({ header, body }) => {
    if (header.startsWith('@')) {
      return collectRules(body, desktop || /^@media\s*\(min-width:\s*1024px\)$/.test(header));
    }
    return [{ selector: header, body, desktop }];
  });
}

const rules = collectRules(css);

describe('sticky Start in the Solar seat', () => {
  it('pins Start to the bottom of the desktop seat', () => {
    const rule = rules.find(({ selector, desktop }) => desktop && selector === startSelector);
    expect(rule).toBeDefined();
    expect(rule.body).toMatch(/position:\s*sticky;/);
    expect(rule.body).toMatch(/bottom:\s*0;/);
    expect(rule.body).toMatch(/var\(--cv-panel/);
  });

  it('keeps narrow layouts unchanged', () => {
    const narrowStickyRules = rules.filter(({ selector, body, desktop }) =>
      !desktop && selector.includes('solar-settings-form') && /position:\s*sticky/.test(body));
    expect(narrowStickyRules).toEqual([]);
  });

  it('sets no flex sizing on the sticky Start rule', () => {
    const startRules = rules.filter(({ selector }) => selector === startSelector);
    expect(startRules.length).toBeGreaterThan(0);
    for (const { body } of startRules) {
      expect(body).not.toMatch(/(?:^|;)\s*flex(?:-grow|-shrink|-basis)?\s*:/);
    }
  });
});
