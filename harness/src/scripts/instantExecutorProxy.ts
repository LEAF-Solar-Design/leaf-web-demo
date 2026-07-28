import { startInstantExecutorProxyFromEnv } from "../ports/impl/instantExecutorProxy.js";

const server = await startInstantExecutorProxyFromEnv();

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    server.close(() => process.exit(0));
  });
}
