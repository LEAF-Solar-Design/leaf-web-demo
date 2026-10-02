import { spawn } from 'node:child_process'
import { access, readdir, stat } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const entrypoint = fileURLToPath(import.meta.url)
const webDir = fileURLToPath(new URL('../../', import.meta.url))
const harnessDir = fileURLToPath(new URL('../../../harness/', import.meta.url))
const config = fileURLToPath(new URL('../../playwright.regressions.config.mjs', import.meta.url))
const playwrightCLI = fileURLToPath(new URL('../../node_modules/@playwright/test/cli.js', import.meta.url))
const drills = Object.freeze({
  pass: fileURLToPath(new URL('./gate-fixtures/pass.case.mjs', import.meta.url)),
  failure: fileURLToPath(new URL('./gate-fixtures/failure.case.mjs', import.meta.url)),
  skip: fileURLToPath(new URL('./gate-fixtures/skip.case.mjs', import.meta.url)),
  empty: fileURLToPath(new URL('./gate-fixtures/empty.case.mjs', import.meta.url)),
})

export function gateInvocation(argv = [], ambient = process.env) {
  if (argv.length && (argv.length !== 2 || argv[0] !== '--drill'
    || !Object.hasOwn(drills, argv[1]))) {
    throw new Error('Usage: runGate.mjs [--drill pass|failure|skip|empty]')
  }
  const env = { ...ambient }
  delete env.LEAF_REGRESSION_SPEC
  delete env.LEAF_REGRESSION_REPORT
  if (argv.length) env.LEAF_REGRESSION_SPEC = drills[argv[1]]
  return {
    executable: process.execPath,
    args: [playwrightCLI, 'test', '--config', config],
    options: { cwd: webDir, env, windowsHide: true, stdio: 'inherit', shell: false },
  }
}

export async function harnessNeedsBuild(directory = harnessDir) {
  let output
  try {
    output = await stat(resolve(directory, 'dist/scripts/serve.js'))
  } catch (error) {
    if (error.code === 'ENOENT') return true
    throw error
  }
  const inputs = ['tsconfig.json', 'tsconfig.build.json', 'package.json', 'package-lock.json']
  async function collect(relative) {
    for (const entry of await readdir(resolve(directory, relative), { withFileTypes: true })) {
      const path = join(relative, entry.name)
      if (entry.isDirectory()) await collect(path)
      else if (entry.isFile() && entry.name.endsWith('.ts')) inputs.push(path)
    }
  }
  await collect('src')
  await collect('scripts')
  for (const input of inputs) {
    if ((await stat(resolve(directory, input))).mtimeMs > output.mtimeMs) return true
  }
  return false
}

async function runChild(invocation, spawnChild, timeoutMs) {
  return await new Promise((accept) => {
      const child = spawnChild(invocation.executable, invocation.args, invocation.options)
      let timedOut = false
      const timer = setTimeout(() => {
        timedOut = true
        console.error(`Regression gate timed out after ${timeoutMs}ms`)
        try { child.kill('SIGKILL') } catch (error) {
          console.error(`Regression gate termination failed: ${error.message}`)
        }
        accept(124)
      }, timeoutMs)
      child.once('error', (error) => {
        clearTimeout(timer)
        console.error(`Regression gate spawn failed: ${error.message}`)
        accept(1)
      })
      child.once('close', (code, signal) => {
        clearTimeout(timer)
        if (signal) console.error(`Regression gate child terminated: ${signal}`)
        accept(timedOut ? 124 : (Number.isInteger(code) && code >= 0 ? code : 1))
      })
  })
}

export async function runGate(argv = [], {
  spawnChild = spawn, checkCLI = access, timeoutMs = 660_000,
  needsHarnessBuild = harnessNeedsBuild,
} = {}) {
  try {
    const invocation = gateInvocation(argv)
    await checkCLI(playwrightCLI)
    const deadline = Date.now() + timeoutMs
    if (!argv.length && await needsHarnessBuild()) {
      console.log('Regression gate: compiling missing or stale harness output')
      // Run the installed compiler for `npx tsc -p tsconfig.build.json`
      // directly, without a shell or an implicit package download.
      const compiler = resolve(harnessDir, 'node_modules/typescript/bin/tsc')
      await checkCLI(compiler)
      const code = await runChild({
        executable: process.execPath,
        args: [compiler, '-p', 'tsconfig.build.json'],
        options: { ...invocation.options, cwd: harnessDir },
      }, spawnChild, Math.max(1, deadline - Date.now()))
      if (code !== 0) {
        console.error(`Regression gate: harness compilation failed (exit ${code})`)
        return code
      }
    }
    if (Date.now() >= deadline) {
      console.error('Regression gate timed out before Playwright invocation')
      return 124
    }
    return await runChild(invocation, spawnChild, deadline - Date.now())
  } catch (error) {
    console.error(`Regression gate failed: ${error.message}`)
    return 1
  }
}

if (process.argv[1] && resolve(process.argv[1]) === entrypoint) {
  process.exitCode = await runGate(process.argv.slice(2))
}
