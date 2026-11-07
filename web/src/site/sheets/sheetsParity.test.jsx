// @vitest-environment jsdom
import { cleanup, render, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import SheetsSet, { C201Sheet } from './sheetsContent.jsx';

const COMMERCIAL_CLAIM = /\$\d|per DAU|per daily active user|\d+-day (free )?trial/i;

afterEach(cleanup);

// F-COMM option A: design-parity-forks-20261101/receipts/operator-forks-all-a-20261101.json.
describe('Sheets canonical pricing links', () => {
  it('has no numeric price or trial claims anywhere in the rendered set', () => {
    const { container } = render(<SheetsSet />);
    expect(container.textContent).not.toMatch(COMMERCIAL_CLAIM);
  });

  it('directs C-201 to the current pricing and trial terms on the same host', () => {
    const { container } = render(<C201Sheet />);
    expect(container.textContent).not.toMatch(COMMERCIAL_CLAIM);
    const link = within(container).getByRole('link', { name: 'View current pricing' });
    expect(link.getAttribute('href')).toBe('/pricing');
    expect(link.closest('p').textContent).toBe(
      'Current pricing and trial terms are on the pricing page. View current pricing',
    );
  });

  it('links to current pricing at all six former CTA-note locations', () => {
    const { container } = render(<SheetsSet />);
    expect(container.querySelectorAll('.sheet-cta-note')).toHaveLength(6);
    for (const id of ['l-000', 'g-000', 'a-101', 'a-102', 'e-401', 's-501']) {
      const notes = container.querySelectorAll(`#${id} .sheet-cta-note`);
      expect(notes).toHaveLength(1);
      const link = within(notes[0]).getByRole('link', { name: 'View current pricing' });
      expect(link.getAttribute('href')).toBe('/pricing');
    }
  });

  it('preserves the seven sheet sections and their order from base 315f7cc6', () => {
    const { container } = render(<SheetsSet />);
    const sections = container.querySelectorAll('section.sheet');
    expect(sections).toHaveLength(7);
    expect(Array.from(sections, section => section.id)).toEqual([
      'l-000', 'g-000', 'a-101', 'a-102', 'e-401', 'c-201', 's-501',
    ]);
  });
});
