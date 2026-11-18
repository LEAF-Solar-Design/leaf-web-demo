# Literal operand replay

Parent: Studio X-bench X04 intake, track 2. Run the fixture recipe from
[the four-tool walk](x04-four-tool-offline-walk.md) to create request.json and
after.json, then export a valid evidence artifact:

```powershell
python scripts/export_x04_offline_evidence.py request.json --out evidence.json
python -c "from pathlib import Path; data=Path('evidence.json').read_bytes(); [Path(name).write_bytes(data) for name in ['-', '-leading.json', 'two words.json', '\u96ea.json']]"
python scripts/compare_x04_offline_evidence.py --out delta.json -- ./- -leading.json
python scripts/batch_validate_x04_evidence.py --out paths.json -- ./- -leading.json 'two words.json' '雪.json'
```

The comparison exits 0 with zero count delta and empty memberships. Batch exits 0
with totals 4/4/0 and indices 0, 1, 2, 3. Path labels retain the supplied strings,
including ./-, spaces and Unicode. The double dash ends option parsing so the
leading-hyphen filename is read literally. It does not change stdin semantics.
The exact input token - selects stdin for comparison. The token ./- reads the
file named dash. Batch requires explicit file paths and refuses the exact token
- with exit 2. Use ./- for a dash-named batch input.

```powershell
python scripts/batch_validate_x04_evidence.py - --out paths.json
python scripts/batch_validate_x04_evidence.py --out paths.json -- ./-
```

The first command preserves paths.json and refuses. The retry exits 0 and writes
one indexed valid occurrence labeled ./-. To check output failures, use an
existing directory or a missing parent, then retry a regular destination:

```powershell
python -c "from pathlib import Path; Path('output-dir').mkdir(exist_ok=True); Path('sentinel.bin').write_bytes(b'unrelated')"
python scripts/compare_x04_offline_evidence.py evidence.json after.json --out output-dir
python scripts/batch_validate_x04_evidence.py evidence.json --out missing-parent/report.json
python scripts/compare_x04_offline_evidence.py evidence.json after.json --out retry.json
python -c "from pathlib import Path; assert Path('sentinel.bin').read_bytes() == b'unrelated'"
```

Both failed publications exit 2 with diagnostics and clean their own temporary
files. The retry exits 0. For an output symlink to an unrelated file, atomic
publication replaces the link itself with a regular complete report and retains
the referenced sentinel bytes. Input/output aliases still refuse before writing.

UTF-8 BOM inputs are accepted. Invalid UTF-8 and escaped lone surrogates refuse
comparison or become isolated batch failures. Valid retries remain usable.
Different selected IDs can describe fully overlapping sets and yield selected-ID
deltas while counts and highlights stay equal. Source-byte integrity stays
unverified throughout these synthetic replays. Real staging, browser and original
drawing-byte proof remain separate X04 acceptance receipts.
