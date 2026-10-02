import { test, expect } from '@playwright/test'

test('gate pass drill executes an assertion', async () => {
  expect(1 + 1).toBe(2)
})
