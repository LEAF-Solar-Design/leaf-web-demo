// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { createNotificationBus, useNotices, useToastBus } from './notifications.js'

afterEach(cleanup)

function Probe({ bus }) {
  const { toast, showToast } = useToastBus(bus)
  return (
    <button type="button" onClick={() => showToast({ text: 'hello' })}>
      {toast ? toast.text : 'no toast'}
    </button>
  )
}

function InboxProbe({ bus }) {
  const notices = useNotices(bus)
  return <span data-testid="count">{notices.length}</span>
}

describe('useToastBus / useNotices lifecycle', () => {
  it('mounting and unmounting the hook leaves zero listeners on the bus', () => {
    const bus = createNotificationBus(5)
    expect(bus.listenerCount()).toBe(0)
    const { unmount } = render(<Probe bus={bus} />)
    expect(bus.listenerCount()).toBe(1)
    unmount()
    expect(bus.listenerCount()).toBe(0)
  })

  it('two consumers (toast + inbox) over the same bus both unsubscribe cleanly', () => {
    const bus = createNotificationBus(5)
    const toastMount = render(<Probe bus={bus} />)
    const inboxMount = render(<InboxProbe bus={bus} />)
    expect(bus.listenerCount()).toBe(2)
    toastMount.unmount()
    expect(bus.listenerCount()).toBe(1)
    inboxMount.unmount()
    expect(bus.listenerCount()).toBe(0)
  })

  it('showToast updates the visible toast and useNotices sees the same kept notice', () => {
    const bus = createNotificationBus(5)
    render(<Probe bus={bus} />)
    render(<InboxProbe bus={bus} />)
    screen.getByRole('button', { name: 'no toast' }).click()
    expect(screen.getByRole('button')).toHaveTextContent('hello')
    expect(screen.getByTestId('count')).toHaveTextContent('1')
  })
})
