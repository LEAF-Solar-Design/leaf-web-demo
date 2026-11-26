import { describe, expect, it } from 'vitest'
import { planLabel } from './planLabel.js'

describe('planLabel', () => {
  it.each([
    ['hosted_free', 'Free'],
    ['hosted_starter', 'Starter'],
    ['hosted_pro', 'Pro'],
    ['self_hosted', 'Self-hosted'],
    ['demo', 'Demo'],
    ['demo_live', 'Demo'],
    ['admin', 'Admin'],
    ['restricted', 'Restricted'],
    ['guest', 'Guest'],
    ['future_plan', 'future_plan'],
    ['toString', 'toString'],
    [null, ''],
    [undefined, ''],
    ['', ''],
  ])('labels %s as %s', (tier, label) => {
    expect(planLabel(tier)).toBe(label)
  })
})
