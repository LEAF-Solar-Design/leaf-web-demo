"""One instrumented real APS extract run -> print the cost receipt.

Cost basis (confirmed 2026-07): AutoCAD Design Automation = 2 Flex tokens/engine-hour,
~$3/token => $6.00/engine-hour. Billable window = timeDownloadStarted -> timeUploadEnded.
"""
import os, sys, time, json
sys.path.insert(0, "da")
os.environ["APS_LIVE"] = "1"
import client
from datetime import datetime

USD_PER_HR = 6.0
dwg = "data/rooftop_demo.dwg"

def iso(t): return datetime.fromisoformat(t.replace("Z", "+00:00"))

name = os.path.basename(dwg)
ts = int(time.time())
ik, ok = f"in/{ts}_{name}", f"out/{ts}_{name}.families.txt"
aid = client.activity_qualified(client.EXTRACT_ACTIVITY)

client.upload_object(dwg, ik)
in_url = client.signed_download_url(ik)
up_key, out_url = client.signed_upload_url(ok)
args = {"HostDwg": {"url": in_url, "verb": "get"}, "Result": {"url": out_url, "verb": "put"}}
st = client.submit_workitem(aid, args, dry_run=False, poll=True)
print("status:", st.get("status"))
s = st.get("stats", {})
for k in ("timeQueued","timeDownloadStarted","timeInstructionsStarted","timeInstructionsEnded","timeUploadEnded"):
    print(f"  {k}: {s.get(k)}")

billable = None
if s.get("timeDownloadStarted") and s.get("timeUploadEnded"):
    billable = (iso(s["timeUploadEnded"]) - iso(s["timeDownloadStarted"])).total_seconds()
engine = None
if s.get("timeInstructionsStarted") and s.get("timeInstructionsEnded"):
    engine = (iso(s["timeInstructionsEnded"]) - iso(s["timeInstructionsStarted"])).total_seconds()

print("\n=== COST RECEIPT (real APS WorkItem) ===")
print(f"  engine (instructions) seconds: {engine}")
print(f"  BILLABLE seconds (download->upload): {billable}")
if billable is not None:
    usd = billable / 3600.0 * USD_PER_HR
    print(f"  cost @ $6.00/engine-hr: ${usd:.5f}  ({billable:.1f}s)")
    print(f"  implied per-1000-runs: ${usd*1000:.2f}")
