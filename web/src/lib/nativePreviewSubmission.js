const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

// Persist only the submission identity. Job state still comes from the server.
export function nativePreviewSubmissionKey(storage, identity, replace = false) {
  if (!Array.isArray(identity) || identity.length !== 6
    || identity.slice(0, 4).some(value => typeof value !== 'string' || !value || value.length > 1024)
    || !Number.isSafeInteger(identity[4]) || identity[4] < 1
    || typeof identity[5] !== 'string' || !/^[0-9a-f]{64}$/.test(identity[5])) throw new Error('Invalid preview identity.')
  const name = `leaf.portfolio.native-preview.v1:${JSON.stringify(identity)}`
  const previous = storage.getItem(name)
  if (previous !== null && !replace) {
    if (previous.length > 128) throw new Error('Invalid saved preview identity.')
    const value = JSON.parse(previous)
    if (!value || Object.keys(value).sort().join(',') !== 'key,version'
      || value.version !== 1 || typeof value.key !== 'string' || !UUID.test(value.key)) throw new Error('Invalid saved preview identity.')
    return value.key
  }
  const key = crypto.randomUUID()
  if (!UUID.test(key)) throw new Error('Invalid preview identity.')
  const serialized = JSON.stringify({ version: 1, key })
  storage.setItem(name, serialized)
  if (storage.getItem(name) !== serialized) throw new Error('Preview identity was not saved.')
  return key
}
