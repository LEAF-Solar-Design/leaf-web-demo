// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { scrollToSheet } from './SheetFrame.jsx';

vi.mock('../router.js', () => ({ navigate: vi.fn() }));

const originalScrollIntoView = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView');
let sheet;
let scrollIntoView;

beforeEach(() => {
  if (!Element.prototype.scrollIntoView) {
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: () => {},
    });
  }
  scrollIntoView = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});
  sheet = document.createElement('section');
  sheet.id = 'a-101';
  document.body.appendChild(sheet);
});

afterEach(() => {
  sheet.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  if (originalScrollIntoView) {
    Object.defineProperty(Element.prototype, 'scrollIntoView', originalScrollIntoView);
  } else {
    delete Element.prototype.scrollIntoView;
  }
});

describe('scrollToSheet motion preference', () => {
  it.each([
    [true, 'auto'],
    [false, 'smooth'],
  ])('uses %s reduced motion preference for %s scrolling', (matches, behavior) => {
    const matchMedia = vi.fn(() => ({ matches }));
    vi.stubGlobal('matchMedia', matchMedia);

    scrollToSheet(sheet.id);

    expect(matchMedia).toHaveBeenCalledWith('(prefers-reduced-motion: reduce)');
    expect(scrollIntoView).toHaveBeenCalledExactlyOnceWith({ behavior, block: 'start' });
    expect(scrollIntoView.mock.contexts[0]).toBe(sheet);
  });

  it('scrolls smoothly without throwing when matchMedia is unavailable', () => {
    vi.stubGlobal('matchMedia', undefined);

    expect(() => scrollToSheet(sheet.id)).not.toThrow();

    expect(scrollIntoView).toHaveBeenCalledExactlyOnceWith({ behavior: 'smooth', block: 'start' });
    expect(scrollIntoView.mock.contexts[0]).toBe(sheet);
  });
});
