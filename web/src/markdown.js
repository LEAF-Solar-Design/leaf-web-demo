// Assistant-text markdown, parsed to DATA — never to HTML.
//
// The chat lane renders model output, so the renderer is a security boundary:
// a model that emits `<img onerror=…>` must show those characters, not run
// them. This module therefore parses to a small node tree and the React
// component maps nodes to ELEMENTS, so React's own escaping is what reaches
// the DOM. There is deliberately no `dangerouslySetInnerHTML` anywhere in the
// path, and no markdown dependency: web/package.json carries only auth0,
// react, react-dom and three, and a chat renderer is not worth a new
// transitive tree (or its CVE surface).
//
// Scope is what an assistant turn actually produces: fenced code, headings,
// bullet/ordered lists, paragraphs, and the inline set (code, strong, em,
// links). Anything unrecognized stays literal text — the honest failure mode
// for a renderer whose input is untrusted.

const FENCE_RE = /^```(\S*)\s*$/
const HEADING_RE = /^(#{1,6})\s+(.*)$/
const BULLET_RE = /^[-*]\s+(.*)$/
const ORDERED_RE = /^(\d+)[.)]\s+(.*)$/

// Only these schemes may become an href. A model can write any string it likes
// into a link target, so `javascript:` (and data:, vbscript:, …) must never
// survive: such a link degrades to plain text carrying its own label.
const SAFE_HREF_RE = /^(https?:\/\/|mailto:)/i

/** Split text into block nodes. Pure: same input, same output, no DOM. */
export function parseMarkdown(text) {
  const src = typeof text === 'string' ? text : ''
  if (!src) return []
  const lines = src.replace(/\r\n?/g, '\n').split('\n')
  const blocks = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]

    // Fenced code. An UNCLOSED fence takes the rest of the buffer: while a
    // turn streams, the closing fence has simply not arrived yet, and showing
    // the partial block as code reads better than showing the backticks.
    const fence = FENCE_RE.exec(line)
    if (fence) {
      const lang = fence[1] || ''
      const body = []
      i += 1
      while (i < lines.length && !FENCE_RE.test(lines[i])) {
        body.push(lines[i])
        i += 1
      }
      if (i < lines.length) i += 1 // consume the closing fence
      blocks.push({ type: 'code', lang, text: body.join('\n') })
      continue
    }

    if (!line.trim()) {
      i += 1
      continue
    }

    const heading = HEADING_RE.exec(line)
    if (heading) {
      blocks.push({ type: 'heading', level: heading[1].length, spans: parseInline(heading[2]) })
      i += 1
      continue
    }

    if (BULLET_RE.test(line) || ORDERED_RE.test(line)) {
      const ordered = !BULLET_RE.test(line)
      const items = []
      while (i < lines.length) {
        const bullet = BULLET_RE.exec(lines[i])
        const numbered = ORDERED_RE.exec(lines[i])
        // A list ends at the first line that is not the SAME kind of item, so
        // a bullet block and an ordered block never merge into one list.
        if (ordered && numbered) items.push(parseInline(numbered[2]))
        else if (!ordered && bullet) items.push(parseInline(bullet[1]))
        else break
        i += 1
      }
      blocks.push({ type: 'list', ordered, items })
      continue
    }

    // Paragraph: consecutive non-blank lines that start no other block.
    const para = []
    while (i < lines.length) {
      const next = lines[i]
      if (!next.trim() || FENCE_RE.test(next) || HEADING_RE.test(next) ||
          BULLET_RE.test(next) || ORDERED_RE.test(next)) break
      para.push(next)
      i += 1
    }
    blocks.push({ type: 'para', spans: parseInline(para.join('\n')) })
  }

  return blocks
}

/**
 * Split one line into inline spans.
 *
 * Inline code is matched FIRST and its contents are never re-scanned, so
 * "`**not bold**`" stays literal inside the code span — the same precedence
 * the CommonMark spec gives code, and the reason a model can quote markup
 * without the renderer eating it.
 */
export function parseInline(text) {
  const src = typeof text === 'string' ? text : ''
  const spans = []
  let buf = ''
  let i = 0

  const flush = () => {
    if (buf) {
      spans.push({ type: 'text', text: buf })
      buf = ''
    }
  }

  while (i < src.length) {
    const rest = src.slice(i)

    const code = /^`([^`]+)`/.exec(rest)
    if (code) {
      flush()
      spans.push({ type: 'code', text: code[1] })
      i += code[0].length
      continue
    }

    const strong = /^\*\*([^*]+)\*\*/.exec(rest)
    if (strong) {
      flush()
      spans.push({ type: 'strong', text: strong[1] })
      i += strong[0].length
      continue
    }

    const em = /^\*([^*\n]+)\*|^_([^_\n]+)_/.exec(rest)
    if (em) {
      flush()
      spans.push({ type: 'em', text: em[1] ?? em[2] })
      i += em[0].length
      continue
    }

    const link = /^\[([^\]]*)\]\(([^)\s]+)\)/.exec(rest)
    if (link) {
      const [, label, href] = link
      if (SAFE_HREF_RE.test(href)) {
        flush()
        spans.push({ type: 'link', text: label || href, href })
      } else {
        // Unsafe scheme -> the construct stays exactly as the model wrote it.
        // It goes back into the text buffer rather than becoming its own span
        // so it merges with the characters around it: a target like
        // `javascript:alert(1)` ends the href match at its INNER paren, and
        // the trailing `)` would otherwise strand as a second span.
        buf += link[0]
      }
      i += link[0].length
      continue
    }

    buf += src[i]
    i += 1
  }

  flush()
  return spans
}
