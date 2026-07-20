// SheetsPage — renders the Website Sheets drawing set as one scrolling page.
// Anchor ids: l-000 · g-000 · a-101 · a-102 · e-401 · c-201 · s-501.
// Deep links: /sheets/<code> or #<code> scroll to the target sheet on mount;
// index-card clicks push the hash (see scrollToSheet in SheetFrame.jsx), and
// hash navigation (back/forward) re-scrolls.
//
// Integrator note: E-401 accepts live data — pass e401Stats / e401Receipt
// (e.g. from /api/site/demo-solve) through <SheetsSet>; static defaults render
// when absent. S-501 similarly accepts s501Capabilities.
import { useEffect, useState } from 'react';
import SheetsSet, { SHEET_CODES } from './sheetsContent.jsx';
import { loadDemoSolve } from '../intakeCache.js';
import '../sheets.css';

function targetFromLocation() {
  const pathMatch = window.location.pathname.match(/^\/sheets\/([a-z]-\d{3})\/?$/i);
  const fromPath = pathMatch ? pathMatch[1].toLowerCase() : null;
  const fromHash = window.location.hash.replace('#', '').toLowerCase();
  if (fromPath && SHEET_CODES.includes(fromPath)) return fromPath;
  if (fromHash && SHEET_CODES.includes(fromHash)) return fromHash;
  return null;
}

export default function SheetsPage() {
  // Live demo-solve numbers for the sheets that make claims about the demo
  // (G-000 hero caption, E-401 receipt strip). Neutral fallbacks render until
  // (or unless) the data arrives.
  const [solve, setSolve] = useState(null);
  useEffect(() => {
    let live = true;
    loadDemoSolve()
      .then((d) => { if (live) setSolve(d); })
      .catch(() => { /* neutral fallback lines render instead */ });
    return () => { live = false; };
  }, []);

  useEffect(() => {
    const scrollToTarget = (behavior) => {
      const code = targetFromLocation();
      if (!code) return;
      // Defer one frame so the sheets have laid out before measuring.
      requestAnimationFrame(() => {
        const el = document.getElementById(code);
        if (el) el.scrollIntoView({ behavior, block: 'start' });
      });
    };
    scrollToTarget('auto');
    const onHashChange = () => scrollToTarget('smooth');
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  return (
    <main className="sheets-root">
      <SheetsSet solve={solve} />
    </main>
  );
}
