import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { parseInline, parseMarkdown } from './markdown.js'

const textOf = (spans) => spans.map((s) => s.text).join('')

describe('parseInline', () => {
  it('keeps plain text as one span', () => {
    assert.deepEqual(parseInline('just words'), [{ type: 'text', text: 'just words' }])
  })

  it('parses strong, em and inline code', () => {
    assert.deepEqual(parseInline('a **b** c `d` e *f*'), [
      { type: 'text', text: 'a ' },
      { type: 'strong', text: 'b' },
      { type: 'text', text: ' c ' },
      { type: 'code', text: 'd' },
      { type: 'text', text: ' e ' },
      { type: 'em', text: 'f' },
    ])
  })

  it('does not re-scan markup inside inline code', () => {
    assert.deepEqual(parseInline('`**not bold**`'), [{ type: 'code', text: '**not bold**' }])
  })

  it('keeps a safe link but degrades an unsafe scheme to literal text', () => {
    assert.deepEqual(parseInline('[docs](https://example.com/x)'), [
      { type: 'link', text: 'docs', href: 'https://example.com/x' },
    ])
    // javascript: must never reach an href. The invariant that matters is
    // "no link span, and every character the model wrote is still shown" —
    // asserted that way so the test pins the security property rather than
    // one particular span split.
    for (const raw of [
      '[click](javascript:alert(1))',
      '[x](data:text/html,<script>)',
      '[y](vbscript:msgbox)',
    ]) {
      const spans = parseInline(raw)
      assert.ok(spans.every((s) => s.type !== 'link'), `${raw} must not produce a link`)
      assert.equal(spans.map((s) => s.text).join(''), raw)
    }
  })

  it('never emits an html span, so tags survive as characters', () => {
    const spans = parseInline('<img src=x onerror=alert(1)>')
    assert.equal(spans.length, 1)
    assert.equal(spans[0].type, 'text')
    assert.equal(spans[0].text, '<img src=x onerror=alert(1)>')
    assert.ok(spans.every((s) => s.type !== 'html'))
  })
})

describe('parseMarkdown', () => {
  it('returns nothing for empty or non-string input', () => {
    assert.deepEqual(parseMarkdown(''), [])
    assert.deepEqual(parseMarkdown(null), [])
    assert.deepEqual(parseMarkdown(undefined), [])
  })

  it('parses a fenced code block with its language', () => {
    const blocks = parseMarkdown('before\n\n```python\nx = 1\ny = 2\n```\n\nafter')
    assert.equal(blocks.length, 3)
    assert.equal(blocks[0].type, 'para')
    assert.deepEqual(blocks[1], { type: 'code', lang: 'python', text: 'x = 1\ny = 2' })
    assert.equal(textOf(blocks[2].spans), 'after')
  })

  it('treats an unclosed fence as code to the end (a turn still streaming)', () => {
    const blocks = parseMarkdown('```\npartial line')
    assert.deepEqual(blocks, [{ type: 'code', lang: '', text: 'partial line' }])
  })

  it('parses headings with their level', () => {
    const blocks = parseMarkdown('## Heading two')
    assert.equal(blocks[0].type, 'heading')
    assert.equal(blocks[0].level, 2)
    assert.equal(textOf(blocks[0].spans), 'Heading two')
  })

  it('parses bullet and ordered lists without merging them', () => {
    const blocks = parseMarkdown('- one\n- two\n\n1. first\n2. second')
    assert.equal(blocks.length, 2)
    assert.equal(blocks[0].type, 'list')
    assert.equal(blocks[0].ordered, false)
    assert.deepEqual(blocks[0].items.map(textOf), ['one', 'two'])
    assert.equal(blocks[1].ordered, true)
    assert.deepEqual(blocks[1].items.map(textOf), ['first', 'second'])
  })

  it('groups consecutive lines into one paragraph and splits on blank lines', () => {
    const blocks = parseMarkdown('line one\nline two\n\nsecond para')
    assert.equal(blocks.length, 2)
    assert.equal(textOf(blocks[0].spans), 'line one\nline two')
    assert.equal(textOf(blocks[1].spans), 'second para')
  })

  it('keeps code-block contents literal, including markup and backslashes', () => {
    const blocks = parseMarkdown('```\n<script>alert(1)</script>\n```')
    assert.deepEqual(blocks, [{ type: 'code', lang: '', text: '<script>alert(1)</script>' }])
  })

  it('emits only known block types (no html passthrough)', () => {
    const blocks = parseMarkdown('# h\n\ntext <b>x</b>\n\n- item\n\n```js\nq\n```')
    const kinds = new Set(blocks.map((b) => b.type))
    for (const kind of kinds) assert.ok(['para', 'heading', 'list', 'code'].includes(kind), kind)
    const para = blocks.find((b) => b.type === 'para' && textOf(b.spans).includes('<b>'))
    assert.ok(para, 'html-looking text survives as literal paragraph text')
  })
})

describe('link scheme allowlist — obfuscation resistance', () => {
  const hrefsOf = (spans) => spans.filter((s) => s.type === 'link').map((s) => s.href)

  it('blocks unsafe schemes regardless of case', () => {
    // The model controls this string, so `javascript:` must not survive in any
    // casing. A blocked link degrades to the literal characters it was written
    // as, carrying its own label.
    for (const scheme of ['javascript', 'JAVASCRIPT', 'JaVaScRiPt', 'data', 'DATA', 'vbscript', 'file', 'blob']) {
      const spans = parseInline(`[click](${scheme}:payload)`)
      assert.deepEqual(hrefsOf(spans), [], `${scheme}: became a link`)
      assert.match(textOf(spans), /click/)
    }
  })

  it('blocks schemes padded with leading whitespace', () => {
    for (const pad of [' ', '\t', '  ']) {
      const spans = parseInline(`[click](${pad}javascript:alert(1))`)
      assert.deepEqual(hrefsOf(spans), [], 'whitespace-padded scheme became a link')
    }
  })

  it('still allows the safe schemes, in any case', () => {
    assert.deepEqual(hrefsOf(parseInline('[a](https://example.com)')), ['https://example.com'])
    assert.deepEqual(hrefsOf(parseInline('[a](HTTP://example.com)')), ['HTTP://example.com'])
    assert.deepEqual(hrefsOf(parseInline('[a](mailto:x@y.z)')), ['mailto:x@y.z'])
  })

  it('never emits raw HTML for an html-shaped payload', () => {
    const spans = parseInline('<img src=x onerror="alert(1)">')
    assert.ok(spans.every((s) => s.type === 'text'), 'html-shaped input produced a non-text span')
    assert.match(textOf(spans), /<img src=x onerror="alert\(1\)">/)
  })
})

describe('markdown tables', () => {
  it('parses the staging light-state reply with inline code in cells', () => {
    const blocks = parseMarkdown('Done.\n\n| Light | State |\n|---|---|\n| Office (group) `light.office` | on, rgb 0,80,255, 100% |\n| WiZ DAEA0A | on, 100% |\n\nNo other lights.')
    assert.deepEqual(blocks.map((block) => block.type), ['para', 'table', 'para'])
    assert.equal(textOf(blocks[0].spans), 'Done.')
    assert.deepEqual(blocks[1].header.map(textOf), ['Light', 'State'])
    assert.equal(blocks[1].rows.length, 2)
    assert.deepEqual(blocks[1].rows[0][0], [
      { type: 'text', text: 'Office (group) ' },
      { type: 'code', text: 'light.office' },
    ])
    assert.deepEqual(blocks[1].rows.map((row) => row.map(textOf)), [
      ['Office (group) light.office', 'on, rgb 0,80,255, 100%'],
      ['WiZ DAEA0A', 'on, 100%'],
    ])
    assert.equal(textOf(blocks[2].spans), 'No other lights.')
  })

  it('pads short rows and truncates long rows to the header width', () => {
    const [table] = parseMarkdown('| A | B |\n|---|---|\n| short |\n| one | two | extra |')
    assert.deepEqual(table.rows.map((row) => row.map(textOf)), [['short', ''], ['one', 'two']])
  })

  it('keeps escaped pipes inside a single cell', () => {
    const [table] = parseMarkdown('| A | B |\n|---|---|\n| a \\| b | c |')
    assert.deepEqual(table.rows[0].map(textOf), ['a | b', 'c'])
  })

  it('keeps a pipe line without a following delimiter in its paragraph', () => {
    const blocks = parseMarkdown('before\n| not a table\nafter')
    assert.deepEqual(blocks, [{ type: 'para', spans: [{ type: 'text', text: 'before\n| not a table\nafter' }] }])
  })

  it('accepts alignment colons and a table immediately after prose', () => {
    const blocks = parseMarkdown('before\n| A | B |\n|:---|---:|\n| one | two |\nafter')
    assert.deepEqual(blocks.map((block) => block.type), ['para', 'table', 'para'])
    assert.deepEqual(blocks[1].header.map(textOf), ['A', 'B'])
    assert.deepEqual(blocks[1].rows[0].map(textOf), ['one', 'two'])
    assert.equal(textOf(blocks[2].spans), 'after')
  })

  it('accepts a single column and delimiter lines without outer pipes', () => {
    const [table] = parseMarkdown('  | A\n:---:\n  | value')
    assert.equal(table.type, 'table')
    assert.deepEqual(table.header.map(textOf), ['A'])
    assert.deepEqual(table.rows[0].map(textOf), ['value'])
  })

  it('caps body rows at 200 and drops the remaining table lines', () => {
    const rows = Array.from({ length: 205 }, (_, i) => `| ${i} |`)
    const blocks = parseMarkdown(['| A |', '|---|', ...rows, 'after'].join('\n'))
    assert.deepEqual(blocks.map((block) => block.type), ['table', 'para'])
    assert.equal(blocks[0].rows.length, 200)
    assert.equal(textOf(blocks[0].rows[199][0]), '199')
    assert.equal(textOf(blocks[1].spans), 'after')
  })
})
