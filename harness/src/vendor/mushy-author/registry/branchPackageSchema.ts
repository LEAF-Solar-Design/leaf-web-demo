/** In-loop cad-tool-package v1 feedback. The server's S1 check is authoritative. */
import { snapshotUntrusted } from "../agent/captureGuards.js";

export interface BranchToolProposal {
  name: string;
  version: string;
  description: string;
  params: Record<string, unknown>;
  returns: Record<string, unknown>;
  capabilities: Array<"drawing.read" | "drawing.write">;
  hosts: { min_release: number; min_host_api: number };
  timeout_ms: number;
  idempotent: boolean;
  main_csx: string;
  lib: Record<string, string>;
}

export interface BranchManifest extends Omit<BranchToolProposal, "main_csx" | "lib"> {
  entry: "main.csx";
  provenance: { author: "agent"; session: string; created: string; modified: string };
  review: { status: "unreviewed" };
}

export interface BranchDiagnostic { rule: string; path: string; message: string }
export interface PreparedBranchPackage {
  manifest: BranchManifest;
  /** Package-relative paths and exact UTF-8 file content. */
  files: Record<string, string>;
}

export class BranchPackageError extends Error {
  constructor(readonly diagnostics: BranchDiagnostic[]) {
    super(diagnostics.map(d => `${d.rule}: ${d.path}: ${d.message}`).join("; "));
    this.name = "BranchPackageError";
  }
}

const NAME = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?![\s\S])/;
const VERSION = /^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?![\s\S])/;
const CSX = /^[a-z0-9][a-z0-9-]*\.csx(?![\s\S])/;
const CONTROLS = /[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069\ufeff]/u;
const FIELDS = ["name", "version", "description", "params", "returns", "capabilities",
  "hosts", "timeout_ms", "idempotent", "main_csx", "lib"];
const RULES: Record<string, string> = {
  name: "V3", version: "V4", description: "V5", entry: "V6", params: "V7",
  returns: "V7", capabilities: "V8", family: "V9", hosts: "V10",
  timeout_ms: "V11", idempotent: "V12", provenance: "V13", review: "V14",
};
const object = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const integer = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v);

/** Check common 2020-12 meta-schema constraints without interpreting user schemas. */
function schemaValid(v: unknown, depth = 0): boolean {
  if (depth > 100) return false;
  if (typeof v === "boolean") return true;
  if (!object(v)) return false;
  const types = ["null", "boolean", "object", "array", "number", "integer", "string"];
  if (v.type !== undefined && !(typeof v.type === "string" ? types.includes(v.type)
    : Array.isArray(v.type) && v.type.length > 0 && new Set(v.type).size === v.type.length
      && v.type.every(t => typeof t === "string" && types.includes(t)))) return false;
  for (const k of ["properties", "patternProperties", "$defs", "definitions", "dependentSchemas"]) {
    if (v[k] !== undefined && (!object(v[k]) || !Object.values(v[k]).every(s => schemaValid(s, depth + 1)))) return false;
  }
  for (const k of ["items", "additionalProperties", "unevaluatedProperties", "unevaluatedItems", "contains", "propertyNames", "not", "if", "then", "else"]) {
    if (v[k] !== undefined && !schemaValid(v[k], depth + 1)) return false;
  }
  for (const k of ["allOf", "anyOf", "oneOf", "prefixItems"]) {
    if (v[k] !== undefined && (!Array.isArray(v[k]) || v[k].length === 0 || !v[k].every(s => schemaValid(s, depth + 1)))) return false;
  }
  for (const k of ["required"]) {
    if (v[k] !== undefined && (!Array.isArray(v[k]) || !v[k].every(s => typeof s === "string") || new Set(v[k]).size !== v[k].length)) return false;
  }
  if (v.enum !== undefined && (!Array.isArray(v.enum) || v.enum.length === 0)) return false;
  for (const k of ["minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties", "minContains", "maxContains"]) {
    if (v[k] !== undefined && (!integer(v[k]) || v[k] < 0)) return false;
  }
  for (const k of ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"]) {
    if (v[k] !== undefined && (typeof v[k] !== "number" || !Number.isFinite(v[k]) || (k === "multipleOf" && v[k] <= 0))) return false;
  }
  for (const k of ["$id", "$ref", "$schema", "$anchor", "$dynamicRef", "$dynamicAnchor", "title", "description", "format", "pattern"]) {
    if (v[k] !== undefined && typeof v[k] !== "string") return false;
  }
  if (typeof v.pattern === "string") {
    try { new RegExp(v.pattern); } catch { return false; }
  }
  for (const k of ["uniqueItems", "readOnly", "writeOnly", "deprecated"]) {
    if (v[k] !== undefined && typeof v[k] !== "boolean") return false;
  }
  return true;
}

/** Validate the harness-generated manifest and layout against the V1..V17 rules. */
export function validateBranchPackage(
  manifest: unknown, files: Record<string, string>, packageName: string,
): BranchDiagnostic[] {
  const errors: BranchDiagnostic[] = [];
  const fail = (rule: string, path: string, message: string) => errors.push({ rule, path, message });
  if (!object(manifest)) return [{ rule: "V1", path: "tool.json", message: "manifest must be an object" }];
  const required = [...FIELDS.filter(k => k !== "main_csx" && k !== "lib"), "entry", "provenance", "review"];
  for (const key of Object.keys(manifest)) {
    if (!required.includes(key) && key !== "family") fail("V2", key, "unknown manifest key");
  }
  for (const key of required) if (!(key in manifest)) fail(RULES[key] ?? "V2", key, "required field");
  if (typeof manifest.name !== "string" || !NAME.test(manifest.name) || manifest.name.length < 3 || manifest.name.length > 64 || manifest.name !== packageName) fail("V3", "name", "invalid package name");
  if (typeof manifest.version !== "string" || !VERSION.test(manifest.version)) fail("V4", "version", "expected X.Y.Z without leading zeros");
  if (typeof manifest.description !== "string" || [...manifest.description].length < 1 || [...manifest.description].length > 500) fail("V5", "description", "expected 1..500 characters");
  if (manifest.entry !== "main.csx" || !("main.csx" in files)) fail("V6", "entry", "main.csx is required");
  if (!object(manifest.params) || manifest.params.type !== "object" || !schemaValid(manifest.params)) fail("V7", "params", "expected an object JSON Schema");
  if (!object(manifest.returns) || !schemaValid(manifest.returns)) fail("V7", "returns", "expected a JSON Schema object");
  if (!Array.isArray(manifest.capabilities) || manifest.capabilities.some(c => c !== "drawing.read" && c !== "drawing.write")) fail("V8", "capabilities", "only drawing.read and drawing.write are permitted");
  if (manifest.family !== undefined && (typeof manifest.family !== "string" || !NAME.test(manifest.family) || manifest.family.length < 3 || manifest.family.length > 64)) fail("V9", "family", "invalid family");
  const hosts = manifest.hosts;
  if (!object(hosts) || Object.keys(hosts).some(k => k !== "min_release" && k !== "min_host_api") || !integer(hosts.min_release) || hosts.min_release < 2018 || hosts.min_release > 2030 || !integer(hosts.min_host_api) || hosts.min_host_api < 1) fail("V10", "hosts", "invalid host requirements");
  if (!integer(manifest.timeout_ms) || manifest.timeout_ms < 1 || manifest.timeout_ms > 600000) fail("V11", "timeout_ms", "expected an integer in 1..600000");
  if (typeof manifest.idempotent !== "boolean") fail("V12", "idempotent", "expected a boolean");
  const p = manifest.provenance;
  const stamp = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z(?![\s\S])/;
  if (!object(p) || Object.keys(p).some(k => !["author", "session", "created", "modified"].includes(k)) || p.author !== "agent" || typeof p.session !== "string" || p.session.length < 1 || [...p.session].length > 200 || typeof p.created !== "string" || !stamp.test(p.created) || typeof p.modified !== "string" || !stamp.test(p.modified)) fail("V13", "provenance", "invalid harness provenance");
  if (!object(manifest.review) || Object.keys(manifest.review).length !== 1 || manifest.review.status !== "unreviewed") fail("V14", "review", "expected unreviewed");
  const walk = (v: unknown, field: string, depth = 0): void => {
    if (depth > 100) { fail("V1", field, "manifest nested too deeply"); return; }
    if (typeof v === "string" && CONTROLS.test(v)) fail(RULES[field] ?? "V2", field, "control character in manifest");
    if (typeof v === "number" && !integer(v)) fail("V15", field, "expected a safe integer");
    if (Array.isArray(v)) for (const item of v) walk(item, field, depth + 1);
    else if (object(v)) for (const [k, item] of Object.entries(v)) { walk(k, field, depth + 1); walk(item, field, depth + 1); }
  };
  for (const [k, v] of Object.entries(manifest)) { walk(k, k); walk(v, k); }
  if (Object.keys(files).length > 64) fail("V16", "", "more than 64 files");
  if (Object.values(files).reduce((n, s) => n + Buffer.byteLength(s), 0) > 4 * 1024 * 1024) fail("V16", "", "package exceeds 4 MiB");
  if (!("tool.json" in files) || Buffer.byteLength(files["tool.json"]) > 256 * 1024) fail("V1", "tool.json", "manifest missing or exceeds 256 KiB");
  for (const [path, source] of Object.entries(files)) {
    if (path !== "tool.json" && path !== "main.csx" && !(path.startsWith("lib/") && CSX.test(path.slice(4)))) fail("V16", path, "disallowed file");
    if (!path.endsWith(".csx")) continue;
    if (/[\ufeff\u202a-\u202e\u2066-\u2069]/u.test(source) || Buffer.from(source).toString("utf8") !== source) fail("V17", path, "invalid Unicode, BOM or bidirectional control");
    for (const line of source.split(/\r\n|[\r\n\u0085\u2028\u2029]/u)) {
      const directive = /^[ \t\f\v]*#[ \t]*(r|load)(?![A-Za-z0-9_])/.exec(line);
      if (!directive) continue;
      const load = /^[ \t]*#load[ \t]+"(lib\/[a-z0-9][a-z0-9-]*\.csx)"[ \t]*(?![\s\S])/.exec(line);
      if (directive[1] === "r" || !load || !Object.hasOwn(files, load[1])) fail("V17", path, "#r is forbidden; #load must name an existing lib/<file>.csx");
    }
  }
  return errors;
}

export function prepareBranchPackage(value: unknown, session: string, now = new Date()): PreparedBranchPackage {
  const proposal = snapshotUntrusted(value);
  if (!object(proposal)) throw new BranchPackageError([{ rule: "V1", path: "", message: "proposal must be a JSON object" }]);
  const errors: BranchDiagnostic[] = [];
  // Inspect original own keys too: the generic snapshot deliberately drops
  // function properties, and assigning a JSON __proto__ key can alter its copy.
  for (const key of Object.getOwnPropertyNames(value)) if (!FIELDS.includes(key)) errors.push({ rule: "V2", path: key, message: "unknown proposal key" });
  for (const key of FIELDS) if (!Object.hasOwn(proposal, key)) errors.push({ rule: RULES[key] ?? "V16", path: key, message: "required proposal field" });
  if (typeof proposal.main_csx !== "string") errors.push({ rule: "V6", path: "main_csx", message: "expected source text" });
  if (!object(proposal.lib) || Object.entries(proposal.lib).some(([k, v]) => !CSX.test(k) || typeof v !== "string")) errors.push({ rule: "V16", path: "lib", message: "expected a map of <file>.csx to source text" });
  if (errors.length) throw new BranchPackageError(errors);
  const { main_csx, lib, ...metadata } = proposal;
  const timestamp = now.toISOString();
  const manifest = {
    ...metadata, entry: "main.csx",
    provenance: { author: "agent", session, created: timestamp, modified: timestamp },
    review: { status: "unreviewed" },
  } as BranchManifest;
  const files: Record<string, string> = Object.create(null);
  files["tool.json"] = JSON.stringify(manifest, null, 2) + "\n";
  files["main.csx"] = main_csx as string;
  for (const [name, source] of Object.entries(lib as Record<string, string>)) files[`lib/${name}`] = source;
  const diagnostics = validateBranchPackage(manifest, files, manifest.name);
  if (diagnostics.length) throw new BranchPackageError(diagnostics);
  return { manifest, files };
}
