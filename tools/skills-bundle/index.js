// Node resolves a directory test target through index.js on Windows.
// Keep the required `node --test tools/skills-bundle/` invocation portable.
import("./pipeline.test.mjs").catch((error) => {
  process.nextTick(() => { throw error; });
});
