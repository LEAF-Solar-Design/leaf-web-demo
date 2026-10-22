import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const root = new URL('../', import.meta.url);
const destination = new URL('web/src/lab/contract/', root);
const files = {
  'lab-block.v1.schema.json': 'contract/lab-block.v1.schema.json',
  'lab-pin.v1.schema.json': 'contract/lab-pin.v1.schema.json',
  'lab-graph.v1.schema.json': 'contract/lab-graph.v1.schema.json',
  'lab-embed.v1.schema.json': 'contract/lab-embed.v1.schema.json',
  'leaf-block-manifest.v1.schema.json': 'contract/fixtures/lab/leaf-block-manifest.v1.schema.json',
  'fixtures/ops-conformance.json': 'contract/fixtures/lab/ops-conformance.json',
};
const check = process.argv.includes('--check');
if (process.argv.slice(2).some(argument => argument !== '--check')) throw new Error('Usage: node scripts/sync-lab-contracts.mjs [--check]');
const pins = {};
let drift = false;
async function sync(name, bytes) {
  const target = new URL(name, destination);
  if (check) {
    const current = await readFile(target).catch(error => {
      if (error.code !== 'ENOENT') throw error;
      return null;
    });
    if (!current || !current.equals(bytes)) {
      console.error(`Lab contract drift: ${fileURLToPath(target)}`);
      drift = true;
    }
  } else {
    await mkdir(new URL('.', target), { recursive: true });
    await writeFile(target, bytes);
  }
}
for (const [name, source] of Object.entries(files)) {
  const bytes = await readFile(new URL(source, root));
  pins[name] = { source, sha256: createHash('sha256').update(bytes).digest('hex') };
  await sync(name, bytes);
}
await sync('PIN.json', Buffer.from(`${JSON.stringify({ files: pins }, null, 2)}\n`));
if (drift) process.exitCode = 1;
