// @vitest-environment jsdom
import React from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import LocalCadConnect from './LocalCadConnect.jsx'

afterEach(cleanup)

describe('LocalCadConnect unreachable', () => {
  it('an unreachable bridge shows only the not-found line, not the transport error', () => {
    render(<LocalCadConnect status="unreachable" error="Failed to fetch" />)

    expect(screen.getByTestId('local-cad-status').textContent).toBe('AutoCAD: not found on this computer')
    const statusText = screen.getByTestId('local-cad-status').closest('[aria-live]').textContent
    expect(statusText).not.toContain('Failed to fetch')
    expect(statusText).not.toContain('The last update failed')
  })

  it('a bridge that answered still reports its update error', () => {
    for (const status of ['waiting', 'connected']) {
      render(<LocalCadConnect status={status} drawing="site.dwg" error="conversion failed" />)

      expect(screen.getByTestId('local-cad-status').closest('[aria-live]').textContent)
        .toContain('. The last update failed: conversion failed')
      cleanup()
    }
  })

  it('every browser wording of a network failure is hidden', () => {
    for (const error of ['Failed to fetch', 'NetworkError when attempting to fetch resource.', 'Load failed']) {
      render(<LocalCadConnect status="unreachable" error={error} />)

      const statusText = screen.getByTestId('local-cad-status').closest('[aria-live]').textContent
      expect(statusText).not.toContain('The last update failed')
      expect(statusText).not.toContain(error)
      cleanup()
    }
  })

  it('an unreachable bridge keeps errors from a connector that answered or hung', () => {
    for (const error of ['Bridge HTTP 500', 'Invalid bridge response', 'Bridge request timed out']) {
      render(<LocalCadConnect status="unreachable" error={error} />)

      expect(screen.getByTestId('local-cad-status').closest('[aria-live]').textContent)
        .toContain(`. The last update failed: ${error}`)
      cleanup()
    }
  })
})
