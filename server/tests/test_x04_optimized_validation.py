"""Optimization must not remove X04 validation or change refusal semantics."""
import json
import os
from pathlib import Path
import subprocess
import sys

X04_ROOT = Path(__file__).resolve().parents[2]


def child_result(body, *, optimized=False, seed=0):
    # Supply explicit configuration only. Never copy or inspect host environment values.
    env = {"PYTHONHASHSEED": str(seed)}
    if os.name == "nt":
        env["SystemRoot"] = "C:/Windows"
    bootstrap = ("import sys,json,platform\n" + f"sys.path[:] = {sys.path!r}\n"
        + "from pathlib import Path\nimport tool_loader\n"
        + f"TOOL=next(t for t in json.loads(Path({str(X04_ROOT / 'engine/registry.json')!r}).read_text())['tools'] if t['name']=='count-modules')\n")
    command = [sys.executable, "-P"] + (["-O"] if optimized else []) + ["-c", bootstrap + body]
    process = subprocess.run(command, cwd=X04_ROOT, env=env, capture_output=True, text=True, timeout=45)
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def test_optimized_and_normal_validation_refusals_are_identical():
    body = '''
cases=[(None,{}),({'inserts':'bad'},{}),({'inserts':[{'handle':3}]},{}),
       ({'inserts':[{'handle':'A','name':'Panel'}]},{'set_ids':True}),
       ({'inserts':[{'handle':'A','name':'Panel'}]},{'set_ids':['missing']})]
results=[]
for intake,params in cases:
    result=tool_loader.run_tool_dynamic(TOOL,intake,params,aps_live=False)
    results.append({'ok':result['ok'],'error':result.get('error'),
                    'result':result.get('result'),'overlay':result.get('overlay')})
print(json.dumps(results,sort_keys=True))
'''
    normal = child_result(body)
    assert normal == child_result(body, optimized=True)
    for result in normal:
        assert not result["ok"] and not result["result"] and not result["overlay"]
        message = result["error"]["message"]
        assert any(text in message for text in ("re-import", "set_ids", "list the sets again"))
