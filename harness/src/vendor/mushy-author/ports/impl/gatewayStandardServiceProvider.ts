import { createHash, randomUUID } from "node:crypto";
import { lookup } from "node:dns/promises";
import { mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { isIP } from "node:net";
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

function isPng(bytes: Buffer): boolean {
  return bytes.length >= 8
    && bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
}

export interface GatewayClient {
  connect(): Promise<void>;
  callTool(name: string, args: Record<string, unknown>): Promise<GatewayToolResult>;
  close(): Promise<void>;
}

export interface IssuedInspectionTarget {
  inspection_target_id: string;
  expires_at: string;
}

interface StoredInspectionTarget {
  inspection_target_id: string;
  identity: StandardServiceIdentity;
  url: string;
  origin: string;
  resolved_addresses: string[];
  expires_at_ms: number;
}

interface ArtifactRecord {
  identity: StandardServiceIdentity;
  path: string;
  media_type: string;
  digest: string;
  size: number;
  expires_at_ms: number;
}

export interface GatewayStandardServiceProviderOptions {
  endpoint: string;
  /** Local-development compatibility only. Production should use authorization. */
  authToken?: string;
  /** Resolve a separate, short-lived gateway bearer for this exact identity. */
  authorization?: (
    identity: StandardServiceIdentity,
  ) => Promise<{ bearer_token: string; expires_at: string }>;
  artifactDir: string;
  screenshotRoot?: string;
  catalog?: StandardServiceCatalog;
  clientFactory?: (context: {
    identity: StandardServiceIdentity;
    authToken?: string;
  }) => Promise<GatewayClient>;
  now?: () => number;
  visualBackend?: "browser-session" | "playwright-single-operator";
  sensitiveValues?: string[];
  visualTargetPolicy?: {
    environment: "production" | "staging" | "development";
    allowedOrigins: string[];
    /** Loopback is accepted only in an explicit development policy. */
    allowLoopback?: boolean;
    resolveHostname?: (hostname: string) => Promise<string[]>;
  };
}

const METADATA_HOSTS = new Set([
  "metadata",
  "metadata.google.internal",
  "metadata.azure.internal",
  "instance-data",
]);

function ipv4Parts(address: string): number[] | null {
  const parts = address.split(".").map(Number);
  return parts.length === 4 && parts.every((part) => Number.isInteger(part) && part >= 0 && part <= 255)
    ? parts
    : null;
}

function addressKind(address: string): "public" | "loopback" | "blocked" {
  const ipv4 = ipv4Parts(address);
  if (ipv4) {
    const [a, b] = ipv4 as [number, number, number, number];
    if (a === 127) return "loopback";
    if (
      a === 0
      || a === 10
      || (a === 100 && b >= 64 && b <= 127)
      || (a === 169 && b === 254)
      || (a === 172 && b >= 16 && b <= 31)
      || (a === 192 && b === 0)
      || (a === 192 && b === 168)
      || (a === 198 && (b === 18 || b === 19))
      || (a === 198 && b === 51)
      || (a === 203 && b === 0)
      || a >= 224
    ) return "blocked";
    return "public";
  }
  const normalized = address.toLowerCase();
  if (normalized === "::1") return "loopback";
  if (normalized === "::" || normalized.startsWith("fc") || normalized.startsWith("fd")) return "blocked";
  if (/^fe[89ab]/.test(normalized) || normalized.startsWith("ff")) return "blocked";
  if (normalized.startsWith("2001:db8:")) return "blocked";
  if (normalized.startsWith("::ffff:")) return addressKind(normalized.slice("::ffff:".length));
  return isIP(normalized) === 6 ? "public" : "blocked";
}

function sameIdentity(a: StandardServiceIdentity, b: StandardServiceIdentity): boolean {
  return a.tenant_id === b.tenant_id
    && a.subject_id === b.subject_id
    && a.session_id === b.session_id
    && a.authority_turn_id === b.authority_turn_id
    && a.subscription_mount_id === b.subscription_mount_id
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
 * never returned to the model. The current raw browser-session and Playwright
 * backends cannot prove address pinning or pre-dispatch redirect and
 * subresource enforcement, so visual execution is development-only.
 */
export class GatewayStandardServiceProvider implements StandardServiceProvider {
  private readonly endpoint: string;
  private readonly authToken?: string;
  private readonly authorization?: GatewayStandardServiceProviderOptions["authorization"];
  private readonly artifactDir: string;
  private readonly screenshotRoot: string;
  private readonly serviceCatalog: StandardServiceCatalog;
  private readonly clientFactory?: GatewayStandardServiceProviderOptions["clientFactory"];
  private readonly now: () => number;
  readonly visualBackend: "browser-session" | "playwright-single-operator";
  private readonly sensitiveValues: string[];
  private readonly targetPolicy?: GatewayStandardServiceProviderOptions["visualTargetPolicy"];
  private readonly allowedTargetOrigins: Set<string>;
  private readonly resolveHostname: (hostname: string) => Promise<string[]>;
  private readonly targets = new Map<string, StoredInspectionTarget>();
  private readonly artifacts = new Map<string, ArtifactRecord>();

  private redactVisualValue(value: unknown, targetUrl: string): unknown {
    const encoded = JSON.stringify(value);
    if (encoded === undefined) return null;
    const redacted = this.sensitiveValues.reduce(
      (text, secret) => text.replaceAll(secret, "<redacted>"),
      encoded.replaceAll(targetUrl, "<issued-target>"),
    );
    return JSON.parse(redacted);
  }

  constructor(options: GatewayStandardServiceProviderOptions) {
    if (options.authToken && options.authorization) {
      throw new Error("standard_service_gateway_authorization_ambiguous");
    }
    const endpoint = new URL(options.endpoint);
    if (endpoint.protocol !== "http:" && endpoint.protocol !== "https:") {
      throw new Error("standard_service_gateway_endpoint_invalid");
    }
    this.endpoint = endpoint.toString();
    this.authToken = options.authToken;
    this.authorization = options.authorization;
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
    this.targetPolicy = options.visualTargetPolicy;
    if (this.targetPolicy?.allowLoopback && this.targetPolicy.environment !== "development") {
      throw new Error("standard_service_visual_loopback_requires_development_policy");
    }
    this.allowedTargetOrigins = new Set((this.targetPolicy?.allowedOrigins ?? []).map((origin) => {
      const parsed = new URL(origin);
      if (parsed.origin !== origin || parsed.username || parsed.password) {
        throw new Error("standard_service_visual_target_origin_invalid");
      }
      return parsed.origin;
    }));
    this.resolveHostname = this.targetPolicy?.resolveHostname ?? (async (hostname) =>
      (await lookup(hostname, { all: true, verbatim: true })).map((entry) => entry.address));
    mkdirSync(this.artifactDir, { recursive: true });
  }

  async issueInspectionTarget(
    identity: StandardServiceIdentity,
    url: string,
    ttlMs = 15 * 60_000,
  ): Promise<IssuedInspectionTarget> {
    if (!Number.isSafeInteger(ttlMs) || ttlMs < 1_000 || ttlMs > 15 * 60_000) {
      throw new Error("standard_service_visual_target_ttl_invalid");
    }
    if (
      !identity.tenant_id
      || !identity.subject_id
      || !identity.session_id
      || !identity.authority_turn_id
      || !identity.subscription_mount_id
      || !identity.runner_profile_id
    ) {
      throw new Error("standard_service_identity_incomplete");
    }
    const validated = await this.validateTargetUrl(url);
    const target = {
      inspection_target_id: randomUUID(),
      identity: structuredClone(identity),
      url: validated.url,
      origin: validated.origin,
      resolved_addresses: validated.addresses,
      expires_at_ms: this.now() + ttlMs,
    };
    this.targets.set(target.inspection_target_id, target);
    return {
      inspection_target_id: target.inspection_target_id,
      expires_at: new Date(target.expires_at_ms).toISOString(),
    };
  }

  readArtifact(identity: StandardServiceIdentity, artifactId: string): { bytes: Buffer; media_type: string; digest: string } {
    const artifact = this.artifacts.get(artifactId);
    if (!artifact || artifact.expires_at_ms <= this.now()) throw new Error("standard_service_artifact_missing_or_expired");
    if (!sameIdentity(identity, artifact.identity)) throw new Error("standard_service_artifact_identity_mismatch");
    const bytes = readFileSync(artifact.path);
    const digest = `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
    if (bytes.length !== artifact.size || !isPng(bytes) || digest !== artifact.digest) {
      throw new Error("standard_service_artifact_integrity_mismatch");
    }
    return { bytes, media_type: artifact.media_type, digest: artifact.digest };
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
    if (this.targetPolicy?.environment !== "development") {
      // These gateway tools own their network stack. Passing a validated
      // hostname cannot pin its address or stop an unsafe redirect or
      // subresource before dispatch. Public environments must use the tenant
      // broker once its visual adapter enforces those controls at the socket.
      throw new Error("standard_service_visual_backend_not_network_safe");
    }
    await this.validateTargetUrl(target.url, target.resolved_addresses);

    if (this.visualBackend === "playwright-single-operator") {
      return this.visualInspectWithPlaywright(identity, target, viewport);
    }

    const client = await this.openClient(identity);
    let browserSessionId: string | null = null;
    try {
      const started = parseToolResult(await client.callTool("browser_session__cw_walk_start", {
        url: target.url,
        viewport,
      }));
      gatewayError(started);
      await this.validateFinalNavigation(target, started.page_url ?? started.url);
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
          dom: this.redactVisualValue(dom, target.url),
          console_errors: this.redactVisualValue(consoleState.console_errors ?? [], target.url),
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
    target: StoredInspectionTarget,
    viewport: "desktop" | "mobile",
  ): Promise<StandardServiceVisualResult> {
    const client = await this.openClient(identity);
    try {
      const size = viewport === "mobile" ? { width: 390, height: 844 } : { width: 1280, height: 900 };
      await this.requireToolSuccess(client, "playwright__browser_resize", size);
      const navigation = await this.requireToolSuccess(client, "playwright__browser_navigate", { url: target.url });
      await this.validateFinalNavigation(target, this.navigationUrl(navigation));
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
    const visualNetworkSafe = this.targetPolicy?.environment === "development";
    let gateway: "ready" | "unavailable" = "unavailable";
    try {
      const client = await this.openClient(identity);
      gateway = "ready";
      await client.close().catch(() => {});
    } catch {
      gateway = "unavailable";
    }
    const visual = !visualNetworkSafe
      ? "unavailable"
      : gateway === "ready" && hasTarget
        ? "ready"
        : gateway === "ready"
          ? "degraded"
          : "unavailable";
    return {
      state: visual,
      catalog_digest: standardServiceCatalogDigest(this.serviceCatalog),
      services: { visual },
      ...(!visualNetworkSafe
        ? { message: "raw visual backend is unavailable outside explicit development" }
        : !hasTarget
          ? { message: "no active inspection target for this session" }
          : {}),
    };
  }

  private async openClient(identity: StandardServiceIdentity): Promise<GatewayClient> {
    let authToken = this.authToken;
    if (this.authorization) {
      const authorization = await this.authorization(structuredClone(identity));
      const expiresAt = Date.parse(authorization?.expires_at ?? "");
      if (!authorization?.bearer_token || !Number.isFinite(expiresAt) || expiresAt <= this.now()) {
        throw new Error("standard_service_gateway_authorization_invalid_or_expired");
      }
      authToken = authorization.bearer_token;
    }
    if (this.clientFactory) {
      const client = await this.clientFactory({ identity: structuredClone(identity), ...(authToken ? { authToken } : {}) });
      try {
        await client.connect();
        return client;
      } catch (error) {
        await client.close().catch(() => {});
        throw error;
      }
    }
    const transport = new StreamableHTTPClientTransport(new URL(this.endpoint), {
      requestInit: authToken ? { headers: { Authorization: `Bearer ${authToken}` } } : {},
    });
    const client = new Client({ name: "mushy-standard-services", version: "1.0.0" });
    const adapter: GatewayClient = {
      connect: () => client.connect(transport),
      callTool: (name, args) => client.callTool({ name, arguments: args }) as Promise<GatewayToolResult>,
      close: () => client.close(),
    };
    try {
      await adapter.connect();
      return adapter;
    } catch (error) {
      await adapter.close().catch(() => {});
      throw error;
    }
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
    if (mediaType.toLowerCase() !== "image/png" || !isPng(bytes)) {
      throw new Error("standard_service_screenshot_image_invalid");
    }
    const digest = `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
    const artifactId = randomUUID();
    const targetPath = join(this.artifactDir, `${artifactId}.png`);
    mkdirSync(dirname(targetPath), { recursive: true });
    writeFileSync(targetPath, bytes, { mode: 0o600 });
    this.artifacts.set(artifactId, {
      identity: structuredClone(identity),
      path: targetPath,
      media_type: "image/png",
      digest,
      size: bytes.length,
      expires_at_ms: expiresAtMs,
    });
    return artifactId;
  }

  private async validateTargetUrl(
    value: string,
    expectedAddresses?: string[],
  ): Promise<{ url: string; origin: string; addresses: string[] }> {
    if (!this.targetPolicy || this.allowedTargetOrigins.size === 0) {
      throw new Error("standard_service_visual_target_policy_required");
    }
    let parsed: URL;
    try { parsed = new URL(value); }
    catch { throw new Error("standard_service_visual_target_invalid"); }
    if (
      (parsed.protocol !== "http:" && parsed.protocol !== "https:")
      || parsed.username
      || parsed.password
      || !this.allowedTargetOrigins.has(parsed.origin)
    ) {
      throw new Error("standard_service_visual_target_not_trusted");
    }
    if (this.targetPolicy.environment === "production" && parsed.protocol !== "https:") {
      throw new Error("standard_service_visual_target_not_trusted");
    }
    const hostname = parsed.hostname.toLowerCase().replace(/\.$/, "").replace(/^\[|\]$/g, "");
    if (
      !hostname
      || hostname === "localhost"
      || hostname.endsWith(".localhost")
      || hostname.endsWith(".local")
      || hostname.endsWith(".internal")
      || METADATA_HOSTS.has(hostname)
    ) {
      if (!(hostname === "localhost"
        && this.targetPolicy.environment === "development"
        && this.targetPolicy.allowLoopback)) {
        throw new Error("standard_service_visual_target_host_blocked");
      }
    }
    let addresses: string[];
    if (isIP(hostname)) {
      addresses = [hostname];
    } else {
      try { addresses = [...new Set(await this.resolveHostname(hostname))].sort(); }
      catch { throw new Error("standard_service_visual_target_dns_failed"); }
      if (!addresses.length) throw new Error("standard_service_visual_target_dns_failed");
    }
    if (expectedAddresses && JSON.stringify(addresses) !== JSON.stringify([...expectedAddresses].sort())) {
      throw new Error("standard_service_visual_target_dns_rebinding");
    }
    for (const address of addresses) {
      const kind = addressKind(address);
      if (kind === "loopback") {
        if (!(this.targetPolicy.environment === "development" && this.targetPolicy.allowLoopback)) {
          throw new Error("standard_service_visual_target_host_blocked");
        }
      } else if (kind !== "public") {
        throw new Error("standard_service_visual_target_host_blocked");
      }
    }
    return { url: parsed.toString(), origin: parsed.origin, addresses };
  }

  private async validateFinalNavigation(target: StoredInspectionTarget, value: unknown): Promise<void> {
    if (typeof value !== "string" || !value) {
      throw new Error("standard_service_visual_final_url_missing");
    }
    const final = await this.validateTargetUrl(value, target.resolved_addresses);
    if (final.origin !== target.origin) {
      throw new Error("standard_service_visual_redirect_not_trusted");
    }
  }

  private navigationUrl(result: GatewayToolResult): string | undefined {
    const text = result.content
      ?.filter((item) => item.type === "text" && typeof item.text === "string")
      .map((item) => item.text)
      .join("\n") ?? "";
    return text.match(/^\s*-?\s*Page URL:\s*(\S+)\s*$/im)?.[1];
  }
}
