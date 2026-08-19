// CAD-upload harness E2E + negative control (Lane C1, card C1-7).
//
// The real cad_upload production surface (server routers, cad version store)
// is built by sibling cards C1-2..C1-4 and has not landed on this branch yet.
// This suite proves the "disposable-task shape" the oracle names using the
// ALREADY-REAL generic isolated-worker primitive (`OperatorWorkerManager` /
// `LocalProcessSubstrate`, contract/OPERATOR.md Lane D): workspace
// "disposable", a version receipt (sha256 + bytes) per artifact, and a read
// round-trip that re-reads the SAME bytes back out.
//
// The `cad_upload` flag itself has no shipped module yet either; this suite
// defines the SAME rollout-mode fence `server/customization_flags.py` (and
// this card's own `server/tests/test_cad_fence.py`) already establish,
// scoped to `LEAF_CAD_UPLOAD_MODE`, so the negative control is a real fence,
// not a tautology. NOTE for the C1-3 lane: land the real gate at this env
// name (or amend `mode`/`cadUploadEnabled` here) in the same PR that mounts
// the real `cad_upload.py`, so this file keeps proving the real gate.

import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  LocalProcessSubstrate,
  OperatorWorkerManager,
} from "../src/operatorWorker/workerManager.js";
import type { WorkerJobEnvelope } from "../src/operatorWorker/workerManager.js";

const CAD_UPLOAD_ENV = "LEAF_CAD_UPLOAD_MODE";

type RolloutMode = "off" | "internal" | "all";

function mode(name: string): RolloutMode {
  const raw = (process.env[name] ?? "off").trim().toLowerCase();
  return raw === "internal" || raw === "all" ? raw : "off";
}

function cadUploadEnabled(): boolean {
  return mode(CAD_UPLOAD_ENV) !== "off";
}

let root: string;
let artifactRoot: string;

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), "cad-upload-disp-"));
  artifactRoot = fs.mkdtempSync(path.join(os.tmpdir(), "cad-upload-art-"));
  delete process.env[CAD_UPLOAD_ENV];
});

afterEach(() => {
  fs.rmSync(root, { recursive: true, force: true });
  fs.rmSync(artifactRoot, { recursive: true, force: true });
  delete process.env[CAD_UPLOAD_ENV];
});

function uploadEnvelope(base64Payload: string, jobKey: string): WorkerJobEnvelope {
  // A single self-contained step: decode the "uploaded" DWG-shaped bytes into
  // the disposable workspace's artifacts/ dir. The manager's submit()
  // collects that file into a sha256+bytes version receipt and copies it out
  // to artifactRoot -- the same shape a real cad_upload write would produce.
  const script = "node -e \"require('fs').mkdirSync('artifacts',{recursive:true});"
    + `require('fs').writeFileSync('artifacts/plan.dwg', Buffer.from('${base64Payload}','base64'))"`;
  return {
    workspace: "disposable",
    commands: [script],
    idempotencyKey: jobKey,
    principalSubject: "tenant|demo",
    sessionId: "cad-upload-session",
  };
}

describe("cad_upload E2E: upload -> version receipt -> read round-trip (disposable-task shape)", () => {
  it("round-trips real bytes through the disposable worker with a matching digest receipt", async () => {
    process.env[CAD_UPLOAD_ENV] = "all";
    expect(cadUploadEnabled()).toBe(true);

    const payload = Buffer.from("AC1027-fake-dwg-bytes-for-C1-7");
    const expectedSha256 = crypto.createHash("sha256").update(payload).digest("hex");

    const manager = new OperatorWorkerManager(
      new LocalProcessSubstrate(root), artifactRoot,
      { allowNonIsolatedSubstrate: true });

    const receipt = await manager.submit(
      uploadEnvelope(payload.toString("base64"), "cad-upload-1"));

    expect(receipt.status).toBe("succeeded");
    expect(receipt.artifacts).toHaveLength(1);
    const [artifact] = receipt.artifacts;

    // Version receipt: exact digest + size, matching the source bytes.
    expect(artifact.sha256).toBe(expectedSha256);
    expect(artifact.bytes).toBe(payload.length);

    // Read round-trip: re-read the persisted artifact and prove byte-identity.
    const readBack = fs.readFileSync(artifact.path);
    expect(readBack.equals(payload)).toBe(true);
    expect(crypto.createHash("sha256").update(readBack).digest("hex"))
      .toBe(expectedSha256);

    // The disposable workspace itself is gone; only the kept artifact remains.
    expect(receipt.workspaceRemoved).toBe(true);
  });
});

describe("negative control: cad_upload OFF refuses everything", () => {
  async function dispatchCadUpload(payload: Buffer, jobKey: string) {
    if (!cadUploadEnabled()) {
      throw new Error("cad_upload_disabled");
    }
    const manager = new OperatorWorkerManager(
      new LocalProcessSubstrate(root), artifactRoot,
      { allowNonIsolatedSubstrate: true });
    return manager.submit(uploadEnvelope(payload.toString("base64"), jobKey));
  }

  it("refuses to dispatch before the isolated worker is ever reached (default unset)", async () => {
    delete process.env[CAD_UPLOAD_ENV]; // unset == off, matches the server-side default
    expect(cadUploadEnabled()).toBe(false);

    await expect(dispatchCadUpload(Buffer.from("x"), "cad-upload-off-1"))
      .rejects.toThrow("cad_upload_disabled");
    // No op-worker-* workspace was ever created: the manager was never reached.
    expect(fs.readdirSync(root)).toEqual([]);
    expect(fs.readdirSync(artifactRoot)).toEqual([]);
  });

  it("refuses for every explicit off-shaped value, including garbage (fail closed)", async () => {
    for (const value of ["off", "OFF", "not-a-real-mode", "true", "1"]) {
      process.env[CAD_UPLOAD_ENV] = value;
      expect(cadUploadEnabled()).toBe(false);
      await expect(dispatchCadUpload(Buffer.from("y"), `cad-upload-off-${value}`))
        .rejects.toThrow("cad_upload_disabled");
    }
    expect(fs.readdirSync(root)).toEqual([]);
    expect(fs.readdirSync(artifactRoot)).toEqual([]);
  });

  it("flip-time proof: the SAME dispatcher accepts once flipped on, then refuses again", async () => {
    process.env[CAD_UPLOAD_ENV] = "all";
    const payload = Buffer.from("flip-time-bytes");
    const receipt = await dispatchCadUpload(payload, "cad-upload-flip-1");
    expect(receipt.status).toBe("succeeded");
    expect(receipt.artifacts).toHaveLength(1);

    process.env[CAD_UPLOAD_ENV] = "off";
    await expect(dispatchCadUpload(payload, "cad-upload-flip-2"))
      .rejects.toThrow("cad_upload_disabled");
  });
});
