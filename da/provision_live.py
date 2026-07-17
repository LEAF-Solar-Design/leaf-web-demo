"""da/provision_live.py — ONE-TIME live provisioning. ROOT runs this, not lanes.

Creates on APS Design Automation exactly what the client needs to submit WorkItems:
  1. the OSS bucket (transient policy),
  2. the extract Activity (pure-LISP, no AppBundle) + a 'prod' alias.

It performs LIVE mutating calls, so it is deliberately NOT invoked by any lane and
NOT by the dry-run CLIs. Run it once:  python da/provision_live.py
Add --tools <registry.json> to also create a LeafTool_<engine_op> script Activity
per 'kind:script' tool that carries an inline LISP body under tool["engine_script"]
(Lane B supplies those scripts; this is a convenience for the demo's tools).

Safe to re-run: bucket 409 and activity/alias 409 are treated as 'already exists'.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client  # noqa: E402
import requests  # noqa: E402


def _post(path, body):
    r = requests.post(f"{client.DA}{path}",
                      headers={**client._auth_headers(), "Content-Type": "application/json"},
                      data=json.dumps(body), timeout=60)
    return r


def ensure_bucket():
    res = client.create_bucket()
    print(f"[bucket] {client.bucket_key()} -> {res}")


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


def main():
    print(f"[auth] nickname={client.nickname()} engine={client.ENGINE}")
    ensure_bucket()
    ensure_activity(client.extract_activity_spec())
    if "--tools" in sys.argv:
        reg = json.load(open(sys.argv[sys.argv.index("--tools") + 1]))
        for t in reg.get("tools", []):
            if t.get("kind") == "script" and t.get("engine_script"):
                ensure_activity(tool_activity_spec(t))
            else:
                print(f"[skip] {t.get('name')} (kind={t.get('kind')}, no inline engine_script)")
    print("DONE. Client can now submit WorkItems (APS_LIVE=1).")


if __name__ == "__main__":
    main()
