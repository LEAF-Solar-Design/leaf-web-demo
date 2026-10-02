import { spawn as nodeSpawn } from 'node:child_process'
import { setTimeout as delay } from 'node:timers/promises'

export async function readProcessTable({ spawn = nodeSpawn, platform = process.platform, timeoutMs = 30000, maxBytes = 8 * 1024 * 1024 } = {}) {
  const windows = platform === 'win32'
  const child = windows
    ? spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', 'Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,WorkingSetSize,Name | ConvertTo-Json -Compress'], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    : spawn('ps', ['-e', '-o', 'pid=,ppid=,rss='], { stdio: ['ignore', 'pipe', 'pipe'] })
  let stdout = ''
  let stderr = ''
  await new Promise((accept, reject) => {
    let settled = false
    const finish = (error) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (error) reject(error)
      else accept()
    }
    const failAndKill = (message) => {
      if (settled) return
      finish(new Error(message))
      child.kill('SIGKILL')
    }
    const timer = setTimeout(() => failAndKill('Process table probe timed out'), timeoutMs)
    child.stdout.on('data', (data) => {
      if (settled) return
      stdout += data.toString()
      if (Buffer.byteLength(stdout) > maxBytes) failAndKill('Process table exceeds 8 MiB')
    })
    child.stderr.on('data', (data) => { if (!settled) stderr = (stderr + data).slice(-4000) })
    child.once('error', finish)
    child.once('close', (code) => finish(code === 0 ? undefined : new Error(`Cannot measure stack process tree: ${stderr || code}`)))
  })
  if (windows) {
    try {
      const data = JSON.parse(stdout)
      return (Array.isArray(data) ? data : [data]).map((row) => ({ pid: Number(row.ProcessId), parent: Number(row.ParentProcessId), rss: Number(row.WorkingSetSize), name: row.Name }))
    } catch (error) {
      throw new Error(`Cannot measure stack process tree: ${error.message}`, { cause: error })
    }
  }
  return stdout.trim().split('\n').filter(Boolean).map((line) => {
    const [pid, parent, rss] = line.trim().split(/\s+/).map(Number)
    return { pid, parent, rss: rss * 1024 }
  })
}

export async function measureWithRetry(measure, { attempts = 3, delayMs = 500, sleep = delay } = {}) {
  if (!Number.isInteger(attempts) || attempts < 1 || attempts > 5) throw new RangeError('attempts must be an integer from 1 to 5')
  let lastError
  for (let attempt = 0; attempt < attempts; attempt++) {
    try { return await measure() } catch (error) { lastError = error }
    if (attempt + 1 < attempts) await sleep(delayMs)
  }
  throw new Error(`Process-tree measurement failed after ${attempts} attempts`, { cause: lastError })
}
