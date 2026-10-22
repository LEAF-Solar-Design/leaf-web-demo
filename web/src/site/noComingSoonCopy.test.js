import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')

describe('live sign-in copy', () => {
  it('does not promise that the live sign-in is coming soon', () => {
    expect(source).not.toContain('Sign-in for the live surface is coming soon')
  })

  it('keeps the live sign-in invitation', () => {
    expect(source).toContain('Sign in to load your tools and drawings')
  })

  it('keeps the sign-in button and login wiring', () => {
    expect(source).toContain('onClick={onSignIn}>Sign in</button>')
    expect(source).toMatch(/onSignIn=\{\(\) => \{[\s\S]*?onLogin\(\)/)
  })
})
