import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { postMessage } from '../../converse.js'
import { track } from '../../telemetry.js'
import QuestionsCard from './QuestionsCard.jsx'

vi.mock('../../telemetry.js', async (original) => ({ ...await original(), track: vi.fn() }))

const questions = [
  { id: 'q1', header: 'Scope', question: 'Which lines?', options: [
    { label: 'All', description: 'Include every layer' }, { label: 'Selected' },
  ] },
  { id: 'q2', header: 'Output', question: 'What should appear?', multiSelect: true,
    options: [{ label: 'Count' }, { label: 'Table', description: '<img src=x>' }] },
]
const props = { sessionId: 'session', questionId: 'card', questions }
const button = (name) => screen.getByRole('button', { name })
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.clearAllMocks() })

describe('QuestionsCard', () => {
  it('roves by question, selects with Enter and submits all selected labels once', async () => {
    const fetch = vi.fn().mockResolvedValue({ status: 202, json: async () => ({ turn_id: 'answer' }) })
    vi.stubGlobal('fetch', fetch)
    render(<QuestionsCard {...props} />)
    expect(button('Submit')).toBeDisabled()
    expect(track).toHaveBeenCalledWith('plan.questions_shown', { count: 2 })
    const all = button(/^All/)
    expect(all).toHaveTextContent('Include every layer')
    act(() => all.focus())
    fireEvent.keyDown(all, { key: 'ArrowRight' })
    expect(button('Selected')).toHaveFocus()
    expect(all.tabIndex).toBe(-1)
    expect(button('Selected').tabIndex).toBe(0)
    fireEvent.keyDown(button('Selected'), { key: 'Enter' })
    expect(button('Selected')).toHaveAttribute('aria-pressed', 'true')
    expect(button('Submit')).toBeDisabled()
    expect(button('Count').tabIndex).toBe(0)
    fireEvent.click(button('Count'))
    fireEvent.click(button(/Table/))
    fireEvent.click(button('Count'))
    expect(button('Count')).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(button('Count'))
    expect(document.querySelector('img')).toBeNull()
    await act(async () => fireEvent.click(button('Submit')))
    expect(fetch).toHaveBeenCalledTimes(1)
    const [url, options] = fetch.mock.calls[0]
    expect(url).toMatch(/\/sessions\/session\/messages$/)
    expect(JSON.parse(options.body)).toEqual({ answers: { question_id: 'card',
      answers: { q1: { labels: ['Selected'] }, q2: { labels: ['Table', 'Count'] } } } })
    expect(track).toHaveBeenCalledWith('plan.questions_answered', { count: 2, other_used: false })
    expect(screen.getByText('Table, Count')).toBeVisible()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('replaces labels with bounded Other text and keeps the draft after a failed send', async () => {
    const fetch = vi.fn().mockRejectedValueOnce(new Error()).mockResolvedValue({
      status: 202, json: async () => ({ turn_id: 'answer' }),
    })
    vi.stubGlobal('fetch', fetch)
    render(<QuestionsCard {...props} />)
    fireEvent.click(button(/^All/))
    fireEvent.click(button('Count'))
    const output = screen.getByRole('group', { name: /Output/ })
    fireEvent.click(within(output).getByRole('button', { name: 'Other' }))
    expect(button('Submit')).toBeDisabled()
    const other = screen.getByLabelText('Other answer for Output')
    expect(other.maxLength).toBe(500)
    fireEvent.change(other, { target: { value: '  <img src=x>  ' } })
    await act(async () => fireEvent.click(button('Submit')))
    expect(screen.getByRole('alert')).toHaveTextContent('Try again')
    await act(async () => fireEvent.click(button('Submit')))
    expect(JSON.parse(fetch.mock.calls[1][1].body).answers.answers.q2).toEqual({ other: '<img src=x>' })
    expect(track).toHaveBeenCalledWith('plan.questions_answered', { count: 2, other_used: true })
    expect(document.querySelector('img')).toBeNull()
  })

  it('renders only recorded answers on replay, including a card the user skipped', () => {
    const view = render(<QuestionsCard {...props} closed />)
    expect(screen.getAllByText('No answer recorded')).toHaveLength(2)
    view.rerender(<QuestionsCard {...props} closed answers={{ q1: { labels: ['All'] }, q2: { other: 'Schedule' } }} />)
    expect(screen.getByText('Schedule')).toBeVisible()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('rejects mixed answers turns before they reach the wire', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    for (const extra of [{ text: 'also' }, { confirm: {} }, { images: [] }, { queue: true }]) {
      await expect(postMessage('session', { answers: { question_id: 'card', answers: {} }, ...extra }))
        .rejects.toThrow('separate turn')
    }
    expect(fetch).not.toHaveBeenCalled()
  })

  it('guards freeform answers on the transport', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const other = 'api_key: ' + 'x'.repeat(24)
    await expect(postMessage('session', { answers: { question_id: 'card', answers: { q1: { other } } } }))
      .rejects.toMatchObject({ secretRefused: true })
    expect(fetch).not.toHaveBeenCalled()
    expect(JSON.stringify(track.mock.calls)).not.toContain(other)
  })
})
