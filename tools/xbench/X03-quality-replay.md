# X03 offline quality replay

From the checkout root, run the seven modules with one Python process.

```text
python -P -m pytest --disable-plugin-autoload -o pythonpath=tools/xbench tools/xbench/tests/test_x03_quality.py tools/xbench/tests/test_x03_integration_contracts.py tools/xbench/tests/test_x03_release_contracts.py tools/xbench/tests/test_x03_dashboard_contracts.py tools/xbench/tests/test_x03_selection_oracle.py tools/xbench/tests/test_x03_layer_count_oracle.py tools/xbench/tests/test_x03_beam_length_oracle.py
```

The route check uses Shapely to compare a cable centerline to a closed aisle polygon and array interiors. Boundary contact is allowed. The inverter check checks feasible integer load intervals for each possible minimum fill using the spread between the largest and smallest fractional fills. The string check requires one open chain in an independently supplied undirected adjacency graph. Missing geometry yields INCOMPLETE.

The drawing oracles reconcile selections on one drawing revision, count matching block instances, and compare grouped 3D beam lengths in metres.

The integration, release and dashboard modules use synthetic response, release and execution records. They check catalog identity and units, delivery counts, missing weather inputs, package and rollback identity, and dashboard reconciliation. They establish how to judge an output. Studio generation, live integrations and deployment require separate receipts. No network or deployment is part of this replay.
