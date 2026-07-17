"""Run all three tools LIVE on APS and diff against the pure-python oracle."""
import os, sys, json
os.environ["APS_LIVE"] = "1"
sys.path.insert(0, "da"); sys.path.insert(0, "engine")
import client, selfcheck

DWG = "data/rooftop_demo.dwg"
intake = json.load(open("data/rooftop_demo.intake.json"))
reg = json.load(open("engine/registry.json"))
PARAMS = {
    "count_by_layer": {},
    "measure_panel_area": {},
    "highlight_panels_near_edge": {"layer": "Panels", "distance": 200},
}

def brief(env):
    r = env.get("result") or {}
    ov = env.get("overlay") or {}
    if "counts" in r: return f"counts={r['counts']}"
    if "matched" in r: return f"matched={r['matched']} handles={len(ov.get('highlight_handles',[]))}"
    if "total_area" in r or "area_sqft" in r:
        return f"area={r.get('total_area', r.get('area_sqft'))}"
    return json.dumps(r)[:120]

allok = True
for t in reg["tools"]:
    op = t["engine_op"]; p = PARAMS.get(op, {})
    live = client.run_tool(DWG, t, p, dry_run=False)
    mock = selfcheck.run_mock(op, intake, p)
    ok = live.get("ok")
    cost = live.get("cost") or {}
    print(f"\n### {t['name']}  (params={p})")
    print(f"  LIVE  ok={ok}  {brief(live)}  cost=${cost.get('usd_est')}  {cost.get('engine_seconds')}s")
    print(f"  MOCK        {brief(mock)}")
    if not ok:
        print(f"  ERROR: {live.get('error')}"); allok = False
print("\nRESULT:", "ALL TOOLS RAN LIVE" if allok else "SOME FAILED")
