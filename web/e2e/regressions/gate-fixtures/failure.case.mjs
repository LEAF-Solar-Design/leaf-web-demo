import { test, expect } from '@playwright/test'

test('gate failure drill fails an assertion', async () => {
  expect(false, 'W3E_GATE_FAILURE_DRILL').toBe(true)
})
