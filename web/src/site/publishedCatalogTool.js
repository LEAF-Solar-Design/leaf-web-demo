export function resolvePublishedCatalogTool(provisionalTool, refreshedTools) {
  const name = provisionalTool?.name
  const tool = Array.isArray(refreshedTools)
    ? refreshedTools.find((candidate) => candidate?.name === name)
    : null

  if (!tool || typeof tool.catalog_digest !== 'string' || !tool.catalog_digest) {
    throw new Error('The published tool is not available in the runnable catalog yet. Refresh and try again.')
  }

  return tool
}
