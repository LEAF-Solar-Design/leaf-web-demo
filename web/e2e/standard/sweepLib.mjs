export const WIDTHS = [[390, 844], [768, 1024], [1024, 768], [1440, 900]]

// Keep every browser-side helper inside this function for page.evaluate.
export function auditPage() {
  const vw = innerWidth
  const docScrollW = document.documentElement.scrollWidth
  const result = {
    vw, docScrollW, pageScrollsX: docScrollW > vw + 1,
    offscreenCount: 0, offscreen: [],
    clippedCount: 0, clipped: [], ellipsisCount: 0,
    smallTextCount: 0, smallText: [],
    smallTargetsCount: 0, smallTargets: [],
  }
  function shortPath(element) {
    const parts = []
    for (let node = element; node && parts.length < 4; node = node.parentElement) {
      let part = node.tagName.toLowerCase()
      if (node.id) {
        parts.unshift(`${part}#${CSS.escape(node.id)}`)
        break
      }
      part += Array.from(node.classList).slice(0, 2).map(name => `.${CSS.escape(name)}`).join('')
      parts.unshift(part)
    }
    return parts.join(' > ')
  }
  function sample(list, element, numbers) {
    if (list.length < 12) {
      list.push({ el: shortPath(element), text: (element.innerText || '').trim().slice(0, 40), ...numbers })
    }
  }
  function scrollableAncestor(element) {
    for (let parent = element.parentElement; parent; parent = parent.parentElement) {
      if (['auto', 'scroll'].includes(getComputedStyle(parent).overflowX)) return true
    }
    return false
  }
  for (const element of document.querySelectorAll('*')) {
    const style = getComputedStyle(element)
    const rect = element.getBoundingClientRect()
    if (style.display === 'none' || style.visibility === 'hidden' ||
        !(Number(style.opacity) > 0.05) || rect.width <= 0 || rect.height <= 0) continue

    if (rect.right > vw + 1 && rect.left < vw && style.position !== 'fixed' && !scrollableAncestor(element)) {
      result.offscreenCount++
      sample(result.offscreen, element, { left: rect.left, right: rect.right, width: rect.width })
    }
    const clipsX = ['hidden', 'clip'].includes(style.overflowX) && element.scrollWidth > element.clientWidth + 2
    const clipsY = ['hidden', 'clip'].includes(style.overflowY) && element.clientHeight > 0 &&
      element.scrollHeight > element.clientHeight + 2
    if ((element.innerText || '').trim() && (clipsX || clipsY)) {
      if (style.textOverflow === 'ellipsis') result.ellipsisCount++
      else {
        result.clippedCount++
        sample(result.clipped, element, {
          scrollWidth: element.scrollWidth, clientWidth: element.clientWidth,
          scrollHeight: element.scrollHeight, clientHeight: element.clientHeight,
        })
      }
    }
    const fontSize = parseFloat(style.fontSize)
    const ownText = Array.from(element.childNodes).some(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim())
    if (ownText && fontSize < 12) {
      result.smallTextCount++
      sample(result.smallText, element, { fontSize })
    }
    if (vw <= 430 && element.matches('a[href], button, [role=button], input, select, textarea, [role=tab]') &&
        (rect.width < 44 || rect.height < 44)) {
      result.smallTargetsCount++
      sample(result.smallTargets, element, { width: rect.width, height: rect.height })
    }
  }
  return result
}

// Worst entries retain the full row so the report identifies the surface and viewport.
export function summarize(rows) {
  const totals = {}
  const worst = {}
  for (const detector of ['pageScrollsX', 'offscreenCount', 'clippedCount', 'ellipsisCount', 'smallTextCount', 'smallTargetsCount']) {
    totals[detector] = 0
    worst[detector] = null
    for (const row of rows) {
      const count = Number(row[detector] ?? 0)
      totals[detector] += count
      if (worst[detector] === null || count > Number(worst[detector][detector] ?? 0)) worst[detector] = row
    }
  }
  return { totals, worst }
}
