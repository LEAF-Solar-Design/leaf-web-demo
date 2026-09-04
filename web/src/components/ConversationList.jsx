// ---------------------------------------------------------------------------
// CONVERSATION LIST (standardization slice 6b, docs/convergence/
// SURFACE-CONTRACT.md `conversations.scope`). ONE primitive that lists the
// tenant's own conversations and resumes one, modelled row-for-row on
// operator/SessionPanel.jsx — the list+resume surface this repo already had —
// so a second list does not invent a second vocabulary.
//
// What it is:
//   * rows read `scope · title · when`, exactly SessionPanel's compact row
//     shape (`profile · environment · status`) with this lane's three fields;
//   * the active row carries `aria-pressed`, exactly SessionPanel's affordance;
//   * resume is `ensureSession(scope)` (the SAME idempotent attach cache the
//     composer uses, so resuming never opens a second session for a
//     conversation already attached) followed by the caller's `onResume`,
//     which hands the scene a `{sessionId, scope, afterSeq}` it feeds to the
//     ConversePanel that owns `openStream`. This primitive deliberately opens
//     no stream of its own: two streams on one session would double every
//     event the panel renders.
//
// What it is NOT: a live region. `/try` asserts exactly ONE `aria-live` node
// (web/e2e/responsive-keyboard.spec.mjs), and a list that announced itself
// would both break that count and talk over the run-status region beside it.
// Nothing here is polite, assertive, or atomic.
//
// Bounds, because this is a list over unbounded server state:
//   * one page per fetch, `limit` clamped to CONVERSATION_PAGE_MAX by
//     converse.listSessions before the request is built;
//   * paging is the server's opaque keyset cursor, and a page is APPENDED, so
//     the render is O(rows) per page and never re-sorts the whole list;
//   * `MAX_PAGES` caps how far one mount will page, so a stuck cursor cannot
//     grow the DOM without end;
//   * every fetch is generation-guarded: a scope switch or an unmount drops a
//     late response instead of seating another scope's rows.
// ---------------------------------------------------------------------------
import { useCallback, useEffect, useRef, useState } from "react";

import { ensureSession, listSessions, normalizeScope } from "../converse.js";

import { fmtWhen } from "./JobRail.jsx";

//: How many pages one mount will fetch before it stops offering "Older".
//: 5 x 20 rows is far past what a rail can show; the cap exists so a server
//: that keeps handing back a cursor cannot grow this list without end.
const MAX_PAGES = 5;
//: Rows per page. Bounded again inside converse.listSessions.
const PAGE_LIMIT = 20;

//: The row label for a conversation with no title yet (no `turn_started` has
//: carried user text). Never a fabricated summary of the transcript.
const UNTITLED = "Untitled";

/** `{kind, handle}` -> the short scope label a row shows. Pure. */
export function scopeLabel(scope) {
  const normalized = normalizeScope(scope);
  if (!normalized) return "Conversation";
  if (normalized.kind === "project") return "Project";
  if (normalized.kind === "entity") return `Element ${normalized.handle}`;
  return `Drawing ${normalized.handle}`;
}

/**
 * WHERE a resumed conversation lives, as a URL, or null when this shell is
 * already looking at it.
 *
 * The shell addresses a drawing through `?drawing=<id>` — the QUERY seed rule
 * DrawingIdentityProvider boots from (drawing/drawingIdentity.js
 * IDENTITY_ORIGIN.QUERY). Resuming another drawing's conversation therefore
 * REUSES the shell's own addressing rather than adding a setter that could
 * seat a drawing behind the identity provider's back. A project- or
 * entity-scoped row carries no drawing to address, so it returns null and the
 * caller just opens the panel on what is attached.
 *
 * Pure and total: a malformed href or scope returns null (stay put) instead of
 * throwing into a click handler. Exported for its own unit test.
 */
export function resumeHref(scope, currentDrawingId, href) {
  const normalized = normalizeScope(scope);
  if (
    !normalized ||
    normalized.kind !== "drawing" ||
    normalized.handle === currentDrawingId
  )
    return null;
  try {
    const url = new URL(href);
    url.searchParams.set("drawing", normalized.handle);
    return url.toString();
  } catch {
    return null;
  }
}

/**
 * ConversationList.
 *
 *   scope             the `{kind, handle}` to list, or null for every
 *                     conversation of the tenant. A scope the client cannot
 *                     normalize lists nothing rather than listing everything:
 *                     showing a wider set than the caller asked for is the
 *                     failure that matters here.
 *   activeSessionId   the conversation currently open, for `aria-pressed`
 *   onResume          (sessionId, {scope, afterSeq}) => void. Called after the
 *                     attach resolves, never before.
 *   label             the section's accessible name
 */
export default function ConversationList({
  scope = null,
  activeSessionId = null,
  onResume = null,
}) {
  const [rows, setRows] = useState([]);
  const [cursor, setCursor] = useState(null);
  const [pages, setPages] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [resuming, setResuming] = useState(null);
  const generationRef = useRef(0);

  // The primitive scope identity. A scope OBJECT is a new reference every
  // render, so the effect below keys on this string instead — otherwise every
  // parent render refetches the page.
  const normalized = normalizeScope(scope);
  const scopeKey = normalized ? `${normalized.kind}:${normalized.handle}` : "";
  const scopeDeclared = scope != null;

  const load = useCallback(
    async (nextCursor, generation) => {
      setLoading(true);
      try {
        const page = await listSessions({
          scope: normalized,
          limit: PAGE_LIMIT,
          cursor: nextCursor,
        });
        if (generation !== generationRef.current) return;
        setRows((current) =>
          nextCursor ? [...current, ...page.sessions] : page.sessions,
        );
        setCursor(page.nextCursor);
        setPages((current) => current + 1);
        setError(null);
      } catch {
        if (generation !== generationRef.current) return;
        // Honest and non-destructive: the rows already on screen stay, and the
        // failure says so. An empty list would read as "you have none".
        setError("Unavailable.");
      } finally {
        if (generation === generationRef.current) setLoading(false);
      }
      // `normalized` is rebuilt each render; `scopeKey` is its identity.
      // eslint-disable-next-line react-hooks/exhaustive-deps
    },
    [scopeKey, scopeDeclared],
  );

  useEffect(() => {
    const generation = ++generationRef.current;
    setRows([]);
    setCursor(null);
    setPages(0);
    setError(null);
    setResuming(null);
    // A declared-but-unnormalizable scope lists nothing (fails closed).
    if (scopeDeclared && !scopeKey) return;
    void load(null, generation);
  }, [load, scopeDeclared, scopeKey]);

  const resume = useCallback(
    async (row) => {
      if (!row || !row.id || resuming) return;
      setResuming(row.id);
      const generation = generationRef.current;
      try {
        const attached = await ensureSession(row.scope);
        if (generation !== generationRef.current) return;
        // The row's own id is the authority for WHICH conversation this is; the
        // attach only proves it is reachable and warms the cache the composer
        // reads. They agree by construction (the scope names one session), and
        // if a server ever disagreed the row the user clicked still wins.
        const sessionId = attached?.session_id || row.id;
        setError(null);
        onResume?.(sessionId, {
          scope: row.scope,
          afterSeq: Number(row.last_seq) || 0,
        });
      } catch {
        if (generation !== generationRef.current) return;
        setError("Resume failed.");
      } finally {
        if (generation === generationRef.current) setResuming(null);
      }
    },
    [onResume, resuming],
  );

  return (
    <section className="conversation-list" aria-label="Conversations">
      <div className="conversation-list-head">
        <span className="converse-title">Conversations</span>
        {loading && <span className="dim">Loading…</span>}
      </div>

      <ul className="conversation-list-rows">
        {rows.map((row) => {
          const when = fmtWhen(row.updated_at);
          return (
            <li key={row.id}>
              <button
                type="button"
                className="conversation-list-item"
                aria-pressed={row.id === activeSessionId}
                disabled={resuming === row.id}
                onClick={() => {
                  void resume(row);
                }}
              >
                <span className="conversation-row-scope">
                  {scopeLabel(row.scope)}
                </span>
                <span className="conversation-row-title">
                  {row.title || UNTITLED}
                </span>
                <span className="dim conversation-row-when">
                  {resuming === row.id ? "Opening" : when ? when.rel : "—"}
                </span>
              </button>
            </li>
          );
        })}
        {rows.length === 0 && !loading && !error && (
          <li className="dim">No conversations.</li>
        )}
      </ul>

      {cursor && pages < MAX_PAGES && (
        <button
          type="button"
          className="chip-act"
          disabled={loading}
          onClick={() => {
            void load(cursor, generationRef.current);
          }}
        >
          Older
        </button>
      )}

      {error && (
        <p className="conversation-list-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
