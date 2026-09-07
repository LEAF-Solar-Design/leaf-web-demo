// Kills by port without an ownership check. This is appropriate for an
// exclusively owned CI runner, but can terminate a developer's own server
// on a workstation.
import { execFile } from 'node:child_process'
import { writeFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { promisify } from 'node:util'

function validate(ports, receiptPath) {
  if (!Array.isArray(ports) || !ports.length
    || ports.some((port) => !Number.isInteger(port) || port < 1 || port > 65535)
    || typeof receiptPath !== 'string' || !receiptPath.trim()) {
    throw new Error('Expected ports in 1..65535 and a receipt path')
  }
}

export async function clearStalePorts({ ports, receiptPath, timeoutMs = 10_000, log = console.log }) {
  validate(ports, receiptPath)
  const receipt = { ok: false, remaining: [], signalled: [], error: null }
  const deadline = Date.now() + timeoutMs
  const budget = () => {
    const left = deadline - Date.now()
    if (left <= 0) throw new Error('Port cleanup exceeded ' + timeoutMs + ' ms')
    return left
  }
  const run = (file, args) => promisify(execFile)(file, args, {
    timeout: Math.max(1, Math.min(750, budget())),
    killSignal: 'SIGKILL', windowsHide: true,
  })
  const pause = async (ms) => {
    await new Promise((resolve) => setTimeout(resolve, Math.min(ms, budget())))
    budget()
  }
  const listeners = async () => {
    const found = []
    if (process.platform === 'win32') {
      const { stdout } = await run('netstat', ['-ano'])
      for (const line of stdout.split(/\r?\n/)) {
        const fields = line.trim().split(/\s+/)
        const port = Number(fields[1]?.split(':').pop())
        if (fields[0] === 'TCP' && fields[3] === 'LISTENING' && ports.includes(port)) {
          found.push({ port, pid: Number(fields[4]) || null })
        }
      }
    } else {
      try {
        for (const port of ports) {
          let stdout
          try {
            ;({ stdout } = await run('lsof', ['-nP', '-ti', 'tcp:' + port, '-sTCP:LISTEN']))
          } catch (error) {
            if (error.code === 1 && !error.stdout && !error.stderr) continue
            throw error
          }
          for (const pid of stdout.trim().split(/\s+/).filter(Boolean)) {
            found.push({ port, pid: Number(pid) || null })
          }
        }
      } catch {
        found.length = 0
        const { stdout } = await run('ss', ['-ltnp'])
        for (const line of stdout.split(/\r?\n/)) {
          const fields = line.trim().split(/\s+/)
          const port = Number(fields[3]?.split(':').pop())
          if (fields[0] !== 'LISTEN' || !ports.includes(port)) continue
          const pids = [...line.matchAll(/pid=(\d+)/g)]
          if (!pids.length) found.push({ port, pid: null })
          for (const match of pids) found.push({ port, pid: Number(match[1]) })
        }
      }
    }
    return found.filter((item, index) => found.findIndex((other) => other.port === item.port && other.pid === item.pid) === index)
  }
  const signal = (items, name) => {
    for (const { port, pid } of items) {
      if (!Number.isInteger(pid) || pid <= 0 || pid === process.pid) {
        throw new Error('Cannot kill listener on port ' + port + ' pid ' + pid)
      }
      try {
        process.kill(pid, name)
        receipt.signalled.push({ port, pid, signal: name })
        log('[link-flow cleanup] ' + name + ' pid ' + pid + ' port ' + port)
      } catch (error) {
        if (error.code !== 'ESRCH') throw error
      }
    }
  }
  try {
    receipt.remaining = await listeners()
    if (receipt.remaining.length) {
      signal(receipt.remaining, 'SIGTERM')
      await pause(3000)
      receipt.remaining = await listeners()
      if (receipt.remaining.length) {
        signal(receipt.remaining, 'SIGKILL')
        await pause(200)
        receipt.remaining = await listeners()
      }
    }
    budget()
    if (receipt.remaining.length) throw new Error('Listeners remain: ' + JSON.stringify(receipt.remaining))
    receipt.ok = true
  } catch (error) {
    receipt.error = error.message
  }
  writeFileSync(receiptPath, JSON.stringify(receipt) + '\n')
  if (receipt.error || receipt.signalled.length) log('[link-flow cleanup] ' + JSON.stringify(receipt))
  return receipt
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  let ports
  let receiptPath
  try {
    const args = process.argv.slice(2)
    for (let i = 0; i < args.length; i += 2) {
      if (args[i] === '--ports' && args[i + 1] !== undefined) {
        ports = args[i + 1].split(',').map((port) => port.trim() ? Number(port) : NaN)
      } else if (args[i] === '--receipt' && args[i + 1] && !args[i + 1].startsWith('--')) {
        receiptPath = args[i + 1]
      } else {
        throw new Error('Expected --ports <list> --receipt <file>')
      }
    }
    validate(ports, receiptPath)
  } catch (error) {
    console.error(error.message)
    process.exitCode = 2
  }
  if (!process.exitCode) {
    try {
      const receipt = await clearStalePorts({ ports, receiptPath })
      process.exitCode = receipt.ok ? 0 : 1
    } catch (error) {
      console.error(error.message)
      process.exitCode = 1
    }
  }
}
