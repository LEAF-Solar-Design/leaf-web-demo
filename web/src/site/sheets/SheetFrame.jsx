// SheetFrame — the one layout component encoding the Website Sheets frame,
// verbatim from "Website Sheets.dc.html": 56px header band → content grid
// (1fr/400px, gap 56, padding 64 72 48) → optional hero-image band with a
// 34px caption strip → title-block strip on the sheet edge (#0c0c0e, bordered
// cells, mono sheet/rev codes). Styling lives in src/site/sheets.css under
// .sheets-root. Halo budget: exactly one per sheet, carried by <TrialCta>.
import { navigate } from '../router.js';

/** Smooth-scroll to a sheet anchor and record it in the hash. Hash updates go
 *  through the router's navigate() so useRoute() subscribers stay in sync and
 *  location.search is preserved (router.js contract). */
export function scrollToSheet(code) {
  const el = document.getElementById(code);
  if (!el) return;
  navigate(`${window.location.pathname}#${code}`);
  el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/** Route a nav/CTA target: '#code' → in-page anchor scroll, '/path' → router. */
export function go(target) {
  if (!target) return;
  if (target.startsWith('#')) scrollToSheet(target.slice(1));
  else if (target.startsWith('mailto:')) window.location.href = target;
  else navigate(target);
}

/** 6px status dot. kind: 'solid' | 'hollow' | 'pulse' (pulse is solid + live). */
export function Dot({ kind = 'solid' }) {
  const cls =
    kind === 'hollow' ? 'sheet-dot hollow'
    : kind === 'pulse' ? 'sheet-dot pulse'
    : 'sheet-dot';
  return <span className={cls} aria-hidden="true" />;
}

/** The single haloed primary per sheet — the trial CTA. */
export function TrialCta({ children = 'Start free trial', target = '/app' }) {
  return (
    <button type="button" className="sheet-cta" onClick={() => go(target)}>
      {children}
    </button>
  );
}

/** Quiet filled accent chip (never outlined). */
export function Chip({ children, target, onClick }) {
  return (
    <button
      type="button"
      className="sheet-chip"
      onClick={onClick || (() => go(target))}
    >
      {children}
    </button>
  );
}

/**
 * SheetFrame props:
 *   id          anchor id, e.g. 'l-000'
 *   context     header context text after the wordmark
 *   nav         [{ label, target }] — target '#code' scrolls, '/path' navigates
 *   left        left column node (headline, sub, CTA row…)
 *   right       right 400px card node
 *   hero        { src, height, position, caption } | undefined — hero band
 *   titleBlock  { discipline, drawnBy?, sheetCode, sheetTitle, rev?, date? }
 */
export default function SheetFrame({ id, context, nav = [], left, right, hero, titleBlock }) {
  const tb = {
    identity: 'Leaf Automation · Columbus, Ohio',
    rev: '14',
    date: 'Jul 2026',
    ...titleBlock,
  };
  return (
    <section className="sheet" id={id} aria-label={`Sheet ${tb.sheetCode} — ${tb.sheetTitle}`}>
      <header className="sheet-head">
        <img src="/site/icon-color.png" width="24" height="24" alt="" className="sheet-mark" />
        <span className="sheet-brand">Leaf Automation</span>
        <span className="sheet-context">{context}</span>
        <nav className="sheet-nav" aria-label="Sheet navigation">
          {nav.map((item) => (
            <button
              key={item.label}
              type="button"
              className={item.accent ? 'sheet-navlink accent' : 'sheet-navlink'}
              onClick={() => go(item.target)}
            >
              {item.label}
            </button>
          ))}
        </nav>
      </header>

      <div className="sheet-body">
        <div className="sheet-left">{left}</div>
        <div className="sheet-right">{right}</div>
      </div>

      {hero && (
        <div className="sheet-hero-wrap">
          <figure className="sheet-hero">
            <div className="sheet-hero-media" style={{ height: hero.height || 300 }}>
              <img
                src={hero.src}
                alt="Live stringing drawing"
                style={{ objectPosition: hero.position || 'center 30%' }}
              />
            </div>
            <figcaption className="sheet-hero-strip">{hero.caption}</figcaption>
          </figure>
        </div>
      )}

      <footer className="sheet-titleblock">
        <div className="tb-cell tb-identity">
          <img src="/site/icon-color.png" width="18" height="18" alt="" />
          <span>{tb.identity}</span>
        </div>
        <div className="tb-cell tb-dim">
          <span>{tb.discipline}</span>
        </div>
        {tb.drawnBy && (
          <div className="tb-cell tb-dim">
            <span>{tb.drawnBy}</span>
          </div>
        )}
        <button
          type="button"
          className="tb-cell tb-sheet"
          onClick={() => scrollToSheet(id)}
          title={`Sheet ${tb.sheetCode}`}
        >
          <span className="tb-k">Sheet</span>
          <span className="tb-code">{tb.sheetCode}</span>
          <span className="tb-sub">· {tb.sheetTitle}</span>
        </button>
        <div className="tb-cell tb-rev">
          <span className="tb-k">Rev</span>
          <span className="tb-code">{tb.rev}</span>
          <span className="tb-sub">· {tb.date}</span>
        </div>
      </footer>
    </section>
  );
}
