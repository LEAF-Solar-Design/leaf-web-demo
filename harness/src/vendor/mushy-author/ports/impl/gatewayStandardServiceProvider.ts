import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { dirname, join, resolve, sep } from "node:path";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import type {
  StandardServiceCall,
  StandardServiceCatalog,
  StandardServiceIdentity,
  StandardServiceProvider,
  StandardServiceRequestResult,
  StandardServiceResult,
  StandardServiceStatus,
  StandardServiceVisualResult,
} from "./standardServices.js";
import {
  STANDARD_SERVICE_CATALOG_V1,
  standardServiceCatalogDigest,
} from "./standardServices.js";

type GatewayToolResult = {
  content?: Array<{ type?: string; text?: string; data?: string; mimeType?: string }>;
  structuredContent?: Record<string, unknown>;
  isError?: boolean;
};

interface GatewayClient {
  connect(): Promise<void>;
  callTool(name: string, args: Record<string, unknown>): Promise<GatewayToolResult>;
  close(): Promise<void>;
}

export interface IssuedInspectionTarget {
  inspection_target_id: string;
  identity: StandardServiceIdentity;
  url: string;
  expires_at_ms: number;
}

interface ArtifactRecord {
  identity: StandardServiceIdentity;
  path: string;
  media_type: string;
  digest: string;
  expires_at_ms: number;
}

export interface GatewayStandardServiceProviderOptions {
  endpoint: string;
  authToken?: string;
  artifactDir: string;
  screenshotRoot?: string;
  catalog?: StandardServiceCatalog;
  clientFactory?: () => Promise<GatewayClient>;
  now?: () => number;
  visualBackend?: "browser-session" | "playwright-single-operator";
  sensitiveValues?: string[];
}

function sameIdentity(a: StandardServiceIdentity, b: StandardServiceIdentity): boolean {
  return a.tenant_id === b.tenant_id
    && a.subject_id === b.subject_id
    && a.session_id === b.session_id
    && a.runner_profile_id === b.runner_profile_id;
}

function parseToolResult(value: GatewayToolResult): Record<string, unknown> {
  if (value.isError) throw new Error("standard_service_gateway_tool_failed");
  if (value.structuredContent && typeof value.structuredContent === "object") return value.structuredContent;
  const text = value.content?.find((item) => item.type === "text" && typeof item.text === "string")?.text;
  if (!text) throw new Error("standard_service_gateway_result_missing");
  try {
    const parsed = JSON.parse(text);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("not object");
    return parsed as Record<string, unknown>;
  } catch {
    throw new Error("standard_service_gateway_result_invalid");
  }
}

function gatewayError(value: Record<string, unknown>): void {
  if (typeof value.error === "string" && value.error) {
    throw new Error("standard_service_gateway_tool_failed");
  }
}

/**
 * Trusted aggregate-gateway adapter for the standard-service facade.
 *
 * This is separate from the tenant MCP proxy. Its endpoint is operator
 * configuration, not tenant input, so private AWS networking may be valid.
 * Every visual URL still comes from an issued target stored outside model
 * context. Gateway screenshot paths are copied into scoped artifacts and are
 * never returned to the model.
 */
export class GatewayStandardServiceProvider implements StandardServiceProvider {
  private readonly endpoint: string;
  private readonly authToken?: string;
  private readonly artifactDir: string;
  private readonly screenshotRoot: string;
  private readonly serviceCatalog: StandardServiceCatalog;
  private readonly clientFactory?: GatewayStandardServiceProviderOptions["clientFactory"];
  private readonly now: () => number;
  readonly visualBackend: "browser-session" | "playwright-single-operator";
  private readonly sensitiveValues: string[];
  private readonly targets = new Map<string, IssuedInspectionTarget>();
  private readonly artifacts = new Map<string, ArtifactRecord>();

  constructor(options: GatewayStandardServiceProviderOptions) {
    const endpoint = new URL(options.endpoint);
    if (endpoint.protocol !== "http:" && endpoint.protocol !== "https:") {
      throw new Error("standard_service_gateway_endpoint_invalid");
    }
    this.endpoint = endpoint.toString();
    this.authToken = options.authToken;
    this.artifactDir = resolve(options.artifactDir);
    this.screenshotRoot = resolve(options.screenshotRoot ?? join(
      process.env.USERPROFILE ?? process.env.HOME ?? "",
      ".cadwalk-engine",
      "walk-screenshots",
    ));
    this.serviceCatalog = options.catalog ?? STANDARD_SERVICE_CATALOG_V1;
    this.clientFactory = options.clientFactory;
    this.now = options.now ?? Date.now;
    this.visualBackend = options.visualBackend ?? "browser-session";
    this.sensitiveValues = (options.sensitiveValues ?? []).filter((value) => value.length >= 4);
    mkdirSync(this.artifactDir, { recursive: true });
  }

  issueInspectionTarget(
    identity: StandardServiceIdentity,
    url: string,
    ttlMs = 15 * 60_000,
  ): IssuedInspectionTarget {
    const parsed = new URL(url);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      throw new Error("standard_service_visual_target_invalid");
    }
    if (!identity.tenant_id || !identity.subject_id || !identity.session_id) {
      throw new Error("standard_service_identity_incomplete");
    }
    const target = {
      inspection_target_id: randomUUID(),
      identity: structuredClone(identity),
      url: parsed.toString(),
      expires_at_ms: this.now() + Math.max(1_000, ttlMs),
    };
    this.targets.set(target.inspection_target_id, target);
    return { ...target, identity: structuredClone(target.identity) };
  }

  readArtifact(identity: StandardServiceIdentity, artifactId: string): { bytes: Buffer; media_type: string; digest: string } {
    const artifact = this.artifacts.get(artifactId);
    if (!artifact || artifact.expires_at_ms <= this.now()) throw new Error("standard_service_artifact_missing_or_expired");
    if (!sameIdentity(identity, artifact.identity)) throw new Error("standard_service_artifact_identity_mismatch");
    return { bytes: readFileSync(artifact.path), media_type: artifact.media_type, digest: artifact.digest };
  }

  async catalog(_identity: StandardServiceIdentity): Promise<StandardServiceCatalog> {
    return structuredClone(this.serviceCatalog);
  }

  async read(_identity: StandardServiceIdentity, _call: StandardServiceCall): Promise<StandardServiceResult> {
    throw new Error("standard_service_read_not_implemented");
  }

  async request(_identity: StandardServiceIdentity, _call: StandardServiceCall): Promise<StandardServiceRequestResult> {
    throw new Error("standard_service_mutation_not_implemented");
  }

  async confirm(_identity: StandardServiceIdentity, _approvalId: string): Promise<StandardServiceResult> {
    throw new Error("standard_service_mutation_not_implemented");
  }

  async visualInspect(
    identity: StandardServiceIdentity,
    inspectionTargetId: string,
    viewport: "desktop" | "mobile",
  ): Promise<StandardServiceVisualResult> {
    const target = this.targets.get(inspectionTargetId);
    if (!target || target.expires_at_ms <= this.now()) throw new Error("standard_service_visual_target_missing_or_expired");
    if (!sameIdentity(identity, target.identity)) throw new Error("standard_service_visual_target_identity_mismatch");

    if (this.visualBackend === "playwright-single-operator") {
      return this.visualInspectWithPlaywright(identity, target, viewport);
    }

    const client = await this.openClient();
    let browserSessionId: string | null = null;
    try {
      const started = parseToolResult(await client.callTool("browser_session__cw_walk_start", {
        url: target.url,
        viewport,
      }));
      gatewayError(started);
      browserSessionId = typeof started.session_id === "string" ? started.session_id : null;
      if (!browserSessionId) throw new Error("standard_service_visual_session_missing");

      const dom = parseToolResult(await client.callTool("browser_session__cw_walk_dom", {
        session_id: browserSessionId,
      }));
      gatewayError(dom);
      const consoleState = parseToolResult(await client.callTool("browser_session__cw_walk_press_key", {
        session_id: browserSessionId,
        key: "Shift",
        wait_after_ms: 0,
      }));
      gatewayError(consoleState);
      const screenshot = parseToolResult(await client.callTool("browser_session__cw_walk_screenshot", {
        session_id: browserSessionId,
      }));
      gatewayError(screenshot);
      const artifactId = this.importScreenshot(identity, screenshot.path, target.expires_at_ms);
      const artifact = this.artifacts.get(artifactId)!;

      return {
        inspection_target_id: inspectionTargetId,
        content: JSON.stringify({
          viewport,
          page_title: started.page_title ?? "",
          dom,
          console_errors: consoleState.console_errors ?? [],
          screenshot: {
            artifact_id: artifactId,
            media_type: artifact.media_type,
            digest: artifact.digest,
            expires_at: new Date(artifact.expires_at_ms).toISOString(),
          },
        }),
        artifact_ids: [artifactId],
        image_artifact_id: artifactId,
        media_type: artifact.media_type,
      };
    } finally {
      if (browserSessionId) {
        await client.callTool("browser_session__cw_walk_finish", { session_id: browserSessionId }).catch(() => {});
      }
      await client.close().catch(() => {});
    }
  }

  private async visualInspectWithPlaywright(
    identity: StandardServiceIdentity,
    target: IssuedInspectionTarget,
    viewport: "desktop" | "mobile",
  ): Promise<StandardServiceVisualResult> {
    const client = await this.openClient();
    try {
      const size = viewport === "mobile" ? { width: 390, height: 844 } : { width: 1280, height: 900 };
      await this.requireToolSuccess(client, "playwright__browser_resize", size);
      const navigation = await this.requireToolSuccess(client, "playwright__browser_navigate", { url: target.url });
      const screenshot = await this.requireToolSuccess(client, "playwright__browser_take_screenshot", {
        type: "png",
        fullPage: false,
      });
      const image = screenshot.content?.find((item) => item.type === "image" && typeof item.data === "string");
      if (!image?.data) throw new Error("standard_service_screenshot_image_missing");
      const bytes = Buffer.from(image.data, "base64");
      const artifactId = this.storeArtifact(identity, bytes, image.mimeType ?? "image/png", target.expires_at_ms);
      const artifact = this.artifacts.get(artifactId)!;
      const navigationText = navigation.content
        ?.filter((item) => item.type === "text" && typeof item.text === "string")
        .map((item) => item.text)
        .join("\n") ?? "";
      const safeNavigationText = navigationText
        .replaceAll(target.url, "<issued-target>")
        .replace(/^\s*- Page URL:.*$/gm, "- Page URL: <issued-target>");
      const redactedNavigationText = this.sensitiveValues.reduce(
        (text, value) => text.replaceAll(value, "<redacted>"),
        safeNavigationText,
      );
      return {
        inspection_target_id: target.inspection_target_id,
        content: JSON.stringify({
          viewport,
          accessibility_snapshot_and_console: redactedNavigationText.slice(0, 20_000),
          screenshot: {
            artifact_id: artifactId,
            media_type: artifact.media_type,
            digest: artifact.digest,
            expires_at: new Date(artifact.expires_at_ms).toISOString(),
          },
          isolation: "single-operator",
        }),
        artifact_ids: [artifactId],
        image_artifact_id: artifactId,
        media_type: artifact.media_type,
      };
    } finally {
      await client.callTool("playwright__browser_close", {}).catch(() => {});
      await client.close().catch(() => {});
    }
  }

  async status(identity: StandardServiceIdentity): Promise<StandardServiceStatus> {
    const hasTarget = [...this.targets.values()].some((target) =>
      target.expires_at_ms > this.now() && sameIdentity(identity, target.identity));
    let gateway: "ready" | "unavailable" = "unavailable";
    try {
      const client = await this.openClient();
      gateway = "ready";
      await client.close().catch(() => {});
    } catch {
      gateway = "unavailable";
    }
    return {
      state: gateway === "ready" && hasTarget ? "ready" : gateway === "ready" ? "degraded" : "unavailable",
      catalog_digest: standardServiceCatalogDigest(this.serviceCatalog),
      services: { visual: gateway === "ready" && hasTarget ? "ready" : gateway === "ready" ? "degraded" : "unavailable" },
      ...(!hasTarget ? { message: "no active inspection target for this session" } : {}),
    };
  }

  private async openClient(): Promise<GatewayClient> {
    if (this.clientFactory) {
      const client = await this.clientFactory();
      await client.connect();
      return client;
    }
    const transport = new StreamableHTTPClientTransport(new URL(this.endpoint), {
      requestInit: this.authToken ? { headers: { Authorization: `Bearer ${this.authToken}` } } : {},
    });
    const client = new Client({ name: "mushy-standard-services", version: "1.0.0" });
    const adapter: GatewayClient = {
      connect: () => client.connect(transport),
      callTool: (name, args) => client.callTool({ name, arguments: args }) as Promise<GatewayToolResult>,
      close: () => client.close(),
    };
    await adapter.connect();
    return adapter;
  }

  private async requireToolSuccess(
    client: GatewayClient,
    name: string,
    args: Record<string, unknown>,
  ): Promise<GatewayToolResult> {
    const result = await client.callTool(name, args);
    if (result.isError) throw new Error(`standard_service_gateway_tool_failed:${name}`);
    return result;
  }

  private importScreenshot(
    identity: StandardServiceIdentity,
    pathValue: unknown,
    expiresAtMs: number,
  ): string {
    if (typeof pathValue !== "string" || !pathValue) throw new Error("standard_service_screenshot_path_missing");
    let realPath: string;
    let realRoot: string;
    try {
      realPath = realpathSync(pathValue);
      realRoot = realpathSync(this.screenshotRoot);
    } catch {
      throw new Error("standard_service_screenshot_path_invalid");
    }
    const rootPrefix = realRoot.endsWith(sep) ? realRoot : `${realRoot}${sep}`;
    if (!realPath.startsWith(rootPrefix)) throw new Error("standard_service_screenshot_path_outside_root");
    return this.storeArtifact(identity, readFileSync(realPath), "image/png", expiresAtMs);
  }

  private storeArtifact(
    identity: StandardServiceIdentity,
    bytes: Buffer,
    mediaType: string,
    expiresAtMs: number,
  ): string {
    const digest = `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
    const artifactId = randomUUID();
    const targetPath = join(this.artifactDir, `${artifactId}.png`);
    mkdirSync(dirname(targetPath), { recursive: true });
    writeFileSync(targetPath, bytes, { mode: 0o600 });
    this.artifacts.set(artifactId, {
      identity: structuredClone(identity),
      path: targetPath,
      media_type: mediaType,
      digest,
      expires_at_ms: expiresAtMs,
    });
    return artifactId;
  }
}
