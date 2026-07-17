"""da/da_runtool.py — CLI for running a tool (§2) via APS Design Automation.

    python da/da_runtool.py --dry-run <dwg> <tool.json> [params.json]
    python da/da_runtool.py <dwg> <tool.json> [params.json]

<tool.json> is a single tool package (§2), OR an object {"tools":[...]} plus
--name <tool-name> to pick one. --dry-run performs NO live APS calls; it mints a
token and prints the WorkItem body referencing the tool's Activity + real engine.
Output is always a Result envelope (§3) on stdout for the live path.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client  # noqa: E402


def _load_tool(path, name=None):
    obj = json.load(open(path))
    if isinstance(obj, dict) and "tools" in obj:
        tools = obj["tools"]
        if name:
            for t in tools:
                if t.get("name") == name:
                    return t
            raise SystemExit(f"tool '{name}' not in {path}")
        return tools[0]
    return obj


def main():
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    name = None
    if "--name" in argv:
        i = argv.index("--name")
        name = argv[i + 1]
        del argv[i:i + 2]
    if len(argv) < 2:
        print(__doc__)
        sys.exit(2)
    dwg, tool_path = argv[0], argv[1]
    params = json.load(open(argv[2])) if len(argv) > 2 else {}
    tool = _load_tool(tool_path, name)

    if dry:
        tok = client.auth_token()
        print(f"[auth] token OK (len={len(tok)}), nickname={client.nickname()}")
        print(f"[engine] {client.ENGINE}")
        print(f"[tool] {tool.get('name')} v{tool.get('version')} "
              f"kind={tool.get('kind')} engine_op={tool.get('engine_op')}")
        body = client.run_tool(dwg, tool, params, dry_run=True)
        print("\n=== WORKITEM SUBMISSION BODY (POST /da/us-east/v3/workitems) ===")
        print(json.dumps(body, indent=2))
        return

    envelope = client.run_tool(dwg, tool, params, dry_run=False)
    print(json.dumps(envelope, indent=2))


if __name__ == "__main__":
    main()
