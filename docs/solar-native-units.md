# Solar native DXF units

Parent: reusable saved Solar layouts in Leaf's shared CAD workspace.

New native layouts declare their physical document units in the DXF HEADER:
metres use `$INSUNITS` code 6 and feet use code 2. Serialization declares units;
it does not rescale coordinates, change poses or replace input provenance.
These are the two units supported by the physical-state model.

A native layout with an explicit conflicting header receives the typed
`NATIVE_LAYOUT_UNITS_MISMATCH` refusal. Duplicate or malformed declarations
receive `NATIVE_LAYOUT_UNITS_INVALID`. A reader never silently chooses one
conflicting declaration.

Legacy native layouts with no declaration, or with unitless code 0, remain
readable using their verified native document units. Serializing a new version
adds the matching explicit declaration. Immutable predecessors remain unchanged;
an exact restore keeps its original bytes.

The focused tests independently tokenize metre/foot output, exercise legacy
reads and malformed/conflicting headers, and check raw foreign header data and
frame attributes survive serialization. Run:

```powershell
python -P -m pytest -o pythonpath=server server/tests/test_solar_native_layout_dxf.py -q
```

The insertion-unit values follow [Autodesk's DXF HEADER reference](https://help.autodesk.com/cloudhelp/2026/ENU/AutoCAD-DXF/files/GUID-A85E8E67-27CD-4C59-BE61-4DC9FADBE74A.htm).
Format tests prove the exported declaration and internal consistency. They do
not prove how a particular external CAD consumer inserts or scales the file.
