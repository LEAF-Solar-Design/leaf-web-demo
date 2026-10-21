import { defineConfig } from "vitest/config";

// The contract (../assistant-contract) is consumed by relative path. dedupe
// resolves its `zod` import, and every `react` import, from THIS package's
// node_modules, so exactly one copy of each loads and the contract directory
// needs no install of its own.
export default defineConfig({
  resolve: {
    dedupe: ["react", "react-dom", "zod"],
  },
  esbuild: {
    jsx: "automatic",
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./test/setup.ts"],
    include: ["test/**/*.test.{ts,tsx}"],
    testTimeout: 15_000,
  },
});
