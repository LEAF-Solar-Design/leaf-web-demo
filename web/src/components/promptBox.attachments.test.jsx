// @vitest-environment jsdom
//
// S22: image attachments in the command bar. With imageAttachmentsEnabled a
// pasted image, an image dropped on the G2 well, or one picked through the
// "+ add" chip lands as a quiet chip carrying its name and size, and the chip
// removes it. Every non-image file in a drop still reaches the G2 ingest
// strip. With the flag off (the catalog bar today) nothing here renders and a
// drop is pure G2.
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import PromptBox, { attachmentName, formatAttachmentSize } from './PromptBox.jsx'

const noop = () => {}
let revoke
let urlSeq

function mount(props = {}) {
  return render(
    <PromptBox
      value=""
      onChange={noop}
      onDispatch={noop}
      projectName="cat-panels"
      mcpDiscoveryEnabled={false}
      imageAttachmentsEnabled
      {...props}
    />,
  )
}

const bar = () => document.querySelector('[data-tour="command-bar"]')
const chips = () => screen.queryAllByTestId('attachment-chip')
const png = (name, bytes) => new File(['x'.repeat(bytes)], name, { type: 'image/png' })
const drop = (files) => fireEvent.drop(bar(), { dataTransfer: { files } })
const ingestStrip = () => document.querySelector('.strip-failed')

beforeEach(() => {
  urlSeq = 0
  revoke = vi.fn()
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = vi.fn(() => `blob:pending-${++urlSeq}`)
    static revokeObjectURL = revoke
  })
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve(null) })))
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('size and name labels', () => {
  it('formats bytes, kilobytes and megabytes', () => {
    expect(formatAttachmentSize(500)).toBe('500 B')
    expect(formatAttachmentSize(2048)).toBe('2.0 KB')
    expect(formatAttachmentSize(300 * 1024)).toBe('300 KB')
    expect(formatAttachmentSize(1.5 * 1024 * 1024)).toBe('1.5 MB')
    expect(formatAttachmentSize(undefined)).toBe('0 B')
    expect(formatAttachmentSize(-4)).toBe('0 B')
  })

  it('names an unnamed pasted image honestly', () => {
    expect(attachmentName({ file: new File(['x'], '', { type: 'image/png' }) })).toBe('Pasted image')
    expect(attachmentName({ file: new File(['x'], 'roof.png', { type: 'image/png' }) })).toBe('roof.png')
  })
})

describe('attachment chips carry name and size', () => {
  it('a pasted image renders a chip with its name, size and thumbnail', () => {
    mount()
    const file = png('roof-photo.png', 2048)
    fireEvent.paste(screen.getByTestId('command-bar'), {
      clipboardData: { items: [{ kind: 'file', type: 'image/png', getAsFile: () => file }] },
    })
    expect(chips()).toHaveLength(1)
    expect(screen.getByTestId('attachment-name')).toHaveTextContent('roof-photo.png')
    expect(screen.getByTestId('attachment-size')).toHaveTextContent('2.0 KB')
    expect(screen.getByAltText('Pending image attachment')).toHaveAttribute('src', 'blob:pending-1')
    expect(chips()[0]).toHaveAttribute('title', 'roof-photo.png, 2.0 KB')
  })

  it('removing a chip drops the attachment and revokes its object URL', () => {
    mount()
    drop([png('a.png', 600), png('b.png', 700)])
    expect(chips()).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: 'Remove image attachment a.png' }))
    expect(chips()).toHaveLength(1)
    expect(screen.getByTestId('attachment-name')).toHaveTextContent('b.png')
    expect(revoke).toHaveBeenCalledWith('blob:pending-1')
  })

  it('the dispatch carries the attached images', () => {
    const onDispatch = vi.fn()
    mount({ onDispatch, value: 'what is on this roof' })
    drop([png('roof.png', 900)])
    fireEvent.click(screen.getByRole('button', { name: 'Run', exact: true }))
    expect(onDispatch).toHaveBeenCalledTimes(1)
    const [, { images }] = onDispatch.mock.calls[0]
    expect(images).toHaveLength(1)
    expect(images[0].file.name).toBe('roof.png')
    expect(images[0].bytes).toBe(900)
  })
})

describe('the G2 drop well', () => {
  it('an image dropped on the bar becomes a chip and raises no ingest strip', () => {
    mount()
    fireEvent.dragEnter(bar(), { dataTransfer: { files: [] } })
    expect(document.querySelector('.bar-drop-hint')).toHaveTextContent('Drop an image to attach')
    drop([png('site-plan.png', 4096)])
    expect(chips()).toHaveLength(1)
    expect(screen.getByTestId('attachment-name')).toHaveTextContent('site-plan.png')
    expect(screen.getByTestId('attachment-size')).toHaveTextContent('4.0 KB')
    expect(ingestStrip()).toBeNull()
    expect(document.querySelector('.bar-drop-hint')).toBeNull()
  })

  it('a non-image drop still reaches G2 ingest', () => {
    mount()
    drop([new File(['{}'], 'manifest.json', { type: 'application/json' })])
    expect(chips()).toHaveLength(0)
    expect(ingestStrip()).toHaveTextContent('manifest.json wasn’t ingested')
  })

  it('a mixed drop attaches the image and sends the rest to G2 ingest', () => {
    mount()
    drop([png('roof.png', 800), new File(['dxf'], 'site.dxf', { type: '' })])
    expect(chips()).toHaveLength(1)
    expect(screen.getByTestId('attachment-name')).toHaveTextContent('roof.png')
    expect(ingestStrip()).toHaveTextContent('site.dxf wasn’t ingested')
  })

  it('an over-cap drop attaches nothing and says why', () => {
    mount()
    drop([png('1.png', 10), png('2.png', 10), png('3.png', 10), png('4.png', 10)])
    expect(chips()).toHaveLength(0)
    expect(screen.getByRole('alert')).toHaveTextContent('At most 3 images per message.')
  })

  it('with the flag off an image drop is pure G2 and no add chip renders', () => {
    mount({ imageAttachmentsEnabled: false })
    expect(screen.queryByRole('button', { name: 'Add image attachment' })).toBeNull()
    expect(screen.queryByTestId('attachment-input')).toBeNull()
    drop([png('roof.png', 800)])
    expect(chips()).toHaveLength(0)
    expect(ingestStrip()).toHaveTextContent('roof.png wasn’t ingested')
  })

  it('with the flag off an image paste is refused with the reply-box pointer', () => {
    mount({ imageAttachmentsEnabled: false })
    const file = png('roof.png', 800)
    fireEvent.paste(screen.getByTestId('command-bar'), {
      clipboardData: { items: [{ kind: 'file', type: 'image/png', getAsFile: () => file }] },
    })
    expect(chips()).toHaveLength(0)
    expect(screen.getByText('Image paste is available in the assistant reply box.')).toBeInTheDocument()
  })
})

describe('the "+ add" chip', () => {
  it('opens the image file picker and attaches what is picked', () => {
    const click = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(() => {})
    mount()
    const add = screen.getByRole('button', { name: 'Add image attachment' })
    expect(add).toHaveTextContent('+ add')
    fireEvent.click(add)
    expect(click).toHaveBeenCalledTimes(1)
    const input = screen.getByTestId('attachment-input')
    expect(input).toHaveAttribute('type', 'file')
    expect(input.getAttribute('accept')).toContain('image/png')
    fireEvent.change(input, { target: { files: [png('picked.png', 3 * 1024)] } })
    expect(chips()).toHaveLength(1)
    expect(screen.getByTestId('attachment-name')).toHaveTextContent('picked.png')
    expect(screen.getByTestId('attachment-size')).toHaveTextContent('3.0 KB')
  })

  it('a picked non-image is refused, not attached', () => {
    mount()
    fireEvent.change(screen.getByTestId('attachment-input'), {
      target: { files: [new File(['%PDF'], 'spec.pdf', { type: 'application/pdf' })] },
    })
    expect(chips()).toHaveLength(0)
    expect(screen.getByRole('alert')).toHaveTextContent('Only PNG, JPEG, WebP or GIF images can be attached.')
  })
})
