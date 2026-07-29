import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { bundleDigest, fail, MAX_MANIFEST_BYTES, MAX_SKILLS, readCuration, sha256, validateBundleStructure } from "./common.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_SOURCE = "C:/Users/ehaug/.claude/skills";

function usage() {
  return "usage: node build.mjs [--source <dir>] [--curation <file>] --tier <tenant-safe|operator> --out <dir>";
}

function parseArgs(args) {
  const options = { source: DEFAULT_SOURCE, curation: path.join(SCRIPT_DIR, "curation.json") };
  for (let index = 0; index < args.length; index += 1) {
    const key = args[index];
    if (!['--source', '--curation', '--tier', '--out'].includes(key) || index + 1 >= args.length) fail(usage());
    options[key.slice(2)] = args[index + 1];
    index += 1;
  }
  if (!options.out || (options.tier !== "tenant-safe" && options.tier !== "operator")) fail(usage());
  return options;
}

export async function build(options) {
  const sourceRoot = path.resolve(options.source);
  const curationPath = path.resolve(options.curation);
  const outputPath = path.resolve(options.out);
  const skills = await readCuration(curationPath, sourceRoot);
  const selected = skills.filter((skill) => skill.tier === options.tier);
  if (selected.length === 0) fail(`no curated skills for tier ${options.tier}`);
  if (selected.length > MAX_SKILLS) {
    fail(`curated ${options.tier} bundle exceeds ${MAX_SKILLS} skills`);
  }
  try {
    await fs.mkdir(outputPath, { recursive: false });
  } catch (error) {
    fail(`output directory must not already exist: ${outputPath} (${error.code ?? error.message})`);
  }

  try {
    await fs.mkdir(path.join(outputPath, ".claude-plugin"));
    await fs.mkdir(path.join(outputPath, "skills"));
    const plugin = { name: `leaf-curated-skills-${options.tier}`, version: "1.0.0", leafTier: options.tier };
    const pluginContent = `${JSON.stringify(plugin, null, 2)}\n`;
    if (Buffer.byteLength(pluginContent) > MAX_MANIFEST_BYTES) {
      fail(`plugin.json exceeds ${MAX_MANIFEST_BYTES} bytes`);
    }
    await fs.writeFile(path.join(outputPath, ".claude-plugin", "plugin.json"), pluginContent, { flag: "wx" });
    for (const skill of selected) {
      const targetDir = path.join(outputPath, "skills", skill.name);
      await fs.mkdir(targetDir);
      await fs.writeFile(path.join(targetDir, "SKILL.md"), skill.content, { flag: "wx" });
    }

    // Write a private placeholder so the same strict tree validator used by
    // verification can check the completed layout before hashes are calculated.
    const manifestPath = path.join(outputPath, "manifest.json");
    await fs.writeFile(manifestPath, "{}\n", { flag: "wx" });
    const structure = await validateBundleStructure(outputPath);
    const files = {};
    for (const relativePath of structure.files) {
      files[relativePath] = sha256(await fs.readFile(path.join(outputPath, ...relativePath.split("/"))));
    }
    const manifest = { version: 1, tier: options.tier, files, bundleDigest: bundleDigest(files) };
    await fs.writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
    console.log(`READY: built ${options.tier} bundle with ${selected.length} skills at ${outputPath}`);
  } catch (error) {
    await fs.rm(outputPath, { recursive: true, force: true });
    throw error;
  }
}

async function main() {
  try {
    await build(parseArgs(process.argv.slice(2)));
  } catch (error) {
    console.error(`NOT-READY: ${error.message}`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
