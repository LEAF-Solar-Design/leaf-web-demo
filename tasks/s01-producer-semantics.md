# Original-layout producer semantics

Parent: Studio18 S01, original-input generation in the hosted Solar CAD workspace.

- Add an explicit preset-loading choice for original-intake publication.
- Preserve current loading for existing requests and complete original-input custody.
- Bind the chosen loading rule to generation models, receipt and exported parameters.
- Test request validation, historical/current model consistency, retry identity and UI capture.

Scope: project_solar_materialization.py and its focused tests; SolarWorkspaceTools and the publication client and their focused tests.
Risk: geometry and recorded parsed models must use the same loader. No new route, authority change or evaluator data.
