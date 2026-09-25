# cable-export: the inverter rating reads the catalog's kW as watts

**Plugin:** Branch2025 `StringHomerunExportForm.cs` 799-802 (`WriteEquipmentSchedule`: `acKw =
SafeParseDouble(inverterData.maxACPower) / 1000.0`, printed with `N1` and "kW AC"), and the same division at 948
(`WriteInverterScheduleCD`, the DC/AC ratio). Read at master a94db8d9 (C:/tmp/solar-parity/wt-b25-main).
**Proven:** test build 2 of the fixed plugin (Branch2025 #309) on the VMC, 2026-09-25, step i9 of the inverter chain
(contract G35): the saved workbook `i9-homeruns.xlsx` (receipt `w7-testbuild2-20260925`) rates the SG250HX
"0.2kW AC".

## Why it is a bug

The inverter catalog stores `maxACPower` in kW, not watts: the SG250HX row reads `maxACPower` 250 beside
`maxDCPower` 375 (a 250 kW inverter rated for about 375 kWp of DC), and its 180.5 A at 800 V is 250 kVA. Dividing
by 1000 again makes a 250 kW inverter "0.2kW". `N1` prints 0.2 for any catalog value from 150 to 249.9 read as
watts; the test build 2 host's catalog row was not captured, so whether it read 250 (with the midpoint rounded
down) or a derated value such as 225 is unknown. Either way the rating is a thousand times low.

## Studio's behaviour, and the declared diff

Studio keeps the catalog's kW (`_catalog_ac_kw` in `server/solar_inverter_outputs.py`) and prints the plugin's
own format, so the Equipment Schedule's inverter row reads "250.0kW AC, 800V, 180.5A" from the capture host's
catalog row. The workbook text differs in exactly one line, the Equipment Schedule's fourth row (cell F4):

    plugin  INV-1..5	String Inverter	Sungrow	SG250HX	5	0.2kW AC, 800V, 180.5A	UL 1741	690.4
    studio  INV-1..5	String Inverter	Sungrow	SG250HX	5	250.0kW AC, 800V, 180.5A	UL 1741	690.4

The Inverter Schedule's DC/AC ratio uses the same kW, but with no module on the capture host every ratio prints
"DC/AC: -" on both sides, so it adds no diff here. When the plugin reads `maxACPower` as kW, this row matches and
the declaration can be dropped.

## Also observed, not diverged

The AC disconnect row reads "800V, 226A": 125 % of 180.5 A is 225.6 A, above the form's largest listed OCPD size
(200 A, line 865), so the form takes the ceiling (line 867). NEC 240.6(A) lists 225 and 250 A as standard sizes, so
the next standard size is 250 A. Studio prints the plugin's 226 A because it is the form's documented fallback
rule rather than a units error; it is recorded here for review.
