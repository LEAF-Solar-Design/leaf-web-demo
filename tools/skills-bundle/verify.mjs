import path from "node:path";
import { fileURLToPath } from "node:url";
import { bundleDigest, fail, MAX_SKILLS, readJsonFile, sha256, validateBundleStructure } from "./common.mjs";

export async function verify(bundlePath) {
  const root = path.resolve(bundlePath);
  const structure = await validateBundleStructure(root);
  if (structure.names.length > MAX_SKILLS) {
    fail(`bundle exceeds ${MAX_SKILLS} skills`);
  }
  const manifest = await readJsonFile(path.join(root, "manifest.json"), "manifest.json");
  // The buffer validateBundleStructure inspected, not a fresh read of the same
  // path: re-reading is what lets a racing writer show us two different files.
  const plugin = JSON.parse(structure.contents.get(".claude-plugin/plugin.json").toString("utf8"));
  if (!manifest || manifest.version !== 1 || (manifest.tier !== "tenant-safe" && manifest.tier !== "operator")) {
    fail("manifest.json has an invalid version or tier");
  }
  if (!plugin || plugin.leafTier !== manifest.tier || typeof plugin.name !== "string" || typeof plugin.version !== "string") {
    fail("plugin.json leafTier does not match manifest tier");
  }
  if (!manifest.files || typeof manifest.files !== "object" || Array.isArray(manifest.files)) {
    fail("manifest.json files must be an object");
  }
  const declaredFiles = Object.keys(manifest.files).sort();
  if (declaredFiles.length !== structure.files.length || declaredFiles.some((file, index) => file !== structure.files[index])) {
    fail("manifest.json file list does not match bundle contents");
  }
  for (const relativePath of structure.files) {
    const expected = manifest.files[relativePath];
    if (typeof expected !== "string" || !/^[a-f0-9]{64}$/.test(expected)) fail(`invalid SHA-256 for ${relativePath}`);
    const inspected = structure.contents.get(relativePath);
    if (!inspected) fail(`no inspected bytes for ${relativePath}`);
    if (sha256(inspected) !== expected) fail(`hash mismatch for ${relativePath}`);
  }
  if (!manifest.skills || typeof manifest.skills !== "object" || Array.isArray(manifest.skills)) {
    fail("manifest.json skills must be an object");
  }
  // The descriptions are checked against the ones read from the INSPECTED bytes,
  // not merely for well-formedness. This is the whole point of recording them:
  // the manifest is what the loader mounts, so a manifest saying something the
  // SKILL.md does not say is exactly the drift this gate exists to catch.
  const declaredSkills = Object.keys(manifest.skills).sort();
  const actualSkills = Object.keys(structure.skills).sort();
  if (declaredSkills.length !== actualSkills.length || declaredSkills.some((name, index) => name !== actualSkills[index])) {
    fail("manifest.json skill list does not match bundle contents");
  }
  for (const name of actualSkills) {
    if (manifest.skills[name] !== structure.skills[name]) {
      fail(`manifest.json description for ${name} does not match its SKILL.md`);
    }
  }
  if (typeof manifest.bundleDigest !== "string" || !/^[a-f0-9]{64}$/.test(manifest.bundleDigest) || manifest.bundleDigest !== bundleDigest(manifest.files, manifest.skills)) {
    fail("bundle digest mismatch");
  }
  return { tier: manifest.tier, skills: structure.names };
}

async function main() {
  if (process.argv.length !== 3) {
    console.error("NOT-READY: usage: node verify.mjs <bundle-dir>");
    process.exitCode = 1;
    return;
  }
  try {
    const result = await verify(process.argv[2]);
    console.log(`READY: ${result.tier} bundle verified with ${result.skills.length} skills`);
  } catch (error) {
    console.error(`NOT-READY: ${error.message}`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
