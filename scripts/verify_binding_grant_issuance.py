"""Run binding-grant issuance checks without shadowing stdlib platform."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


def run(test_file, *, require_all=False):
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="binding-grant-check-") as directory:
        report = Path(directory) / "results.xml"
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(root / "platform/tests" / test_file),
             f"--junitxml={report}"], cwd=root.parent, check=False,
        )
        if result.returncode:
            return result.returncode
        suites = ET.parse(report).getroot()
        cases = suites.findall(".//testcase")
        if not cases or any(case.find("failure") is not None or case.find("error") is not None
                            for case in cases):
            return 1
        if require_all and any(case.find("skipped") is not None for case in cases):
            print("DB tests must all run when DATABASE_URL is set", flush=True)
            return 1
        return 0


def main():
    result = run("test_binding_grant_issuance_static.py", require_all=True)
    if result:
        return result
    if not os.environ.get("DATABASE_URL"):
        print("DB tests not run locally", flush=True)
        return 0
    return run("test_binding_grant_issuance.py", require_all=True)


if __name__ == "__main__":
    raise SystemExit(main())
