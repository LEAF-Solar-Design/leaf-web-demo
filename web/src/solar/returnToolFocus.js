function visible(control) {
  if (!control?.isConnected) return false
  for (let element = control; element; element = element.parentElement) {
    const style = getComputedStyle(element)
    if (element.hidden || style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false
  }
  return true
}

export default function returnToolFocus(name) {
  // CSS.escape is available in browsers; DOM-only test hosts may omit it.
  const buttons = globalThis.CSS?.escape
    ? document.querySelectorAll(`button[data-tool="${CSS.escape(name)}"]`)
    : [...document.querySelectorAll('button[data-tool]')].filter((button) => button.dataset.tool === name)
  const button = [...buttons].find(visible)
  if (button) {
    button.focus()
    return
  }
  const overflowButton = [...buttons].find((candidate) => candidate.closest('#drafting-ribbon-panels'))
  if (!overflowButton) return
  const more = document.querySelector('button[aria-controls="drafting-ribbon-panels"]')
  if (visible(more) && more.getAttribute('aria-expanded') === 'false') more.focus()
}
