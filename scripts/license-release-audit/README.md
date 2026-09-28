# License release audit (P-061, P-062)

A read-only git audit of the acadrust (MPL-2.0) license posture. It answers two
questions from `docs/CAD-ENGINE-LICENSE-REVIEW.md`, and it only reads git: no
network, no working-tree writes, and every git call has a time bound.

```bash
python scripts/license-release-audit/audit.py            # JSON report on stdout
python scripts/license-release-audit/audit.py --out r.json
python scripts/license-release-audit/verify.py           # report shape check, prints ALL PASS
```

## P-061: the unscanned window

The license fence (`scripts/check_license_fence.py`) scans only
`SCAN_ROOTS = ("web", "vendor")`. The acadrust wrapper crate first lived at
`engine/acadrust-worker/`, so the fence's green for that period said nothing
about it. `audit.py` walks every commit reachable from `HEAD` and finds each one
where acadrust crate source or a manifest naming acadrust existed outside those
roots. It counts a path when one of these holds:

- a directory component contains `acadrust`;
- the file name contains `acadrust` and ends in `.rs`, `.wasm`, `.toml` or `.lock`;
- it is a `Cargo.toml` or `Cargo.lock` whose content at that commit mentions acadrust.

Other out-of-root paths named for acadrust, such as the subprocess adapter
`engine/acadrust_adapter.py` and its tests, hold no engine code. The report lists
them under `out_of_root_acadrust_named_paths_not_counted`; they are named, not
hidden.

For each commit in the window, the audit imports the fence from its own file
(it does not copy it), extracts the missing root from that commit's tree into a
temporary directory, and runs `scan_tree` over it. It also copies each crate
directory to the fence's `ALLOWED_ACADRUST_PREFIX` and scans that copy, which
shows what the fence would have said had the crate sat where it sits now. The
fence's acadrust prefix rule always fires on the crate's own files at the old
path. Those hits are counted as `expected_prefix_hits_inside_crate_dirs`, and
they are not findings when the relocated copy scans clean.

Each commit gets one disposition:

- `clean`: no OpenCADStudio identifier, no GPL-3.0 header, no acadrust reference outside the crate directory, and the relocated copy scans clean;
- `finding`: any other fence violation. The note says whether it sits inside the crate directory or elsewhere in the added root;
- `unresolved`: the tree could not be read or extracted. The audit fails closed and never guesses `clean`.

Scans are cached per tree object, so the cost grows with the number of distinct
trees, not the length of the window.

## P-062: the pin and the NOTICE

- `cargo_pin_is_bare_rev` is true only when `vendor/acadrust-worker/Cargo.toml`
  names acadrust exactly once, as `dependencies.acadrust = { git, rev }` with a
  40-hex rev and no other keys, when no `[patch]` or `[replace]` entry names it,
  and when `HEAD` holds no vendored acadrust directory or cargo vendored-sources
  config. `cargo_pin_detail` also says whether the pinned rev matches the rev the
  review accepted.
- `notice_present` is true when a tracked file at `HEAD`, outside `docs/`,
  `plans/`, tests and this directory, carries the acadrust Mozilla Public License
  notice. The check joins string literals that are split across lines before it
  matches, so a notice held in a source constant is found. The location names the
  file and line, whether the wording is exactly the review's NOTICE line, and
  which web file renders `cad_engine?.notice`.

## What it proves

- Which commits on the current branch carried acadrust crate source or manifests
  outside the fence's scope, and the fence's verdict on those files under
  today's fence logic.
- Whether today's Cargo pin still has the shape the review accepted, and whether
  the NOTICE text exists in a shipped source file.

## What it does not prove

- The fence at `HEAD` is not the fence each commit ran in CI. Today's rules are
  applied to old trees, and the fence's own historical green over `web/` and
  `vendor/` is taken as given, not re-run.
- Only tracked content is audited. Untracked build output, such as a local
  `wasm-pack` `pkg/` directory copied into a locally built image, is invisible to
  git. The review's own local-build residual still stands.
- Commits reachable only from other branches are out of scope.
- A clean fence verdict is not a license opinion. It shows only that the fence's
  deny rules do not fire.
- `notice_present` shows that the text exists in the served source, not that a
  deployed build renders it to a user, and not that `cad_edit` is enabled
  anywhere.
