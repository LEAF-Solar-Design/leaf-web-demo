const fs = require('node:fs'), assert = require('node:assert/strict')
const { rpc, ctx, copies, intake, protocolStats, resources, dispose } = require('./x07_adapter_jsx_journey.cjs')
async function main() {
  try {
    const label = 'Pánel 雪😀', layer = 'Módules Ø'
    for (const row of intake.inserts) { row.name = label; row.layer = layer }
    for (const copy of copies) { copy.block = label; copy.layer = layer }
    const context = ctx('chunked-utf8')
    const first = await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })
    for (const pattern of [[1], [2,1,3], [7,2,1,4]]) {
      const start = protocolStats.length
      const result = await rpc(context, 'editor_load', context, context.customer_key, null, null, null, { chunk_pattern: pattern })
      assert.equal(protocolStats.length, start+1)
      const stats = protocolStats.at(-1)
      assert.equal(stats.receipts, 1); assert.ok(stats.parts > 1)
      if (pattern.length === 1) assert.equal(stats.parts, stats.bytes)
      assert.equal(result.roles.module.block, label); assert.equal(result.roles.module.layer, layer)
      assert.equal(result.roles.module.block.includes('\uFFFD'), false)
      assert.equal(resources().child_count, 0)
    }
    const whitespaceTrimmed = await rpc(context, 'editor_load', context, context.customer_key, null, null, null,
      { chunk_pattern: [1], truncate_bytes: 1 })
    assert.equal(whitespaceTrimmed.roles.module.block, label)
    assert.equal(protocolStats.at(-1).receipts, 1)
    for (const bytes of [1, 5, 20]) {
      await assert.rejects(rpc(context, 'editor_load', context, context.customer_key, null, null, null,
        { chunk_pattern: [1], truncate_json_bytes: bytes }), error => {
          assert.equal(error.code, 'BRIDGE_MALFORMED_JSON'); assert.ok(error.message.length < 100); return true
        })
      assert.equal(protocolStats.at(-1).receipts, 0)
      assert.equal(resources().child_count, 0)
    }
    assert.deepEqual((await rpc(context, 'load', context)).application_record, first.applied_profile)
    console.log('PASS: one complete receipt across byte/token/multibyte splits, preserved Unicode, bounded truncation refusal and child cleanup')
  } finally {
    await dispose(); assert.equal(resources().child_count, 0)
    assert.equal(fs.existsSync(resources().database), false); assert.equal(fs.existsSync(resources().root), false)
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
