import { z } from "zod";
import branchApiAllowlist from "../registry/api-allowlist.json" with { type: "json" };
import type { AgentGrant, AuthorTelemetry, ReadonlyFsTenantRepoTool } from "../ports/index.js";
import {
  buildScrubbedEnv, resolveAuthorEffort, resolveAuthorModel,
  resolveAuthorTimeoutMs, type AuthorSdkQuery,
} from "../ports/impl/agentSdkRunner.js";
import { grantSecrets, redactSecrets } from "../redact.js";
import { carriedFailureCategory, safeErrorMessage, withFailureCategory } from "./captureGuards.js";
import type { BranchSubmissionResult } from "./tools/submitBranchToolProposal.js";
import type { BranchCompileDiagnostic, BranchCompileSummary } from "./tools/branchCompileCheck.js";
import { BranchPackageError } from "../registry/branchPackageSchema.js";

export function branchTargetPrompt(revision?: { packagePath: string; version: string }): string {
  // The explicit import also makes tsc copy the artifact into the harness dist.
  const allowlist = branchApiAllowlist;
  const prompt = `You author cad-tool-package v1 C# scripts for Branch's ScriptHost.
Submit one package using submitBranchToolProposal with exactly these keys:
name, version, description, params, returns, capabilities, hosts, timeout_ms,
idempotent, main_csx, lib. lib maps <file>.csx to its complete source text.
The harness sets entry=main.csx, provenance.author=agent and review.status=unreviewed.
Use a 3..64 character lower-case kebab name, X.Y.Z version without leading zeros,
a 1..500 character description, object params JSON Schema and returns JSON Schema.
hosts={min_release:2018..2030,min_host_api:integer>=1}; timeout_ms=1..600000;
idempotent is boolean. Every number anywhere in the manifest must be a whole number (V15): defaults, minimum, maximum, multipleOf, enum and examples inside params and returns too. A fractional default such as 2.5 is refused. Leave it out of params and write it in main_csx: Ctx.Args.Value<double?>("x") ?? 2.5.
Maximum 64 total files
(tool.json and main.csx count) and 4 MiB total, with tool.json at most 256 KiB.
Only drawing.read and drawing.write capabilities are allowed. No files.* or net.http.
No #r directives, BOM or bidi controls. #load must be a whole line naming an
existing lib/<file>.csx. No other paths, subdirectories, Python files or registry edits.
Inspect with fs_tenant_repo; it is read-only. Fix rule diagnostics before resubmitting.
Every submission is compiled with Branch's ScriptHost compiler against AutoCAD 2025
reference assemblies. Fix compile and API-allowlist errors reported as V18 diagnostics and resubmit.
Write C# operators literally (&&, <, >); never HTML entities.
After a successful submission, end the turn at once with the single word Done: no summary, no more tool calls. Success still does not prove CAD execution. Do not call an APS Python test.

ToolGlobals exposes Ctx (IToolContext): Db (Database), Doc (Document), Ed (Editor,
identity only), Args (JObject), Cancel (CancellationToken), Log (IToolLog), and Service.
Use Ctx.Db.TransactionManager.StartTransaction(), open objects through that
transaction, and commit. Honor Ctx.Cancel. Return a JSON-serializable result.
Use drawing.write for modifications. Disk and network APIs are unavailable.

Allowlist traps. These are refused by the scan even where the entity type is allowed (an unlisted
enum or return type refuses the expression):
- Fractional numbers in params or returns (V15): keep the schema type number without a default and put the fractional default in main_csx.
- ColorMethod.ByAciIndex does not exist: use Color.FromColorIndex(ColorMethod.ByAci, n) or Color.FromRgb(r, g, b).
- Hatch patterns and loops (HatchPatternType, HatchLoopTypes): for fills use Solid (3 or 4 corners,
  Z order) or a Polyline with ConstantWidth.
- LineWeight on entities or layers: use Polyline ConstantWidth or vertex widths, and Color.
- TextHorizontalMode, TextVerticalMode, AttachmentPoint (DBText HorizontalMode, VerticalMode,
  Justify; MText Attachment): set DBText.Position, or MText Location and Width, and compute the offset.
- ObjectId.ObjectClass, GetRXClass, RXClass, GetType, typeof: open the object and test with is or as.
- Ctx.Ed.WriteMessage, Regen and every other Editor member: use Ctx.Log.Info and the returned result.
- JTokenType and JToken.ToObject: use Ctx.Args.Value<T?>("name") ?? fallback, as JArray, or an explicit cast.
- Database.Insunits, CurrentSpaceId, Dimstyle, SymbolUtilityServices: take units and sizes as arguments
  and open BlockTableRecord.ModelSpace through the BlockTable.
- Viewport, Table, Leader, MLeader, Wipeout, dynamic block properties, Clone, Explode, GetOffsetCurves,
  IntersectWith, Polyline.GetSegmentType: build new Line, Polyline, Solid, MText, dimension or
  BlockReference entities.
- Autodesk.AutoCAD.Runtime.Exception, System.Random, Regex: catch System.Exception, use deterministic
  arithmetic, use string methods.

Worked example panel-gap-check (drawing.read):
main_csx:
#load "lib/geometry.csx"
using System.Collections.Generic;
using Autodesk.AutoCAD.DatabaseServices;
using Newtonsoft.Json.Linq;
int minGap = Ctx.Args.Value<int?>("min_gap_in") ?? 18;
var offenders = new List<string>();
using (var tr = Ctx.Db.TransactionManager.StartTransaction()) {
  var bt = (BlockTable)tr.GetObject(Ctx.Db.BlockTableId, OpenMode.ForRead);
  var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
  foreach (ObjectId id in ms) {
    Ctx.Cancel.ThrowIfCancellationRequested();
    var ent = tr.GetObject(id, OpenMode.ForRead) as Entity;
    if (ent != null && ent.Layer == "PANELS" && EdgeGapInches(ent.GeometricExtents) < minGap)
      offenders.Add(id.Handle.ToString());
  }
  tr.Commit();
}
return new { offenders };
lib/geometry.csx:
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Newtonsoft.Json.Linq;
double EdgeGapInches(Extents3d ext) { return ext.MinPoint.DistanceTo(Point3d.Origin); }
This helper measures distance to the origin; for actual roof gaps implement the
requested boundary calculation rather than presenting this example as one.

Worked example label-writer (drawing.read and drawing.write; idempotent=false):
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Newtonsoft.Json.Linq;
using (var tr = Ctx.Db.TransactionManager.StartTransaction()) {
  var bt = (BlockTable)tr.GetObject(Ctx.Db.BlockTableId, OpenMode.ForRead);
  var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForWrite);
  var text = new DBText();
  text.Position = Point3d.Origin;
  text.Height = 12;
  text.TextString = Ctx.Args.Value<string>("label") ?? "PV";
  ms.AppendEntity(text);
  tr.AddNewlyCreatedDBObject(text, true);
  tr.Commit();
}
return new { added = 1 };

Worked example area-marks (drawing.read and drawing.write; per cell outline Polyline plus Solid, MText number, total, AlignedDimension, BlockReference; idempotent=false). Keep the outline when you fill: a fill alone does not draw the shape.
main_csx:
using System.Collections.Generic;
using Autodesk.AutoCAD.Colors;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Newtonsoft.Json.Linq;
double limit = Ctx.Args.Value<double?>("area_limit") ?? 5000.0;
int marked = 0, blocks = 0;
double left = double.MaxValue, top = double.MinValue, big = 0;
using (var tr = Ctx.Db.TransactionManager.StartTransaction()) {
  var layers = (LayerTable)tr.GetObject(Ctx.Db.LayerTableId, OpenMode.ForRead);
  if (!layers.Has("AREA-MARKS")) {
    layers.UpgradeOpen();
    var rec = new LayerTableRecord { Name = "AREA-MARKS", Color = Color.FromRgb(255, 200, 0) };
    layers.Add(rec);
    tr.AddNewlyCreatedDBObject(rec, true);
  }
  var bt = (BlockTable)tr.GetObject(Ctx.Db.BlockTableId, OpenMode.ForRead);
  var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForWrite);
  var ids = new List<ObjectId>();
  foreach (ObjectId id in ms) ids.Add(id);
  void Add(Entity e, Color c) {
    e.SetDatabaseDefaults(Ctx.Db);
    e.Layer = "AREA-MARKS";
    e.Color = c;
    ms.AppendEntity(e);
    tr.AddNewlyCreatedDBObject(e, true);
  }
  foreach (var id in ids) {
    Ctx.Cancel.ThrowIfCancellationRequested();
    var ent = tr.GetObject(id, OpenMode.ForRead) as Entity;
    if (ent == null) continue;
    if (ent.Layer == "AREA-MARKS") { ent.UpgradeOpen(); ent.Erase(); continue; }
    if (ent is BlockReference br) {
      blocks++;
      br.UpgradeOpen();
      br.Color = Color.FromRgb(0, 160, 255);
      continue;
    }
    var pl = ent as Polyline;
    if (pl == null || !pl.Closed || pl.NumberOfVertices < 3) continue;
    var ext = pl.GeometricExtents;
    Point3d lo = ext.MinPoint, hi = ext.MaxPoint;
    var mid = new Point3d((lo.X + hi.X) / 2, (lo.Y + hi.Y) / 2, 0);
    double size = System.Math.Max(System.Math.Min(hi.X - lo.X, hi.Y - lo.Y) / 10, 0.01);
    var color = pl.Area < limit ? Color.FromRgb(230, 40, 40) : Color.FromRgb(40, 180, 80);
    // Outline first: a fill alone does not draw the shape.
    var outline = new Polyline();
    for (int i = 0; i < pl.NumberOfVertices; i++)
      outline.AddVertexAt(i, pl.GetPoint2dAt(i), pl.GetBulgeAt(i), size / 10, size / 10);
    outline.Closed = true;
    Add(outline, Color.FromRgb(20, 20, 20));
    // Solid corner order is Z-shaped: p0, p1, p3, p2. Fill only true four-corner cells.
    if (pl.NumberOfVertices == 4)
      Add(new Solid(pl.GetPoint3dAt(0), pl.GetPoint3dAt(1), pl.GetPoint3dAt(3), pl.GetPoint3dAt(2)), color);
    Add(new MText { Location = new Point3d(mid.X - size, mid.Y + size, 0), TextHeight = size * 2,
      Contents = (marked + 1).ToString() }, Color.FromRgb(20, 20, 20));
    Add(new AlignedDimension { XLine1Point = lo, XLine2Point = new Point3d(hi.X, lo.Y, 0),
      DimLinePoint = new Point3d(mid.X, lo.Y - 2 * size, 0), Dimtxt = size, Dimasz = size }, color);
    left = System.Math.Min(left, lo.X);
    top = System.Math.Max(top, hi.Y);
    big = System.Math.Max(big, size * 4);
    marked++;
  }
  if (marked > 0)
    Add(new MText { Location = new Point3d(left, top + 2 * big, 0), TextHeight = big,
      Contents = "TOTAL " + marked }, Color.FromRgb(255, 200, 0));
  tr.Commit();
}
return new { marked, blocks };

Pinned API allowlist (types, members, namespaces, denials and capability gates):
${JSON.stringify(allowlist, null, 2)}`;
  if (!revision) return prompt;
  return `${prompt}\n\nThis is a revision of ${revision.packagePath} (current version ${revision.version}). Read its tool.json, main.csx and lib files with fs_tenant_repo, edit them, keep the package name, and submit the complete package with a strictly greater version.`;
}

export interface BranchCompileFeedback { summary: BranchCompileSummary; diagnostics: BranchCompileDiagnostic[] }
export class BranchCompileRefusal extends BranchPackageError {
  constructor(path: string, message: string, readonly compile: BranchCompileFeedback) {
    super([{ rule: "V18", path, message }]);
  }
}

export class BranchAuthorFailure extends Error {
  constructor(message: string, readonly reason: Readonly<Record<string, string | number>>, readonly compile?: BranchCompileFeedback) {
    super(message);
    this.name = "BranchAuthorFailure";
  }
}

export interface BranchAuthorInput {
  description: string;
  systemPrompt: string;
  repoDir: string;
  grant: AgentGrant;
  fsTenantRepo: ReadonlyFsTenantRepoTool;
  submitBranchToolProposal(proposal: unknown): BranchSubmissionResult | Promise<BranchSubmissionResult>;
}
export interface BranchAuthorResult { preview: string; telemetry?: AuthorTelemetry & { usage_incomplete?: true } }
export type BranchAuthorRunner = (input: BranchAuthorInput) => Promise<BranchAuthorResult>;

type ToolReply = { content: Array<{ type: "text"; text: string }>; isError?: boolean };
export interface BranchAuthorSdk {
  tool(name: string, description: string, shape: unknown, handler: (args: Record<string, unknown>) => Promise<ToolReply>): unknown;
  createSdkMcpServer(options: Record<string, unknown>): unknown;
  query(input: { prompt: string; options: Record<string, unknown> }): AuthorSdkQuery;
}

/** Branch-only SDK adapter. Studio continues through its existing AgentRunner. */
export const BRANCH_ACCEPTED_GRACE_MS = 5_000;

export async function runBranchTargetSession(
  input: BranchAuthorInput,
  options: { sdkImport?: () => Promise<BranchAuthorSdk>; acceptedGraceMs?: number; timeoutMs?: number } = {},
): Promise<BranchAuthorResult> {
  try {
    // Keep the SDK's heavy declaration graph out of the harness build.
    const sdk = options.sdkImport ? await options.sdkImport()
      : await import(["@anthropic-ai", "claude-agent-sdk"].join("/")) as BranchAuthorSdk;
    const reply = (value: unknown, isError = false): ToolReply => ({
      content: [{ type: "text", text: typeof value === "string" ? value : JSON.stringify(value) }],
      ...(isError ? { isError: true } : {}),
    });
    let submitted = false;
    let sessionClosed = false;
    let graceTimer: ReturnType<typeof setTimeout> | undefined;
    let finishGrace!: () => void;
    const grace = new Promise<void>(resolve => { finishGrace = resolve; });
    let lastRefusal: string | undefined;
    let lastRefusalRule: string | undefined;
    let lastCompile: BranchCompileFeedback | undefined;
    let submitCalls = 0;
    const tools = [
      sdk.tool("fs_tenant_repo", "Read-only repository inspection.", {
        action: z.enum(["read", "list", "exists"]), path: z.string(),
      }, async args => {
        try {
          const path = String(args.path);
          if (args.action === "read") return reply(input.fsTenantRepo.readFile(path));
          if (args.action === "list") return reply(input.fsTenantRepo.listDir(path || "."));
          if (args.action === "exists") return reply(input.fsTenantRepo.exists(path));
          return reply("unknown read action", true);
        } catch (error) { return reply(safeErrorMessage(error), true); }
      }),
      // A raw shape is wrapped in a stripping object by the SDK. Preserve
      // unknown keys so the submission validator can refuse them with V2.
      sdk.tool("submitBranchToolProposal", "Validate V1..V18 feedback and publish exactly one branch package; returns rule diagnostics on refusal.", z.looseObject({
        name: z.string(), version: z.string(), description: z.string(),
        params: z.record(z.string(), z.unknown()), returns: z.record(z.string(), z.unknown()),
        capabilities: z.array(z.string()),
        hosts: z.looseObject({ min_release: z.number(), min_host_api: z.number() }),
        timeout_ms: z.number(), idempotent: z.boolean(), main_csx: z.string(),
        lib: z.record(z.string(), z.string()),
      }), async proposal => {
        submitCalls++;
        try {
          if (submitted) return reply("V16: only one successful package submission is allowed", true);
          if (sessionClosed) return reply("V16: submission is closed", true);
          const result = await input.submitBranchToolProposal(proposal);
          if (sessionClosed) return reply("V16: submission is closed", true);
          submitted = true;
          graceTimer = setTimeout(finishGrace, options.acceptedGraceMs ?? BRANCH_ACCEPTED_GRACE_MS);
          return reply({ status: "VALID", manifest: result.manifest, files: result.files, package_digest: result.package_digest,
            ...(result.source_decoded !== undefined ? { source_decoded: result.source_decoded } : {}) });
        } catch (error) {
          lastRefusal = safeErrorMessage(error);
          lastCompile = error instanceof BranchCompileRefusal ? error.compile : undefined;
          const rule = error instanceof BranchPackageError
            ? error.diagnostics[0]?.rule : /^(V-name|V\d{1,2}):/.exec(lastRefusal)?.[1];
          lastRefusalRule = rule && /^(V-name|V[1-9][0-9]?)$/.test(rule) ? rule : undefined;
          return reply(lastRefusal, true);
        }
      }),
    ];
    const server = sdk.createSdkMcpServer({ name: "branch_author", version: "1.0.0", tools });
    const allowed = ["mcp__branch_author__fs_tenant_repo", "mcp__branch_author__submitBranchToolProposal"];
    const abort = new AbortController();
    const query = sdk.query({
      prompt: `${input.systemPrompt}\n\nAuthor this requested branch package:\n${input.description}`,
      options: {
        cwd: input.repoDir, env: buildScrubbedEnv(input.grant, process.env),
        model: (input.grant.kind === "api_key" ? input.grant.model : undefined) ?? resolveAuthorModel(undefined),
        effort: resolveAuthorEffort(undefined),
        maxTurns: 24, settingSources: [], permissionMode: "default", tools: [],
        abortController: abort, mcpServers: { branch_author: server }, allowedTools: allowed,
        hooks: { PostToolUse: [{ matcher: "mcp__branch_author__submitBranchToolProposal",
          hooks: [async () => submitted ? { continue: false, stopReason: "branch package accepted" } : {}] }] },
        // The two allowed branch_author tools are approved by allowedTools and never reach this callback; deny every other tool.
        canUseTool: async () => ({ behavior: "deny", message: "Only branch package submission and read-only repository inspection are permitted." }),
      },
    });
    let result: Record<string, unknown> | null = null;
    let turns = 0;
    let inputTokens = 0;
    let outputTokens = 0;
    let costTokens = 0;
    const models = new Set<string>();
    const messages = new Map<string | symbol, { input: number; output: number }>();
    let finishResult!: () => void;
    const resultSeen = new Promise<void>(resolve => { finishResult = resolve; });
    const timeoutMs = options.timeoutMs ?? resolveAuthorTimeoutMs(undefined);
    let wallTimer: ReturnType<typeof setTimeout> | undefined;
    const deadline = new Promise<never>((_resolve, reject) => {
      wallTimer = setTimeout(() => {
        sessionClosed = true;
        reject(new Error(`Agent SDK author session exceeded the ${Math.round(timeoutMs / 1000)}s wall-clock limit.`));
      }, timeoutMs);
    });
    const drain = async () => {
      for await (const raw of query) {
        if (sessionClosed) break;
        const msg = raw as Record<string, unknown>;
        if (msg.type === "rate_limit_event") {
          const info = msg.rate_limit_info as { status?: string; resetsAt?: number } | undefined;
          if (info?.status !== "rejected") continue;
          const reset = info.resetsAt;
          const seconds = typeof reset === "number" ? Math.max(0, Math.round((reset > 1e12 ? reset / 1000 : reset) - Date.now() / 1000)) : null;
          throw withFailureCategory(new Error(`Agent SDK rate limited${seconds === null ? " (retry horizon unknown)" : ` (retry after ~${seconds}s)`}`), "rate_limited");
        }
        if (msg.type === "assistant") {
          turns += 1;
          const message = (msg.message ?? {}) as Record<string, unknown>;
          const usage = (message.usage ?? {}) as Record<string, number>;
          const id = typeof message.id === "string" ? message.id : typeof msg.id === "string" ? msg.id : Symbol();
          const previous = messages.get(id);
          messages.set(id, { input: Math.max(previous?.input ?? 0, usage.input_tokens ?? 0),
            output: Math.max(previous?.output ?? 0, usage.output_tokens ?? 0) });
          inputTokens += usage.input_tokens ?? 0;
          outputTokens += usage.output_tokens ?? 0;
          costTokens += (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0) + (usage.cache_creation_input_tokens ?? 0);
          if (typeof message.model === "string") models.add(message.model);
          if (["authentication_failed", "oauth_org_not_allowed", "billing_error"].includes(String(msg.error))) {
            throw withFailureCategory(new Error(`Agent SDK auth failure: ${msg.error}`), msg.error === "billing_error" ? "quota_exhausted" : "auth_failure");
          }
          if (msg.error === "rate_limit") throw withFailureCategory(new Error("Agent SDK rate limited (retry horizon unknown)"), "rate_limited");
          if (turns > 24 || costTokens > 500_000) throw withFailureCategory(new Error("Agent SDK branch author spend cap exceeded"), "quota_exhausted");
        }
        if (msg.type === "result") { result = msg; finishResult(); break; }
      }
    };
    try {
      // Race the drain itself: an SDK iterator or its return() may never settle.
      // Promise.race attaches rejection handlers even after another arm wins.
      await Promise.race([drain(), deadline, grace, resultSeen]);
    } catch (error) {
      if (!submitted) throw error;
    } finally {
      sessionClosed = true;
      clearTimeout(wallTimer);
      clearTimeout(graceTimer);
      try { query.close(); } catch { /* Cleanup cannot replace an accepted package. */ }
      abort.abort();
    }
    const final = result as Record<string, unknown> | null;
    if (!submitted && (!final || final.is_error || final.subtype !== "success")) {
      throw new BranchAuthorFailure("branch author session did not finish successfully", {
        code: "branch_author_unsuccessful", turns,
      });
    }
    if (!submitted && lastRefusalRule) {
      throw new BranchAuthorFailure(`branch author session ended without a validated package: last submission refused by ${lastRefusalRule}`, {
        code: "branch_author_package_refused", rule: lastRefusalRule, turns, submit_calls: submitCalls,
      }, lastCompile);
    }
    if (!submitted) {
      throw new BranchAuthorFailure("branch author session ended without submitting a package", {
        code: "branch_author_no_submission", turns, submit_calls: submitCalls,
      });
    }
    const usage = (final?.usage ?? {}) as Record<string, number>;
    const fallback = [...messages.values()].reduce((sum, message) => ({
      input: sum.input + message.input, output: sum.output + message.output,
    }), { input: 0, output: 0 });
    return {
      preview: "Branch C# package authored; awaiting server validation and review.",
      telemetry: {
        turns: final ? typeof final.num_turns === "number" ? final.num_turns : turns : messages.size,
        input_tokens: usage.input_tokens ?? (final ? inputTokens : fallback.input),
        output_tokens: usage.output_tokens ?? (final ? outputTokens : fallback.output),
        models: [...models],
        ...(typeof final?.total_cost_usd === "number" ? { total_cost_usd: final.total_cost_usd } : {}),
        ...(!final ? { usage_incomplete: true as const } : {}),
      },
    };
  } catch (error) {
    if (error instanceof BranchAuthorFailure) throw error;
    const message = redactSecrets(safeErrorMessage(error), grantSecrets(input.grant));
    const wrapped = new Error(message);
    wrapped.stack = `${wrapped.name}: ${message}`;
    const category = carriedFailureCategory(error);
    throw category ? withFailureCategory(wrapped, category) : wrapped;
  }
}
