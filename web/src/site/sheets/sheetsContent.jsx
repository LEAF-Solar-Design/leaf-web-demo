// sheetsContent — the seven sheet compositions of the Website Sheets set.
// L-000 and G-000 are VERBATIM from "Website Sheets.dc.html"; A-101..S-501
// recast the ui-kit homepage copy (ui_kits/website/index.html) into the same
// frame, sentence case, mono only for refs/revs/codes.
import SheetFrame, { Dot, TrialCta, Chip, go } from './SheetFrame.jsx';
import { DemandCaptureCard } from '../../components/SessionGate.jsx';

export const SHEET_CODES = ['l-000', 'g-000', 'a-101', 'a-102', 'e-401', 'c-201', 's-501'];

const TRIAL_NOTE = '14-day trial · $299 / mo per daily active user';
const DEPLOYED = 'Deployed at Black & Veatch and 40+ commercial EPCs';
const BRANCH_DISCIPLINE = 'Branch — solar stringing for AutoCAD';
const DRAWN_BY = 'Drawn by Leaf · reviewed by you';

// G-000 nav doubles as the interior sheets' nav — every entry is a set anchor.
const BRANCH_NAV = [
  { label: 'The problem', target: '#a-101' },
  { label: 'How it works', target: '#a-102' },
  { label: 'Evidence', target: '#e-401' },
  { label: 'Pricing', target: '#c-201' },
  { label: 'Docs', target: '#s-501' },
  { label: 'Sign in', target: '/app', accent: true },
];

const PLATFORM_NAV = [
  { label: 'The platform', target: '#l-000' },
  { label: 'Tools ▾', target: '#g-000' },
  { label: 'Evidence', target: '#e-401' },
  { label: 'Pricing', target: '#c-201' },
  { label: 'Docs', target: '#s-501' },
  { label: 'Sign in', target: '/app', accent: true },
];

/* ---------- shared fragments ---------- */

function DeployedRow() {
  return (
    <div className="sheet-deploy">
      <Dot />
      <span>{DEPLOYED}</span>
    </div>
  );
}

/** The set index card (G-000 anatomy), reused on every Branch sheet with the
 *  current row highlighted. Rows and hints are verbatim from the mock. */
function SetIndexCard({ current }) {
  const rows = [
    { code: 'G-000', label: 'Cover', hint: '' },
    { code: 'A-101', label: 'The problem', hint: '25 hrs, dressed up' },
    { code: 'A-102', label: 'How Branch works', hint: '4 steps' },
    { code: 'E-401', label: 'Evidence & compliance', hint: '0 / 12k violations' },
    { code: 'C-201', label: 'Pricing', hint: 'one number' },
    { code: 'S-501', label: 'Docs & API', hint: 'for your IT' },
  ];
  return (
    <div className="sheet-card">
      <div className="sheet-card-head">
        <b>In this set</b>
        <span>6 sheets</span>
      </div>
      <div className="sheet-card-rows">
        {rows.map((row) => {
          const id = row.code.toLowerCase();
          const isCurrent = id === current;
          return (
            <button
              key={row.code}
              type="button"
              className={isCurrent ? 'sheet-index-row current' : 'sheet-index-row'}
              onClick={isCurrent ? undefined : () => go(`#${id}`)}
              aria-current={isCurrent ? 'true' : undefined}
            >
              <span className="bar" />
              <span className="code">{row.code}</span>
              <span className="label">{row.label}</span>
              <span className="hint">{isCurrent ? 'you are here' : row.hint}</span>
            </button>
          );
        })}
      </div>
      <div className="sheet-card-foot spread">
        <span className="foot-note">Scroll reads the set in order</span>
        <button type="button" className="sheet-print" onClick={() => window.print()}>
          Print set
        </button>
      </div>
    </div>
  );
}

/* ---------- L-000 · Platform cover (verbatim) ---------- */

export function L000Sheet() {
  return (
    <SheetFrame
      id="l-000"
      context="construction automation"
      nav={PLATFORM_NAV}
      left={
        <>
          <div className="sheet-lede">
            <h1 className="sheet-headline">
              We do the <em>drafting</em>. You do the engineering.
            </h1>
            <p className="sheet-sub">
              Three decades of manual drafting, dressed up as engineering. Leaf builds the
              automation your discipline actually uses — custom tooling on one platform, inside
              your CAD, with evidence on every output. No engineering judgment, ever: you review,
              you stamp.
            </p>
          </div>
          <div className="sheet-cta-row">
            <TrialCta />
            <Chip target="#g-000">See Branch — solar stringing ↓</Chip>
            <span className="sheet-cta-note">{TRIAL_NOTE}</span>
          </div>
          <DeployedRow />
        </>
      }
      right={
        <div className="sheet-card">
          <div className="sheet-card-head">
            <b>The platform</b>
            <span>one console, per-discipline tools</span>
          </div>
          <div className="sheet-card-rows">
            <div className="sheet-card-row">
              <Dot />
              <span className="row-text">Custom tools, built from your feature requests</span>
            </div>
            <div className="sheet-card-row">
              <Dot />
              <span className="row-text">An evidence bundle on every output — cited, checkable</span>
            </div>
            <div className="sheet-card-row">
              <Dot kind="hollow" />
              <span className="row-text">Trust tiers — new tooling starts sandboxed, humans promote</span>
            </div>
            <div className="sheet-card-row">
              <Dot />
              <span className="row-text">Lives inside AutoCAD — your title block, your layers</span>
            </div>
          </div>
          <div className="sheet-card-foot">
            <span className="foot-note">Tools:</span>
            <span className="foot-tool">
              <Dot />
              Branch · solar stringing
            </span>
            <span className="foot-tool dim pulse">
              <Dot kind="hollow" />
              next discipline in build
            </span>
          </div>
        </div>
      }
      hero={{
        src: '/site/hero-drawing.png',
        height: 300,
        position: 'center 30%',
        caption: (
          <>
            <Dot kind="pulse" />
            <span className="strip-text">
              This drawing strings itself live on the real site — marketing recasts into the
              tool, same surface
            </span>
            <button type="button" className="strip-ref" onClick={() => go('/try')}>
              live demo · press T
            </button>
          </>
        ),
      }}
      titleBlock={{
        discipline: 'One platform · per-discipline drafting tools',
        sheetCode: 'L-000',
        sheetTitle: 'Platform cover',
      }}
    />
  );
}

/* ---------- G-000 · Branch cover (verbatim) ---------- */

// Builds the live-demo claim strings from the actual solve payload — numbers
// on the sheets must match what the stage renders, so they are derived, never
// typed. Returns neutral no-number fallbacks when no solve is available.
export function liveDemoLines(solve) {
  const st = solve?.stats;
  if (!st || !st.string_count) {
    return {
      liveLine: 'Strung live in the demo — press T on the cover to watch it run',
      receipt: 'NEC 690.7',
    };
  }
  const pass = solve?.electrical?.pass !== false;
  const maxMod = solve?.electrical?.max_modules_per_string;
  const wire = st.total_wire_ft ? ` · ${Math.round(st.total_wire_ft).toLocaleString('en-US')} ft DC wire` : '';
  const sha = (solve?.intake_sha256 || '').slice(0, 8);
  const solver = solve?.solver;
  return {
    liveLine: `Strung live in the demo — ${st.string_count} strings` +
      (maxMod ? ` · ${maxMod} modules max` : '') + wire,
    receipt: `NEC 690.7 ${pass ? '✓' : '✗'}` +
      (sha ? ` · solve #${sha}` : '') +
      (solver ? ` · ${solver.tool} v${solver.version}` : ''),
  };
}

export function G000Sheet({ solve } = {}) {
  const live = liveDemoLines(solve);
  return (
    <SheetFrame
      id="g-000"
      context="Branch · solar stringing for AutoCAD"
      nav={BRANCH_NAV}
      left={
        <>
          <div className="sheet-lede">
            <h1 className="sheet-headline">
              Stringing is drafting. <em>Branch does it.</em>
            </h1>
            <p className="sheet-sub narrow">
              The first Leaf tool. Branch turns a site plan into strung, NEC-checked drafting
              objects inside your AutoCAD — on your title block, against your code cycle. You
              review, you stamp.
            </p>
          </div>
          <div className="sheet-bignum-row">
            <span className="sheet-bignum">25 h → 3 min</span>
            <span className="sheet-bignum-cap">
              stringing a 3.2 MW rooftop · internal EPC survey, n = 18
            </span>
          </div>
          <div className="sheet-cta-row">
            <TrialCta />
            <Chip target="/try">Try it on this drawing</Chip>
            <span className="sheet-cta-note">{TRIAL_NOTE}</span>
          </div>
          <DeployedRow />
        </>
      }
      right={<SetIndexCard current="g-000" />}
      hero={{
        src: '/site/hero-drawing.png',
        height: 340,
        position: 'center 40%',
        caption: (
          <>
            <Dot kind="pulse" />
            <span className="strip-text">{live.liveLine.replace('in the demo', 'in the demo above')}</span>
            <span className="strip-ref">{live.receipt}</span>
          </>
        ),
      }}
      titleBlock={{
        discipline: BRANCH_DISCIPLINE,
        drawnBy: DRAWN_BY,
        sheetCode: 'G-000',
        sheetTitle: 'Cover',
      }}
    />
  );
}

/* ---------- A-101 · The problem (ui-kit Problem section, recast) ---------- */

export function A101Sheet() {
  return (
    <SheetFrame
      id="a-101"
      context="Branch · solar stringing for AutoCAD"
      nav={BRANCH_NAV}
      left={
        <>
          <div className="sheet-lede">
            <h1 className="sheet-headline">
              Three decades of <em>manual drafting</em>, dressed up as engineering.
            </h1>
            <p className="sheet-sub">
              You still route conduit by hand. Still count panels onto strings. Still spend days
              on work a computer should handle — while labor shortages tighten, code gets denser,
              and deadlines don’t move.
            </p>
            <p className="sheet-accent-line">It’s not engineering. It’s drafting.</p>
          </div>
          <div className="sheet-bignum-row">
            <span className="sheet-bignum">25 hrs</span>
            <span className="sheet-bignum-cap">
              manual stringing per 1 MW commercial project · NREL solar labor study
            </span>
          </div>
          <div className="sheet-rows">
            <div className="sheet-row">
              <span className="num">40%</span>
              <span className="txt">
                of projects require rework — a panel layout shift means re-doing everything
                downstream of it
              </span>
              <span className="cap">internal EPC survey, n = 18</span>
            </div>
            <div className="sheet-row">
              <span className="num">15 / 15</span>
              <span className="txt">
                engineering firms refused to share building plans to train generic AI —
                proprietary IP isn’t training data
              </span>
              <span className="cap">Leaf Automation research</span>
            </div>
          </div>
          <div className="sheet-cta-row">
            <TrialCta />
            <Chip target="#a-102">How Branch works ↓</Chip>
            <span className="sheet-cta-note">{TRIAL_NOTE}</span>
          </div>
        </>
      }
      right={<SetIndexCard current="a-101" />}
      titleBlock={{
        discipline: BRANCH_DISCIPLINE,
        drawnBy: DRAWN_BY,
        sheetCode: 'A-101',
        sheetTitle: 'The problem',
      }}
    />
  );
}

/* ---------- A-102 · How Branch works (ui-kit HowItWorks, recast) ---------- */

const STEPS = [
  {
    code: '01',
    title: 'Bring in the layout',
    desc: 'Drop a SolarEdge Designer PDF, import from Helioscope, or use your own CAD panel grid. Branch reads specs, orientations, and setbacks automatically.',
    timing: '~30 sec · all sources',
  },
  {
    code: '02',
    title: 'Set the engineering',
    desc: 'Inverter model, MPPT count, voltage window, temp range. You make the decisions; Branch records them. No calculations on your behalf.',
    timing: '~60 sec · once per project family',
  },
  {
    code: '03',
    title: 'Run the solver',
    desc: 'Strings, homeruns, tags, cable lengths — placed on the drawing in your standard layers and block conventions. NEC voltage-window compliance by construction.',
    timing: '~2 min · 3.2 MW rooftop',
  },
  {
    code: '04',
    title: 'Hand off the CDs',
    desc: 'Circuit length CSV for the BOM, tagged DWG for the stamped set, PDF for the AHJ. Rerun on any layout change — the whole cycle is disposable.',
    timing: '~15 sec · reruns just as fast',
  },
];

export function A102Sheet() {
  return (
    <SheetFrame
      id="a-102"
      context="Branch · solar stringing for AutoCAD"
      nav={BRANCH_NAV}
      left={
        <>
          <div className="sheet-lede">
            <h1 className="sheet-headline">
              Four steps. <em>Three minutes.</em> Inside your AutoCAD.
            </h1>
          </div>
          <div className="sheet-rows steps">
            {STEPS.map((step) => (
              <div className="sheet-row" key={step.code}>
                <span className="code">{step.code}</span>
                <span className="step-main">
                  <span className="step-title">{step.title}</span>
                  <span className="txt">{step.desc}</span>
                </span>
                <span className="cap">{step.timing}</span>
              </div>
            ))}
          </div>
          <div className="sheet-cta-row">
            <TrialCta />
            <Chip target="#e-401">See the evidence ↓</Chip>
            <span className="sheet-cta-note">{TRIAL_NOTE}</span>
          </div>
        </>
      }
      right={<SetIndexCard current="a-102" />}
      titleBlock={{
        discipline: BRANCH_DISCIPLINE,
        drawnBy: DRAWN_BY,
        sheetCode: 'A-102',
        sheetTitle: 'How Branch works',
      }}
    />
  );
}

/* ---------- E-401 · Evidence & compliance (ui-kit Proof, recast) ---------- */

const E401_DEFAULT_STATS = {
  violations: '0 / 12k',
  violationsCaption: 'NEC violations across deployed projects',
  timePerMw: '3 min',
  timePerMwNote: '↓ from 25 hrs · time per MW',
  reworkDelta: '−73%',
  reworkNote: 'rework incidents per project',
  payback: '1 proj',
  paybackNote: 'typical recoup at $299 / mo',
  // Neutral no-number fallback — live numbers arrive via <SheetsSet solve>.
  liveLine: 'Strung live in the demo — press T on the cover to watch it run',
};
const E401_DEFAULT_RECEIPT = 'NEC 690.7';

/**
 * Accepts optional live data:
 *   stats   — partial override of E401_DEFAULT_STATS
 *   receipt — mono receipt string (e.g. from /api/site/demo-solve)
 */
export function E401Sheet({ stats, receipt } = {}) {
  const s = { ...E401_DEFAULT_STATS, ...stats };
  const r = receipt || E401_DEFAULT_RECEIPT;
  return (
    <SheetFrame
      id="e-401"
      context="Branch · solar stringing for AutoCAD"
      nav={BRANCH_NAV}
      left={
        <>
          <div className="sheet-lede">
            <h1 className="sheet-headline">
              Not a research demo. <em>A shipping product.</em>
            </h1>
            <p className="sheet-sub">
              Branch has been stringing real commercial projects since 2024 — at Fortune-500
              engineering firms, regional EPCs, and multi-discipline subs. Every solve ships an
              evidence bundle — cited, checkable, on the output itself.
            </p>
          </div>
          <div className="sheet-bignum-row">
            <span className="sheet-bignum">{s.violations}</span>
            <span className="sheet-bignum-cap">{s.violationsCaption}</span>
          </div>
          <div className="sheet-rows">
            <div className="sheet-row">
              <span className="num">{s.timePerMw}</span>
              <span className="txt">{s.timePerMwNote}</span>
              <span className="cap">internal EPC survey, n = 18</span>
            </div>
            <div className="sheet-row">
              <span className="num">{s.reworkDelta}</span>
              <span className="txt">{s.reworkNote}</span>
              <span className="cap">internal EPC survey, n = 18</span>
            </div>
            <div className="sheet-row">
              <span className="num">{s.payback}</span>
              <span className="txt">{s.paybackNote}</span>
              <span className="cap">at one seat</span>
            </div>
          </div>
          <blockquote className="sheet-quote">
            <p>
              Stringing used to eat the first day of every project. Now it’s a coffee break. The
              rework tolerance is the part I didn’t expect — if the layout shifts, we just rerun
              it.
            </p>
            <footer>Senior PV designer · commercial EPC · 2,400+ modules per project</footer>
          </blockquote>
          <div className="sheet-receipt">
            <Dot kind="pulse" />
            <span className="strip-text">{s.liveLine}</span>
            <span className="strip-ref">{r}</span>
          </div>
          <div className="sheet-cta-row">
            <TrialCta />
            <Chip target="#c-201">Pricing ↓</Chip>
            <span className="sheet-cta-note">{TRIAL_NOTE}</span>
          </div>
        </>
      }
      right={<SetIndexCard current="e-401" />}
      titleBlock={{
        discipline: BRANCH_DISCIPLINE,
        drawnBy: DRAWN_BY,
        sheetCode: 'E-401',
        sheetTitle: 'Evidence & compliance',
      }}
    />
  );
}

/* ---------- C-201 · Pricing (ui-kit CTA/pricing lines, recast) ---------- */

export function C201Sheet() {
  return (
    <SheetFrame
      id="c-201"
      context="Branch · solar stringing for AutoCAD"
      nav={BRANCH_NAV}
      left={
        <>
          <div className="sheet-lede">
            <h1 className="sheet-headline">
              Stop drafting what you should be <em>generating.</em>
            </h1>
            <p className="sheet-sub">One number. It covers the tool, the evidence bundles, and
              every rerun.</p>
          </div>
          <div className="sheet-bignum-row price">
            <span className="sheet-price">$299</span>
            <span className="sheet-bignum-cap">
              $299 / mo per daily active user · 14-day trial
            </span>
          </div>
          <div className="sheet-rows">
            <div className="sheet-row">
              <span className="txt">14-day free trial · no credit card</span>
            </div>
            <div className="sheet-row">
              <span className="txt">Typical payback — one project at $299 / mo</span>
              <span className="cap">internal EPC survey, n = 18</span>
            </div>
          </div>
          <div className="sheet-cta-row">
            <TrialCta />
            <DemandCaptureCard compact />
          </div>
          <DeployedRow />
        </>
      }
      right={<SetIndexCard current="c-201" />}
      titleBlock={{
        discipline: BRANCH_DISCIPLINE,
        drawnBy: DRAWN_BY,
        sheetCode: 'C-201',
        sheetTitle: 'Pricing',
      }}
    />
  );
}

/* ---------- S-501 · Docs & API (quiet rows, for your IT) ---------- */

const S501_DEFAULT_CAPABILITIES = [
  { label: 'AutoCAD 2018–2026', hint: 'Windows' },
  { label: 'Getting-started docs', hint: 'install to first solve' },
  { label: 'API & entitlements overview', hint: 'for your IT' },
  { label: 'Evidence bundle on every output', hint: 'cited, checkable' },
];

/** Accepts an optional capabilities list: [{ label, hint }]. */
export function S501Sheet({ capabilities } = {}) {
  const rows = capabilities && capabilities.length ? capabilities : S501_DEFAULT_CAPABILITIES;
  return (
    <SheetFrame
      id="s-501"
      context="Branch · solar stringing for AutoCAD"
      nav={BRANCH_NAV}
      left={
        <>
          <div className="sheet-lede">
            <h1 className="sheet-headline">
              Docs & API — <em>for your IT.</em>
            </h1>
            <p className="sheet-sub">
              What Branch needs, what it touches, and where the docs live. The short version,
              in order.
            </p>
          </div>
          <div className="sheet-rows quiet">
            {rows.map((row) => (
              <div className="sheet-row" key={row.label}>
                <span className="txt strong">{row.label}</span>
                <span className="cap">{row.hint}</span>
              </div>
            ))}
          </div>
          <div className="sheet-cta-row">
            <TrialCta />
            <Chip target="/try">Try it on this drawing</Chip>
            <span className="sheet-cta-note">{TRIAL_NOTE}</span>
          </div>
        </>
      }
      right={<SetIndexCard current="s-501" />}
      titleBlock={{
        discipline: BRANCH_DISCIPLINE,
        drawnBy: DRAWN_BY,
        sheetCode: 'S-501',
        sheetTitle: 'Docs & API',
      }}
    />
  );
}

/* ---------- the full set, in order ---------- */

export default function SheetsSet({ solve, e401Stats, e401Receipt, s501Capabilities } = {}) {
  // Derive the live-demo claim lines from the solve unless the caller passed
  // explicit overrides (tests / storybook-style usage).
  const live = liveDemoLines(solve);
  const stats = e401Stats || (solve ? { liveLine: live.liveLine } : undefined);
  const receipt = e401Receipt || (solve ? live.receipt : undefined);
  return (
    <>
      <L000Sheet />
      <G000Sheet solve={solve} />
      <A101Sheet />
      <A102Sheet />
      <E401Sheet stats={stats} receipt={receipt} />
      <C201Sheet />
      <S501Sheet capabilities={s501Capabilities} />
    </>
  );
}
