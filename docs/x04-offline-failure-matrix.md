# X04 offline failure matrix

Parent: Studio X-bench X04 raw drawing intake. Every input below is synthetic.
Run from the X04 module-count worktree root in PowerShell.

```powershell
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_count_modules.py server/tests/test_x04_selection_drawing_isolation.py server/tests/test_x04_module_selection_failures.py -q
node web/node_modules/vitest/vitest.mjs run src/components/ModuleSetPicker.test.jsx src/components/DrawingDependencyReport.test.jsx --root web
```

| Failure | Synthetic reproduction | Observable expected result |
|---|---|---|
| Overlapping selection | Select three sets containing repeated A and B handles, including case and 0x aliases. | Physical union counts three, highlights A/B/D once, and leaves input unchanged. Per-set counts may overlap. |
| Missing handles | Two records have no handle. | Count two records without inventing a physical identity or highlight handle. |
| Wrong drawing selection | Submit the East drawing set ID to the West drawing, alone and mixed with a valid ID. | Trusted loader returns an actionable error and no partial result or overlay. |
| Stale UI choice | Change drawing key while reusing a set ID, then return to the old drawing. | Neither drawing reuses the old check. Submission needs a fresh choice. |
| Disabled submission | Select two candidates, enter running state, then deselect and reselect after it ends. | Disabled submission emits nothing. Final callback contains current choices only. |
| Empty dependency report | Replace a report with an empty report from another drawing. | Old dependency rows disappear and the new coverage warning stays visible. |
| Keyboard activation | Focus labeled native checkboxes, activate with Space, and activate submit with Enter. | Both chosen sets reach one callback. |

The keyboard test dispatches DOM key events and simulates the browser's native click
default because JSDOM does not implement that default. It verifies labeled controls,
focus, selection and callback behavior. It is not physical keyboard or real-browser
evidence. No screenshots, real customer drawings or staging acceptance are implied.

These checks also do not prove raw dependency completeness, rendered block geometry,
upload deadlines or exact counts on the held-out corpus. Those retain their staging,
raw-inventory and shared-grader conditions in XBENCH/status/X04-status.md.
