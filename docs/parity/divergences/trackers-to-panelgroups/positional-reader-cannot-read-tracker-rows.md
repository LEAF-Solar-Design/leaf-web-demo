# trackers-to-panelgroups: the command cannot read the tracker rows the plugin writes

**Plugin:** Branch2025 `Pvcase/LeafTrackersToPanelGroupsCommand.cs` 618-637 (`ReadLeafTrackerXData`), filed on
Branch2025 issue #281.
**Proven:** licensed AutoCAD 2025 on the VMC, 2026-09-23, step d3 of the terrain chain, on a drawing holding the
plugin's own LEAFTRACK output. The command raised the AutoCAD modal "System.FormatException: The input string
'schema_version' was not in a correct format." (stack: `ReadLeafTrackerXData` :635, `TryBuildLeafBlockSpec` :597,
`CollectTrackers` :297) and committed nothing: DBMOD stayed 0 and the reopened dump is byte-identical to the one
before.

## Why

`ReadLeafTrackerXData` reads the tracker record by position and converts slots 1 to 3 with `Convert.ToInt32`
(lines 632 to 635). The tracker writers now emit key=value strings that include `schema_version`, and the
key-aware reader `Terrain/TrackerRowReader.cs` exists but is not used on this path.

## Studio's behaviour, and the declared diffs

Studio reads the same tracker rows through the key-aware reader and converts them to panel groups, reporting how
many groups and slots it created; the plugin's evidence is its empty delta plus the `command-error` report row. The
declared diffs are exactly the comparator's. When the plugin reads tracker rows by key, this capability can be
recaptured for an ordinary receipt.

## Retired

Retired by Branch2025 #310 (tracker XData read by key), with #319 removing an unrelated open-time ProjectName write: recaptured 2026-09-25 on an unsigned test build of master a94db8d9 (terrain chain d3, d2 reopened on the same build), LEAFTRACKERSTOPANELGROUPS creates 237 panel groups from 69,678 module slots and the receipt passes with no declared diffs (Studio's counters from #1441, the plugin adapter's success path from Branch2025 #317). The signed release carrying the fix is pending the operator (Solar residuals R23).
