# String sizer (k1): both sides refuse the service's double-encoded result

The receipt `docs/parity/receipts/string-sizer/w4-k1.json` compares the plugin's StringSizer with Studio's pinned sizing client on the same request. It passes with no diffs because both sides refuse the same response. Neither commits anything.

**The request** (`k0-intake.json`):
- zip 78701, which the service's S3 weather cache serves, so the run does not depend on NREL/NLR;
- the typed CEC module `Canadian Solar Inc  CS5T 130M`, sent as `Canadian_Solar_Inc__CS5T_130M`;
- the project's default inverter, Sungrow SG250HX (max DC 1500 V);
- fixed tilt, close-mount racking, tilt 5, azimuth 180.

The plugin is 1.1.4.4 on licensed AutoCAD 2025. The 2026-09-24 capture is private.

**What happened:**
- **The service:** answered HTTP 200 (its log shows the P99.5 string length of 41). Since the AWS migration (aws-string-sizer #3, 2026-07-24), `app.py` wraps the already-serialized result in `JSONResponse`, so the body is a JSON string, not the result object (`k1-response.json`).
- **The plugin:** StringSizeForm fails to convert that string ("Error converting value ..."). It shows Calculation Error, and after Cancel the drawing is unchanged.
- **Studio:** its client (`server/solar_sizing_client.py`, pinned to the plugin's contract) rejects the same body. The engine reports the same outcome and commits nothing.

This is a service regression that breaks string sizing for every plugin user, not a parity gap. The one-line fix is LEAF-Solar-Design/aws-string-sizer#10, awaiting an explicit deploy yes. Once it is deployed, the plugin reaches its result form, which commits PanelsInSequence and the VocCold fields. The engine refuses that path until a capture exercises it, and this receipt will then be replaced by the success capture.

A second finding: the plugin's module picker lists `pv_modules_2026.db`, and none of its 241 names maps to a key in the service's CEC database. A module picked from the list therefore always gets a 400 "Key not found". Only a typed CEC-shaped name reaches the model, as here. Filed on the Branch2025 side.
