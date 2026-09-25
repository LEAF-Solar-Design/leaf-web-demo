# string-rebuild: the shipped plugin cannot run STRINGREBUILD

**Plugin:** Branch2025 `BranchCmd.cs` 18464 (`[CommandMethod("STRINGREBUILD")]`, an instance method) and 456 (the
class's only constructor, `BranchCmd(bool)`), filed on Branch2025 issue #281.
**Proven:** licensed AutoCAD 2025 on the VMC, 2026-09-23, rooftop chain step c11 (from the saved c10 drawing):
every invocation raises the AutoCAD modal "Cannot dynamically create an instance of type
'Leaf_Solar_Design.BranchCmd'. Reason: No parameterless constructor defined." After the modal is dismissed, the
drawing's entities are unchanged on save and reopen.

## Why

AutoCAD creates the command class per document through a parameterless constructor. `BranchCmd` has none, and
STRINGREBUILD is its only command, so the command is unreachable.

## Studio's behaviour, and the declared diffs

Studio rebuilds each string's panel association and reports how many strings it rebuilt (`rebuilt-strings`). The
plugin's evidence is the `command-error` report plus one setting row: the step ran in a fresh session, and saving
it appended the two duplicate cable-catalog entries described in
`docs/parity/divergences/pad-grading-batch/settings-save-duplicates-cable-catalog.md`. The declared diffs are
exactly the comparator's.

## Retired

Retired by Branch2025 #309 (STRINGREBUILD reachable), #318 (whole-string match) and #328 (a string whose panels are unchanged keeps its stored order): recaptured 2026-09-25 on an unsigned test build of master 1d4f900f plus #327 and #330 (rooftop chain c11, c10 reopened on the same build). STRINGREBUILD prints "Rebuilt panel associations for 173 string(s)", the c10 and c11 dumps are identical, and the receipt compares with no diffs. The signed release carrying these fixes is pending the operator (Solar residuals R23).
