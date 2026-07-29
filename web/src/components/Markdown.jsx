// Renders assistant markdown as React ELEMENTS (never HTML).
//
// The parser in ../markdown.js returns data; this file maps that data to
// elements, so every string reaching the DOM goes through React's escaping.
// There is deliberately no `dangerouslySetInnerHTML` here — that absence is
// the security property, and a code review should treat adding one as a
// breaking change to the chat lane's threat model.
//
// Styling stays in the panel's existing vocabulary (`converse-*`, `route-tool`,
// `dim`) rather than introducing a parallel type scale.

import { parseMarkdown } from '../markdown.js'

function Spans({ spans }) {
  return spans.map((span, i) => {
    if (span.type === 'code') return <code key={i} className="converse-md-code">{span.text}</code>
    if (span.type === 'strong') return <strong key={i}>{span.text}</strong>
    if (span.type === 'em') return <em key={i}>{span.text}</em>
    if (span.type === 'link') {
      // The parser has already rejected any scheme outside http/https/mailto,
      // so an href here is safe by construction. noopener/noreferrer because
      // the target is model-authored content.
      return (
        <a key={i} href={span.href} target="_blank" rel="noopener noreferrer">
          {span.text}
        </a>
      )
    }
    return <span key={i}>{span.text}</span>
  })
}

export default function Markdown({ text }) {
  const blocks = parseMarkdown(text)
  if (blocks.length === 0) return null

  return blocks.map((block, i) => {
    if (block.type === 'code') {
      return (
        <pre key={i} className="converse-md-pre">
          {block.lang && <span className="converse-md-lang dim">{block.lang}</span>}
          <code>{block.text}</code>
        </pre>
      )
    }
    if (block.type === 'heading') {
      // Headings inside a chat bubble stay visually modest: the panel already
      // owns the page's heading scale, so this is weight, not size.
      return (
        <div key={i} className="converse-md-h" data-level={block.level}>
          <Spans spans={block.spans} />
        </div>
      )
    }
    if (block.type === 'list') {
      const Tag = block.ordered ? 'ol' : 'ul'
      return (
        <Tag key={i} className="converse-md-list">
          {block.items.map((item, j) => (
            <li key={j}><Spans spans={item} /></li>
          ))}
        </Tag>
      )
    }
    return (
      <p key={i} className="converse-md-p">
        <Spans spans={block.spans} />
      </p>
    )
  })
}
