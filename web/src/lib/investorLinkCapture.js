export const INVESTOR_LINK_GLOBAL = '__leafInvestorLink'
export const INVESTOR_LINK_PATTERN = /^v1\.[0-9a-f]{8}\.[1-9][0-9]{9}\.[1-9][0-9]{9}\.[A-Za-z0-9_-]{43}$/

// Executed by the HTML parser before the application or its telemetry loads.
export const INVESTOR_LINK_CAPTURE_SOURCE = `(function () {
  try {
    var hash = location.hash;
    if (hash.indexOf('#investor=') !== 0) return;
    history.replaceState(history.state, '', location.pathname + location.search);
    var token = hash.slice(10);
    Object.defineProperty(window, '${INVESTOR_LINK_GLOBAL}', {
      value: ${INVESTOR_LINK_PATTERN}.test(token) ? token : '',
      configurable: true, enumerable: false, writable: false
    });
  } catch (ignored) {}
})();`

export function takeInvestorLink(win = window) {
  if (!Object.prototype.hasOwnProperty.call(win, INVESTOR_LINK_GLOBAL)) return undefined
  const token = win[INVESTOR_LINK_GLOBAL]
  delete win[INVESTOR_LINK_GLOBAL]
  return typeof token === 'string' ? token : ''
}
