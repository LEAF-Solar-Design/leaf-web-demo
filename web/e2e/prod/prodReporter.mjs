import { CANARY_INVENTORY, InventoryRun, validateReceipt } from './prodInventory.mjs'

export const RECEIPT_PREFIX = 'PROD_READONLY_RECEIPT '
const identity = (test) => ({
  file: test.location.file, title: test.title, expectedStatus: test.expectedStatus,
  annotations: test.annotations, repeatEachIndex: test.repeatEachIndex,
})

export default class ProdReporter {
  constructor(options = {}) { this.run = new InventoryRun(options.canary ? CANARY_INVENTORY : undefined) }
  printsToStdio() { return true }
  onBegin(config, suite) {
    try {
      if (config.projects.length !== 1 || config.projects[0].retries !== 0 || config.projects[0].repeatEach !== 1
          || config.shard || config.projects[0].use.browserName !== 'chromium') this.run.error()
      this.run.collect(suite.allTests().map(identity))
    } catch { this.run.error() }
  }
  onTestBegin(test, result) { try { this.run.begin(identity(test), result) } catch { this.run.error() } }
  onTestEnd(test, result) {
    try {
      this.run.end(identity(test), result)
      for (const annotation of test.annotations.filter(({ type }) => type === 'prod-denials')) {
        const counts = JSON.parse(annotation.description)
        // Validate and copy only the fixed code vocabulary; never print annotations.
        const receipt = this.run.finish('failed')
        const safe = validateReceipt({ ...receipt, denials: counts }, this.run.expected)
        for (const [code, count] of Object.entries(safe.denials)) {
          this.run.denials[code] = (this.run.denials[code] || 0) + count
          if (count) this.run.error()
        }
      }
    } catch { this.run.error() }
  }
  onError() { this.run.error() }
  // Raw errors, steps, stdout/stderr and attachments intentionally have no output hooks.
  onEnd(result) {
    let receipt
    try { receipt = validateReceipt(this.run.finish(result.status), this.run.expected) } catch {
      this.run.error()
      receipt = this.run.finish('failed')
    }
    process.stdout.write(`${RECEIPT_PREFIX}${JSON.stringify(receipt)}\n`)
    return { status: receipt.status }
  }
}
