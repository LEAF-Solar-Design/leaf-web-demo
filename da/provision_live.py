"""da/provision_live.py — ONE-TIME live provisioning. ROOT runs this, not lanes.

Creates on APS Design Automation exactly what the client needs to submit WorkItems:
  1. the OSS bucket (PERSISTENT policy — backs the versioned per-tenant drawing
     store in da/store.py; objects do NOT expire). This provisions a FRESH bucket
     key (`leaf-web-store-*`); the old `leaf-web-demo-*` transient bucket is
     abandoned to expire on its own (OSS bucket policy is immutable at creation).
  2. the extract Activity (pure-LISP, no AppBundle) + a 'prod' alias.

It performs LIVE mutating calls, so it is deliberately NOT invoked by any lane and
NOT by the dry-run CLIs. Run it once:  python da/provision_live.py
Add --tools <registry.json> to also create a LeafTool_<engine_op> script Activity
per 'kind:script' tool that carries an inline LISP body under tool["engine_script"]
(Lane B supplies those scripts; this is a convenience for the demo's tools).

Safe to re-run: bucket 409 and activity/alias 409 are treated as 'already exists'.

Multi-tenant (ADDITIVE):
  python da/provision_live.py --dry-run [--tenant <id>] [--per-bucket]
      Print the exact bucket key(s), Activity ids, tenant key prefix, and example
      per-run object keys it WOULD create/use — with ZERO APS mutation and ZERO
      network call (no token, no nickname fetch). Safe to run anywhere.
  python da/provision_live.py --tenant <id>
      LIVE, idempotent: provision/verify per-tenant isolation. Default isolation
      is SHARED BUCKET + per-tenant key prefix (`t/<id>/...`), so the only live
      resources are the shared bucket + GLOBAL Activities (already 409-idempotent);
      the tenant is realized purely by key discipline (no per-tenant provisioning).
  python da/provision_live.py --tenant <id> --per-bucket
      LIVE: the reversible per-bucket alternative — create a per-tenant OSS bucket
      (da.tenant.tenant_bucket) instead of relying on the shared-bucket prefix.

The legacy no-arg path (shared bucket + extract Activity) is unchanged.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client  # noqa: E402
import tenant as tenant_mod  # noqa: E402  (pure; no network/creds)
import requests  # noqa: E402


def _post(path, body):
    r = requests.post(f"{client.DA}{path}",
                      headers={**client._auth_headers(), "Content-Type": "application/json"},
                      data=json.dumps(body), timeout=60)
    return r


def ensure_bucket():
    # create_bucket() now defaults to policyKey="persistent" (see da/client.py).
    res = client.create_bucket()
    print(f"[bucket] key={client.bucket_key()} region={client.OSS_REGION} "
          f"policy=persistent -> {res}")
    print("[bucket] record this key+region in da/STORE.md (persistent drawing store)")


def ensure_activity(spec):
    name = spec["id"]
    r = _post("/activities", spec)
    if r.status_code in (200, 201):
        print(f"[activity] created {name} v{r.json().get('version')}")
    elif r.status_code == 409:
        print(f"[activity] {name} already exists (409)")
    else:
        raise RuntimeError(f"activity {name} failed {r.status_code}: {r.text[:300]}")
    # alias 'prod' -> version 1 (or newest)
    ver = r.json().get("version", 1) if r.status_code in (200, 201) else 1
    a = _post(f"/activities/{name}/aliases", {"id": client.ALIAS, "version": ver})
    if a.status_code in (200, 201):
        print(f"[alias] {name}+{client.ALIAS} -> v{ver}")
    elif a.status_code == 409:
        print(f"[alias] {name}+{client.ALIAS} exists (409) "
              f"(PATCH /activities/{name}/aliases/{client.ALIAS} to bump version)")
    else:
        raise RuntimeError(f"alias {name} failed {a.status_code}: {a.text[:300]}")


def tool_activity_spec(tool):
    """Build a LeafTool_<op> script Activity from a §2 tool with an inline LISP body.

    tool['engine_script'] must be the .scr/LISP content that writes result.json
    (a §3-compatible {result, overlay?} JSON) using $(args[Params].path) if needed.
    """
    op = tool.get("engine_op") or tool["name"].replace("-", "_")
    scr = tool["engine_script"]
    if "\r\n" not in scr:
        scr = scr.replace("\n", "\r\n") + "\r\n"
    return {
        "id": f"{client.TOOL_ACTIVITY_PREFIX}{op}",
        "engine": client.ENGINE,
        "commandLine": [
            r'$(engine.path)\accoreconsole.exe /i "$(args[HostDwg].path)" '
            r'/s "$(settings[script].path)"'
        ],
        "parameters": {
            "HostDwg": {"verb": "get", "required": True, "localName": client.HOSTDWG_LOCALNAME},
            "Params": {"verb": "get", "required": False, "localName": "params.json"},
            "Result": {"verb": "put", "required": True, "localName": client.TOOL_RESULT_LOCALNAME},
        },
        "settings": {"script": {"value": scr}},
        "description": tool.get("description", f"Leaf tool {tool['name']}"),
    }


def _safe_bucket_key(tenant_id=None, per_bucket=False):
    """Best-effort bucket key for the dry-run report — reads only the LOCAL creds
    file (no network). Falls back to a placeholder if creds are absent."""
    try:
        base = client.bucket_key()
    except Exception:  # noqa: BLE001  (creds missing / unreadable)
        base = "leaf-web-store-<client_id_suffix>"
    if per_bucket and tenant_id is not None:
        try:
            return tenant_mod.tenant_bucket(tenant_id, base=base if not base.startswith("leaf-web-store-<") else None)
        except Exception:  # noqa: BLE001
            return f"{base}-t-{tenant_id}"
    return base


def dry_run_report(tenant_id=None, per_bucket=False, tools_path=None):
    """Print the exact ids + keys that would be created/used. ZERO APS mutation,
    ZERO network — no token, no nickname fetch (uses env APS_NICKNAME or a
    placeholder)."""
    nick = os.environ.get("APS_NICKNAME", "<nickname>")
    strategy = "per-bucket (reversible alternative)" if per_bucket else "shared bucket + per-tenant key prefix (default)"
    print("=== provision_live --dry-run (no APS mutation, no network) ===")
    print(f"[engine]      {client.ENGINE}")
    print(f"[isolation]   {strategy}")
    print(f"[concurrency] APS_MAX_CONCURRENCY (Flex ceiling) reads at run time; see da/queue.py")
    bucket = _safe_bucket_key(tenant_id, per_bucket)
    print(f"[bucket]      key={bucket} region={client.OSS_REGION} policy=persistent (409=exists tolerated)")
    ext = f"{nick}.{client.EXTRACT_ACTIVITY}+{client.ALIAS}"
    print(f"[activity]    extract (dwg): {ext}  (GLOBAL - shared across tenants)")
    dxf = f"{nick}.{client.EXTRACT_DXF_ACTIVITY}+{client.ALIAS}"
    print(f"[activity]    extract (dxf): {dxf}  (--with-dxf-activity / --dxf-activity-only)")
    print(f"[activity]    tools:   {nick}.{client.TOOL_ACTIVITY_PREFIX}<engine_op>+{client.ALIAS}  (GLOBAL)")
    if tenant_id is not None:
        tid = tenant_mod.validate_tenant_id(tenant_id)
        prefix = tenant_mod.tenant_key_prefix(tid)
        print(f"[tenant]      id={tid}")
        print(f"[tenant]      key prefix (per-run scratch): {prefix}")
        print(f"[tenant]      example input key:  {prefix}in/<ts>_rooftop_demo.dwg")
        print(f"[tenant]      example output key: {prefix}out/<ts>_count_by_layer.result.json")
        print(f"[tenant]      versioned store (da/store.py) namespace: tenants/{tid}/drawings/<drawing_id>/v/<n>.dwg")
        if per_bucket:
            print(f"[tenant]      per-tenant bucket: {tenant_mod.tenant_bucket(tid)}")
        else:
            print(f"[tenant]      NOTE: shared-bucket strategy => no per-tenant bucket to create; "
                  f"isolation is by key prefix. No live provisioning needed for this tenant.")
    if tools_path:
        try:
            reg = json.load(open(tools_path))
            for t in reg.get("tools", []):
                if t.get("kind") == "script" and t.get("engine_script"):
                    print(f"[activity]    would ensure {nick}.{client.TOOL_ACTIVITY_PREFIX}"
                          f"{t.get('engine_op') or t['name'].replace('-', '_')}+{client.ALIAS}")
        except Exception as exc:  # noqa: BLE001
            print(f"[tools]       could not read {tools_path}: {exc}")
    print("DONE (dry-run). No APS calls were made.")


def provision_tenant(tenant_id, per_bucket=False):
    """LIVE, idempotent per-tenant provisioning. Default (shared bucket + key
    prefix) needs NO per-tenant live resource beyond the shared bucket + global
    Activities; --per-bucket creates a per-tenant OSS bucket instead."""
    tid = tenant_mod.validate_tenant_id(tenant_id)
    print(f"[tenant] id={tid} isolation={'per-bucket' if per_bucket else 'shared-bucket+key-prefix'}")
    print(f"[tenant] key prefix (per-run scratch): {tenant_mod.tenant_key_prefix(tid)}")
    if per_bucket:
        bkey = tenant_mod.tenant_bucket(tid)
        os.environ["APS_BUCKET"] = bkey  # target the per-tenant bucket for create_bucket()
        res = client.create_bucket()
        print(f"[tenant] per-tenant bucket key={bkey} -> {res} (409=exists tolerated)")
    else:
        print("[tenant] shared-bucket strategy: no per-tenant bucket created; the shared bucket "
              "+ global Activities already cover this tenant (isolation is by key prefix).")
    print(f"[tenant] DONE. Client can submit WorkItems as tenant {tid!r} (APS_LIVE=1).")


def _bundle_id_from_zip(zip_path):
    """Bundle id = PackageContents.xml's ApplicationPackage/@Name inside the zip (never the file name)."""
    import zipfile
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(zip_path) as zf:
        pc = [n for n in zf.namelist() if n.endswith("PackageContents.xml")]
        if not pc:
            raise SystemExit(f"{zip_path}: no PackageContents.xml in the bundle")
        root = ET.fromstring(zf.read(pc[0]))
    name = root.attrib.get("Name", "")
    if not client._APPBUNDLE_ID_RE.match(name):
        raise SystemExit(f"{zip_path}: PackageContents Name {name!r} is not a valid bundle id")
    return name


def main():
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    per_bucket = "--per-bucket" in argv
    tenant_id = None
    if "--tenant" in argv:
        i = argv.index("--tenant")
        if i + 1 < len(argv):
            tenant_id = argv[i + 1]
        else:
            print("--tenant requires an id", file=sys.stderr)
            sys.exit(2)
    tools_path = None
    if "--tools" in argv:
        tools_path = argv[argv.index("--tools") + 1]
    appbundle_zip = None
    if "--appbundle" in argv:
        i = argv.index("--appbundle")
        if i + 1 < len(argv):
            appbundle_zip = argv[i + 1]
        else:
            print("--appbundle requires a zip path", file=sys.stderr)
            sys.exit(2)

    if dry:
        dry_run_report(tenant_id=tenant_id, per_bucket=per_bucket, tools_path=tools_path)
        if appbundle_zip:
            bid = _bundle_id_from_zip(appbundle_zip)
            print(f"[appbundle]   would ensure {bid} from {appbundle_zip} "
                  f"({os.path.getsize(appbundle_zip)} bytes) engine={client.ENGINE} alias={client.ALIAS}")
            print(json.dumps(client.ensure_appbundle(bid, appbundle_zip, dry_run=True), indent=2))
        return

    dxf_only = "--dxf-activity-only" in argv
    with_dxf = "--with-dxf-activity" in argv or dxf_only

    # ---- LIVE from here (mutating calls) ----
    print(f"[auth] nickname={client.nickname()} engine={client.ENGINE}")

    if dxf_only:
        # Narrow lane: create JUST the DXF extract Activity + alias and stop.
        # Touches no bucket, no DWG Activity, no tools, no tenant — the safest
        # way to add DXF extraction to an environment already provisioned.
        ensure_activity(client.extract_dxf_activity_spec())
        print("DONE (dxf-activity-only). LeafExtractDxf+prod is live; nothing else changed.")
        return

    if appbundle_zip:
        # Narrow lane: (re)publish ONE compiled bundle + its prod alias, nothing else.
        bid = _bundle_id_from_zip(appbundle_zip)
        res = client.ensure_appbundle(bid, appbundle_zip)
        print(f"[appbundle] {bid} -> {res}")
        if not tools_path:
            print("DONE (appbundle). Provision its Activity with --tools <registry.json>.")
            return

    ensure_bucket()
    ensure_activity(client.extract_activity_spec())
    if with_dxf:
        ensure_activity(client.extract_dxf_activity_spec())
    if tools_path:
        reg = json.load(open(tools_path))
        for t in reg.get("tools", []):
            if t.get("kind") == "script" and t.get("engine_script"):
                ensure_activity(tool_activity_spec(t))
            elif t.get("kind") == "appbundle":
                # Activity for a compiled bundle: /al loads it, script runs its command.
                ensure_activity(client.tool_activity_spec(t))
            else:
                print(f"[skip] {t.get('name')} (kind={t.get('kind')}, no inline engine_script)")
    if tenant_id is not None:
        provision_tenant(tenant_id, per_bucket=per_bucket)
    print("DONE. Client can now submit WorkItems (APS_LIVE=1).")


if __name__ == "__main__":
    main()
