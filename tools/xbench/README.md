# Studio X-bench graders

This standard-library-only package supplies shared grading tools for X-benches
beside Studio18. It verifies fixture manifests and staging identity, runs a bench
module, and atomically records a grade and an optional readiness marker. Bench
modules X01 through X10 are supplied in later slices.

From the repository root:

```sh
python tools/xbench/grade.py X01 --manifest /tmp/xbench/manifest.json --base https://staging.example.test --out /tmp/xbench/results --readiness-dir /tmp/xbench/readiness
```

Use `--no-readiness` to suppress the readiness marker. `--benches-dir DIR` selects
a different directory of bench modules. The default readiness directory is
`D:/agents/production-lanes-2026-11-10/XBENCH/driver/readiness`.

Exit codes are 0 for PASS, 1 for FAIL, 4 for UNREADABLE, and 5 for usage or
configuration errors. A failing step takes precedence over an unreadable step.
An empty step list is UNREADABLE. Invalid inputs and absent bench modules produce
no receipts. Fixture or health failures produce an UNREADABLE receipt.

`grade.json` contains these fields in order: `schema`, `bench`, `verdict`,
`source_sha`, `manifest_sha256`, `started_at`, `finished_at`, `steps`, `fixtures`,
and `limits`. Its schema is `leaf.xbench.grade.v1`. Steps are sorted by `n` and
contain `n`, `verdict`, `evidence`, `observed`, and `expected`. Evidence is limited
to 2000 characters. Non-JSON observations and expectations become truncated
representations. Fixture rows include expected identity, size, hash, verification
status, a reason, and observed size and hash. Size mismatches skip hashing.
Timestamps are UTC with a `Z` suffix. The source SHA comes from `/api/health` and
must be 40 lowercase hexadecimal characters; it can be null only for UNREADABLE.
The manifest digest covers the exact raw manifest bytes.

A manifest uses schema `leaf.xbench.manifest.v1`, an X01 through X10 `bench`,
and a list of at most 200 fixtures. Each fixture needs a unique string `id`, a
string `path`, a nonnegative integer `bytes`, and a 64-character lowercase SHA256.
Manifests are limited to 1 MiB. Other top-level metadata is retained. For example,
this synthetic fixture describes an empty file:

```json
{
  "schema": "leaf.xbench.manifest.v1",
  "bench": "X01",
  "fixtures": [
    {
      "id": "synthetic-empty",
      "path": "/tmp/xbench/synthetic-empty.bin",
      "bytes": 0,
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    }
  ]
}
```

Real fixtures and their manifests live outside git. Commit only synthetic data
and examples. Fixture paths are used as given; relative paths resolve against
the process working directory.

Write a bench module as `benches/x01.py` (or the corresponding lowercase ID):

```python
from xbench_core import Step

LIMITS = "Synthetic demonstration only"

def run(ctx):
    return [Step(1, "PASS", "Synthetic check completed", observed=1, expected=1)]
```

`run(ctx) -> list[Step]` receives a `BenchContext` with `bench`, `manifest`,
`base`, `out_dir`, `source_sha`, and verified `fixtures`. Use unique positive step
numbers and verdicts PASS, FAIL, or UNREADABLE. Optional `LIMITS` must be a string.
Module loading errors, exceptions, invalid results, and duplicate step numbers
produce an UNREADABLE receipt. PASS writes `<bench>.json` readiness with the source
SHA, receipt path, UTC timestamp, and verdict READINESS_PASS unless suppressed.

The tests use pytest and synthetic files under `tmp_path`, with fake health
openers and no network calls. The repository's top-level `platform/` shadows the
standard library, so run the suite with the safe import path:

```sh
python -P -m pytest tools/xbench/tests -q -p no:cacheprovider -o pythonpath=tools/xbench
```
