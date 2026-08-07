// Consumer shim over the supported mushy-code package root. Keep Leaf callers
// off internal vendor paths so upstream can remove private modules safely.
export {
  guardedFetch,
  isAllowedMcpHost,
  isForbiddenMcpAddress,
  proxyTenantMcpServer,
  resolveAllowedMcpHost,
} from "../../vendor/mushy-author/index.js";
export type {
  McpHostResolver,
  McpServerConfig,
  ProxiedMcpServer,
} from "../../vendor/mushy-author/index.js";
