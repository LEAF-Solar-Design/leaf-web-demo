/**
 * TS validator for contract/surface-config.v1.schema.json (leaf.surface-config.v1).
 *
 * A faithful hand-port, same rationale as registry/toolPackageSchema.ts: no ajv
 * dependency in this package, and the oracle stays in one small file rather than
 * pulling in a schema-validation runtime for one artifact class.
 *
 * Slot names mirror web/src/site/productSurfaces.js (leaf-web-demo, a separate
 * lane this repo does not read); three slots (chrome, conversations, builds)
 * carry their own closed field set because the slice-7 spec text names their
 * fields explicitly, the rest are typed as objects only. See the schema file's
 * $comment blocks for the exact scoping.
 */

const SURFACE_IDS = new Set(["browser", "cad", "solar", "ios", "sheets"]);

const SLOT_NAMES = new Set([
  "chrome", "toolbar", "rails", "commandLine", "authoring", "versions",
  "conversations", "builds", "contextMenu", "groundMaterial",
]);

const CHROME_FIELDS = new Set(["tab"]);
const CONVERSATIONS_FIELDS = new Set(["scope"]);
const BUILDS_FIELDS = new Set(["routes"]);

const GENERIC_OBJECT_SLOTS = new Set([
  "toolbar", "rails", "commandLine", "versions", "contextMenu", "groundMaterial",
]);

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function fieldsSubsetOf(value: Record<string, unknown>, allowed: Set<string>): boolean {
  return Object.keys(value).every((key) => allowed.has(key));
}

function validChrome(value: unknown): boolean {
  if (!isPlainObject(value) || !fieldsSubsetOf(value, CHROME_FIELDS)) return false;
  const tab = value.tab;
  return tab === undefined || typeof tab === "string";
}

function validConversations(value: unknown): boolean {
  if (!isPlainObject(value) || !fieldsSubsetOf(value, CONVERSATIONS_FIELDS)) return false;
  const scope = value.scope;
  return scope === undefined || typeof scope === "string";
}

function validBuilds(value: unknown): boolean {
  if (!isPlainObject(value) || !fieldsSubsetOf(value, BUILDS_FIELDS)) return false;
  const routes = value.routes;
  return routes === undefined ||
    (Array.isArray(routes) && routes.every((route) => typeof route === "string"));
}

/** One fixed-depth dispatch over the closed SLOT_NAMES set — no recursion, so
 * validation depth is bounded by construction, not by input. */
function validSlot(name: string, value: unknown): boolean {
  if (name === "chrome") return validChrome(value);
  if (name === "conversations") return validConversations(value);
  if (name === "builds") return validBuilds(value);
  if (name === "authoring") return typeof value === "boolean";
  if (GENERIC_OBJECT_SLOTS.has(name)) return isPlainObject(value);
  return false; // unreachable: every SLOT_NAMES member is handled above
}

/**
 * Validate an unknown value against contract/surface-config.v1.schema.json.
 * Returns diagnostics; empty === valid. ANY violation names the offending path
 * but the caller rejects the WHOLE overlay on any single diagnostic — matching
 * mushy_fold.surface_config's fold-side discipline of never a partial merge.
 */
export function validateSurfaceConfig(value: unknown): string[] {
  if (!isPlainObject(value)) return ["surface config must be a JSON object"];
  const errs: string[] = [];
  for (const [surfaceId, slots] of Object.entries(value)) {
    if (!SURFACE_IDS.has(surfaceId)) {
      errs.push(`unknown surface id ${JSON.stringify(surfaceId)}`);
      continue;
    }
    if (!isPlainObject(slots)) {
      errs.push(`${surfaceId}: must be a JSON object`);
      continue;
    }
    for (const [slotName, slotValue] of Object.entries(slots)) {
      if (!SLOT_NAMES.has(slotName)) {
        errs.push(`${surfaceId}.${slotName}: unknown slot`);
      } else if (!validSlot(slotName, slotValue)) {
        errs.push(`${surfaceId}.${slotName}: wrong type or unknown field`);
      }
    }
  }
  return errs;
}
