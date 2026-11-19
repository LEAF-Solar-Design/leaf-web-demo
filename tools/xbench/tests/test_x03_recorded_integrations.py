import hashlib
import pytest
from benches.x03_catalog_oracle import catalog_quality
from benches.x03_issue_delivery_oracle import delivery_quality
from benches.x03_weather_oracle import weather_quality


@pytest.mark.parametrize("plug,socket,retention,loads", [
    pytest.param("sensor", "board", 12, [3, 12], id="check-a-pulled-sensor-lead"),
    pytest.param("feeder", "cabinet", 90, [90, 45], id="qualify-a-cabinet-connector"),
    pytest.param("jumper", "inline", 25, [10, 25, 5], id="check-an-inline-mated-pair"),
])
def test_mated_latch_retention(plug, socket, retention, loads):
    from benches.x03_catalog_oracle import connector_latch_quality as grade

    sheet = dict(revision="r1", rated_amps=20, latched_retention_n={plug: {socket: retention}})
    assert 10 <= sheet["rated_amps"]
    assert grade("r1", sheet, plug, socket, True, loads) == "PASS"
    assert grade("r1", sheet, plug, socket, True, [*loads, retention + 1]) == "FAIL"
    assert grade("r1", sheet, plug, socket, False, loads) == "FAIL"
    assert grade("r1", sheet, plug, socket, None, loads) == "INCOMPLETE"
    assert grade("r1", sheet, plug, "unqualified mate", True, loads) == "INCOMPLETE"


@pytest.mark.parametrize("cable,operation,minimum,readings", [
    pytest.param("control", "reel pull", -10, [-5, -10, 2], id="qualify-a-winter-control-cable-pull"),
    pytest.param("feeder", "field bend", 0, [10, 0, 3], id="check-a-cold-feeder-bend"),
    pytest.param("sensor", "hand dress", 5, [5, 8], id="dress-a-chilled-sensor-lead"),
])
def test_installation_temperature_uses_cold_bend_qualification(cable, operation, minimum, readings):
    from benches.x03_catalog_oracle import cable_handling_temperature_quality as grade

    sheet = dict(revision="test-catalog", service_c=(-40, 80), cold_bend_min_c={cable: {operation: minimum}})
    freezing_event = [*readings, minimum - 1]
    assert sheet["service_c"][0] <= min(freezing_event) <= max(freezing_event) <= sheet["service_c"][1]
    assert grade("test-catalog", sheet, cable, operation, readings) == "PASS"
    assert grade("test-catalog", sheet, cable, operation, freezing_event) == "FAIL"
    assert grade("test-catalog", sheet, cable, "unqualified", readings) == "INCOMPLETE"
    assert grade("test-catalog", sheet, cable, operation, []) == "INCOMPLETE"


@pytest.mark.parametrize("low,high,qualified_cycles", [
    pytest.param(-35, 80, 100, id="check-the-outdoor-controller"),
    pytest.param(-5, 40, 5000, id="qualify-the-cabinet-display"),
    pytest.param(10, 65, 800, id="check-the-power-module"),
])
def test_thermal_cycles_within_operating_range(low, high, qualified_cycles):
    from benches.x03_catalog_oracle import thermal_cycle_quality as grade

    sheet = dict(revision="tc2", operating_c=(-45, 95), thermal_cycle_envelopes=[
        (low, high, qualified_cycles), (low + 1, high, qualified_cycles * 2),
    ])
    assert sheet["operating_c"][0] <= low < high <= sheet["operating_c"][1]
    assert grade("tc2", sheet, low, high, qualified_cycles) == "PASS"
    assert grade("tc2", sheet, low, high, qualified_cycles + 1) == "FAIL"
    assert grade("tc2", sheet, low, high + 1, qualified_cycles) == "FAIL"
    assert grade("tc2", dict(sheet, thermal_cycle_envelopes=[]), low, high, qualified_cycles) == "INCOMPLETE"
    assert grade("unknown", sheet, low, high, qualified_cycles) == "INCOMPLETE"


@pytest.mark.parametrize("trace,peak,dwell", [
    pytest.param([(0, 180), (10, 220), (20, 180)], 220, 10, id="qualify-a-controller-ramp"),
    pytest.param([(0, 200), (4, 200), (8, 230), (14, 230), (18, 200)], 230, 14,
                 id="check-a-module-plateau"),
    pytest.param([(0, 190), (6, 210), (12, 190), (16, 210), (20, 190)], 210, 10,
                 id="sum-two-connector-exposures"),
])
def test_pinned_reflow_peak_and_liquidus_dwell(trace, peak, dwell):
    from benches.x03_catalog_oracle import reflow_profile_quality as grade

    rule = dict(revision="board-r1", max_sample_gap_s=10, liquidus_c=200,
                max_peak_c=peak, max_above_liquidus_s=dwell)
    duration = trace[-1][0]
    assert grade("board-r1", rule, trace, duration) == "PASS"
    assert grade("board-r1", dict(rule, max_peak_c=peak - 1), trace, duration) == "FAIL"
    assert grade("board-r1", dict(rule, max_above_liquidus_s=dwell - 1), trace, duration) == "FAIL"
    assert grade("board-r1", rule, trace[:-1], duration) == "INCOMPLETE"
    assert grade("board-r1", dict(rule, max_sample_gap_s=1), trace, duration) == "INCOMPLETE"


@pytest.mark.parametrize("tool,low,high,target,outside", [
    pytest.param("driver", 1, 5, 1, 0.9, id="qualify-low-torque-terminal"),
    pytest.param("wrench", 10, 40, 40, 41, id="check-rail-clamp-wrench"),
    pytest.param("multiplier", 40, 160, 100, 39, id="qualify-flange-assembly-tool"),
])
def test_torque_tool_calibration(tool, low, high, target, outside):
    from benches.x03_catalog_oracle import torque_tool_quality as grade

    certificate = dict(revision="r1", tool_id=tool, min_nm=low, max_nm=high,
                       valid_from="2026-01-01", valid_through="2026-06-30")
    assert grade("r1", certificate, tool, "2026-06-30", target) == "PASS"
    assert grade("r1", certificate, tool, "2026-07-01", target) == "FAIL"
    assert grade("r1", certificate, tool, "2026-06-30", outside) == "FAIL"
    assert grade("r1", dict(certificate, valid_through=None), tool, "2026-06-30", target) == "INCOMPLETE"
    assert grade("r1", certificate, "another tool", "2026-06-30", target) == "INCOMPLETE"


@pytest.mark.parametrize("zone,group,temp_class,accepted,rejected", [
    pytest.param("Z1", "IIC", "T4", "db IIC T4", "ec IIC T4", id="choose-a-zone-one-enclosure"),
    pytest.param("Z2", "IIB", "T5", "ec IIB T5", "ec IIA T5", id="qualify-a-gas-monitor"),
    pytest.param("Z1", "IIA", "T6", "ia IIA T6", "ia IIA T3", id="check-a-cool-surface-probe"),
])
def test_hazardous_area_approved_markings(zone, group, temp_class, accepted, rejected):
    from benches.x03_catalog_oracle import hazardous_area_quality as grade

    sheet = dict(revision="lab-only", approved_markings={zone: {group: {temp_class: [accepted]}}})
    assert grade("lab-only", sheet, zone, group, temp_class, accepted) == "PASS"
    assert grade("lab-only", sheet, zone, group, temp_class, rejected) == "FAIL"
    assert grade("other", sheet, zone, group, temp_class, accepted) == "INCOMPLETE"
    assert grade("lab-only", sheet, zone, group, None, accepted) == "INCOMPLETE"
    assert grade("lab-only", sheet, zone, group, temp_class, None) == "INCOMPLETE"


@pytest.mark.parametrize("site,dust,water", [("dust chamber",6,4),("water jet",5,6),("immersion",6,8)])
def test_installation_requires_both_qualified_ingress_codes(site,dust,water):
    from benches.x03_catalog_oracle import ingress_quality as grade
    catalog = dict(revision="sheet",exposures={site:dict(dust=[dust],water=[water])})
    assert grade(site,dust,water,catalog,"sheet") == "PASS"
    assert grade(site,dust-1,water,catalog,"sheet") == "FAIL"
    assert grade(site,dust,water-1,catalog,"sheet") == "FAIL"
    assert grade("unknown site",dust,water,catalog,"sheet") == "INCOMPLETE"


@pytest.mark.parametrize("effects,terminal", [([],"quarantined"),([1],"recovered"),([0,1,0],"recovered")])
def test_exhausted_delivery_quarantine_and_recovery(effects,terminal):
    from benches.x03_issue_delivery_oracle import dead_letter_quality as grade
    def replay(n):
        return dict(delivery="job",parent_receipt="q1",new_effects=n)
    rows = [replay(n) for n in effects]
    assert grade(["job"],{"job":"q1"},rows,{"job":terminal}) == "PASS"
    assert grade(["job"],{},[],{"job":"quarantined"}) == "FAIL"
    assert grade(["job"],{"job":"q1"},[replay(1),replay(1)],{"job":"recovered"}) == "FAIL"
    assert grade(["job"],{"job":"q1"},[dict(replay(1),parent_receipt="other")],{"job":"recovered"}) == "FAIL"
    assert grade(["job"],{"job":"q1"},rows,{}) == "INCOMPLETE"


@pytest.mark.parametrize("part,pack,minimum,demand,units", [
    ("clips", 4, 10, 1, 12), ("clamps", 5, 5, 14, 15), ("bolts", 12, 0, 24, 24)])
def test_order_uses_pinned_pack_policy(part, pack, minimum, demand, units):
    from benches.x03_catalog_oracle import pack_order_quality
    row = dict(pack_units=pack, minimum_units=minimum, stock_units=units)
    def check(policy, order, revision="v1"):
        return pack_order_quality(dict(revision="v1", parts={part: policy}), revision, part, demand, order)
    assert check(row, units) == "PASS"
    assert check(row, units - 1) == "FAIL"
    assert check(dict(row, stock_units=units - 1), units) == "FAIL"
    assert check(row, units, "v2") == "FAIL"
    assert check(dict(row, minimum_units=None), units) == "INCOMPLETE"


@pytest.mark.parametrize("part,unit", [("panel","W"), ("beam","m"), ("fan","rpm")])
def test_catalog_identity_and_unique_finite_rating(part, unit):
    row = dict(part=part, version="v2", unit=unit, rating=125)
    def check(rows, result):
        return catalog_quality(rows, part, "v2", unit, result)
    assert check([row], row) == "PASS"
    assert check([row,row],row) == "INCOMPLETE"
    for changes in [dict(rating=True),dict(rating=float("inf")),dict(rating=float("nan")),
                    dict(part="other"), dict(unit="wrong"), dict(version="old")]:
        assert check([row], dict(row,**changes)) == "FAIL"


@pytest.mark.parametrize("drawing", ["roof","frame","duct"])
def test_receiver_effect_after_timeout_retry(drawing):
    old = dict(destination="primary",drawing=drawing,revision="1",payload_bytes=b"first",payload_digest=hashlib.sha256(b"first").hexdigest())
    new = dict(old,revision="2",payload_bytes=b"second",payload_digest=hashlib.sha256(b"second").hexdigest())
    attempts = [dict(old,status="timeout"),dict(old,status="ack"),dict(new,status="ack")]
    assert delivery_quality(attempts,[old,new]) == "PASS"
    for effects in ([old], [old,old,new], [old,dict(new,payload_digest="changed")]):
        assert delivery_quality(attempts,effects) == "FAIL"
    assert delivery_quality(attempts+[dict(old,payload_digest="conflict")],[old,new]) == "FAIL"


@pytest.mark.parametrize("temp,modules,expected", [(-25,10,460),(0,5,215),(50,2,74)])
def test_voltage_reference(temp, modules, expected):
    weather = dict(site="roof",station="local",timezone="UTC",cell_temp=temp,unit="C",timestamp=90)
    def check(data, result):
        return weather_quality(data,site="roof",station="local",as_of=100,max_age=10,voc_at_25=40,
                              coefficient_per_c=-0.003,modules=modules,reported=result)
    assert check(weather,expected) == "PASS"
    assert check(weather,expected+1) == "FAIL"
    assert check({},expected) == "INCOMPLETE"
    for changes in (dict(timestamp=89), dict(unit="F"), dict(site="roof",station="local",timezone="UTC",cell_temp=float("nan"))):
        assert check(dict(weather,**changes),expected) == "FAIL"


@pytest.mark.parametrize("field,bad", [("part",None), ("version"," "), ("unit",[])])
def test_missing_requested_catalog_identity(field,bad):
    row = dict(part="p",version="v",unit="W",rating=20)
    row[field] = bad
    assert catalog_quality([row],row["part"],row["version"],row["unit"],row) == "INCOMPLETE"


@pytest.mark.parametrize("overrides", [{"modules":10**500}, {"voc_at_25":1e308,"modules":100},
                                       {"coefficient_per_c":1}])
def test_voltage_arithmetic_fails_closed(overrides):
    args = dict(site="roof",station="local",as_of=10,max_age=5,voc_at_25=50,modules=5,coefficient_per_c=-0.003,reported=250)
    args.update(overrides)
    assert weather_quality(dict(site="roof",station="local",timezone="UTC",cell_temp=0,unit="C",timestamp=10),**args) == "FAIL"


@pytest.mark.parametrize("site", ["roof","yard","hangar"])
def test_weather_location_and_delivery_destination(site):
    weather = dict(site=site,station="s1",timezone="UTC",cell_temp=25,unit="C",timestamp=5)
    def check(data):
        return weather_quality(data,site=site,station="s1",as_of=5,max_age=1,voc_at_25=40,
                               coefficient_per_c=-0.003,modules=2,reported=80)
    assert check(weather) == "PASS"
    assert check(dict(weather,site="other")) == "FAIL"
    assert check(dict(weather,station="other")) == "FAIL"
    assert check(dict(weather,site=None)) == "INCOMPLETE"
    a = dict(destination="primary",drawing=site,revision="one",payload_bytes=b"abc",payload_digest=hashlib.sha256(b"abc").hexdigest())
    b = dict(a,destination="backup")
    assert delivery_quality([a,a,b],[a,b]) == "PASS"
    assert delivery_quality([a,b],[a]) == "FAIL"
    assert delivery_quality([a,b],[a,a]) == "FAIL"


@pytest.mark.parametrize("case", ["roof","frame","duct"])
def test_receiver_bytes_must_match_label(case):
    data = case.encode()
    row = dict(destination="test",drawing=case,revision="v1",payload_bytes=data,
               payload_digest=hashlib.sha256(data).hexdigest())
    assert delivery_quality([row,row],[row]) == "PASS"
    assert delivery_quality([row],[dict(row,payload_bytes=data+b"!")]) == "FAIL"
    assert delivery_quality([row],[dict(row,payload_bytes=None)]) == "INCOMPLETE"


@pytest.mark.parametrize("site", ["roof","shed","office"])
def test_clock_skew_and_latest_revision(site):
    args = dict(site=site,station="s",as_of=100,max_age=5,clock_skew=2,
                voc_at_25=5,modules=1,coefficient_per_c=0,reported=5)
    sample = dict(site=site,station="s",unit="C",cell_temp=25,timestamp=102,timezone="UTC")
    assert weather_quality(sample,**args) == "PASS"
    assert weather_quality(dict(sample,timestamp=103),**args) == "FAIL"
    assert weather_quality(dict(sample,timezone=None),**args) == "INCOMPLETE"
    old = dict(drawing=site,destination="receiver",revision="one",payload_bytes=b"old",payload_digest=hashlib.sha256(b"old").hexdigest())
    new = dict(old,revision="two",payload_bytes=b"new",payload_digest=hashlib.sha256(b"new").hexdigest())
    def check(state):
        return delivery_quality([old,new,old],[new,old],revision_order=["one","two"],final_state=state)
    assert check([new]) == "PASS"
    assert check([old]) == "FAIL"
    assert check(None) == "INCOMPLETE"

@pytest.mark.parametrize("part,source,target,value,converted", [
    ("panel", "kW", "W", 0.425, 425), ("relay", "mA", "A", 125, 0.125),
    ("inverter", "V", "kV", 1200, 1.2),
])
def test_catalog_unit_conversion(part, source, target, value, converted):
    row = dict(part=part, version="r1", unit=source, rating=value)
    out = dict(row, unit=target, rating=converted)
    def check(entry, report):
        return catalog_quality([entry], part, "r1", target, report)
    assert check(row, out) == "PASS"
    assert check(row, dict(out, rating=value)) == "FAIL"
    assert check(dict(row, unit="A" if target != "A" else "W"), out) == "FAIL"
    assert check(dict(row, unit=None), out) == "INCOMPLETE"


@pytest.mark.parametrize("drawing", ["roof", "frame", "duct"])
def test_ack_before_receiver_commit(drawing):
    row = dict(destination="issues", drawing=drawing, revision="1", payload_bytes=b"issue",
               payload_digest=hashlib.sha256(b"issue").hexdigest())
    requests = [dict(row, http_status=202), dict(row, http_status=200)]
    assert delivery_quality(requests, [requests[-1]]) == "INCOMPLETE"
    events = [dict(row, state="accepted"), dict(row, state="pending")]
    assert delivery_quality(requests, events) != "PASS"
    assert delivery_quality(requests, events + [dict(row, state="applied")]) == "PASS"
    assert delivery_quality(requests, [dict(row, state="unknown")]) == "INCOMPLETE"


@pytest.mark.parametrize("temps,coefficient,worst", [
    ([-25, 25, 0], -0.002, 440), ([0, 25, 50], 0.002, 420), ([20, 10, 25], -0.005, 430),
])
def test_series_requires_every_interval(temps, coefficient, worst):
    from benches.x03_weather_oracle import series_voltage_quality
    samples = [dict(site="plant", station="S", timezone="UTC", unit="C", timestamp=t, cell_temp=temp)
               for t, temp in enumerate(temps)]
    def check(rows, voltage):
        return series_voltage_quality(rows, [0, 1, 2], reported=voltage, site="plant", station="S",
                                     as_of=2, max_age=2, modules=10, voc_at_25=40, coefficient_per_c=coefficient)
    assert check(samples[::-1], worst) == "PASS"
    assert check(samples, 400) == "FAIL"
    assert check(samples[1:], worst) == "INCOMPLETE"


@pytest.mark.parametrize("field", ["voltage_V", "current_A", "power_W"])
def test_catalog_constraints_share_one_part(field):
    from benches.x03_catalog_oracle import catalog_selection_quality
    bounds = {"voltage_V": (40, 50), "current_A": (8, 12), "power_W": (350, 500)}
    good = dict(part="P", version="2", ratings=dict(voltage_V=45, current_A=10, power_W=450))
    other = dict(part="Q", version="2", ratings=dict(good["ratings"], **{field: bounds[field][0]}))
    def grade(result):
        return catalog_selection_quality([good, other], "2", bounds, result)
    assert grade(good) == "PASS"
    assert grade(dict(good, ratings=other["ratings"])) == "FAIL"
    assert grade(dict(good, version="1")) == "FAIL"
    assert catalog_selection_quality([good], "2", {field: (0, 1)}, good) == "FAIL"


@pytest.mark.parametrize("parts", [("module", "rack"), ("relay", "fuse", "fan"), ("duct", "tee", "cap", "bend")])
def test_recorded_catalog_cursor_chain(parts):
    from benches.x03_catalog_oracle import paginated_catalog_quality
    pages = [dict(cursor=None if i == 0 else str(i), next=str(i+1) if i+1 < len(parts) else None,
                  rows=[dict(part=part, rating=i+1)], version="v2") for i, part in enumerate(parts)]
    def grade(records):
        return paginated_catalog_quality(records, "v2", list(parts))
    assert grade(pages[::-1]) == "PASS"
    assert grade(pages[:-1]) == "INCOMPLETE"
    assert grade(pages + [pages[-1]]) == "FAIL"
    assert grade([*pages[:-1], dict(pages[-1], next="1")]) == "FAIL"
    assert grade([*pages[:-1], dict(pages[-1], rows=[dict(part=parts[0], rating=99)])]) == "FAIL"
    assert grade([*pages[:-1], dict(pages[-1], version="v1")]) == "FAIL"


@pytest.mark.parametrize("drawing,start,late", [("roof",1,1),("frame",10,9),("duct",4,3)])
def test_deleted_drawing_requires_explicit_recreation(drawing,start,late):
    from benches.x03_issue_delivery_oracle import tombstone_replay_quality
    def event(rev, action):
        return dict(drawing=drawing, revision=rev, action=action)
    events = [event(start,"upsert"), event(start+1,"delete"), event(late,"upsert"),
              event(start+2,"upsert"), event(start+3,"recreate")]
    snapshots = [[drawing], [], [], [], [drawing]]
    grants = {(drawing,start+3)}
    assert tombstone_replay_quality(events, grants, snapshots) == "PASS"
    assert tombstone_replay_quality(events, set(), snapshots) == "FAIL"
    assert tombstone_replay_quality(events, grants, [*snapshots[:2], [drawing], *snapshots[3:]]) == "FAIL"
    assert tombstone_replay_quality(events, None, snapshots) == "INCOMPLETE"


@pytest.mark.parametrize("selected, status, successor, rating, approval, expected", [
    ("old", "active", None, 10, True, "FAIL"),
    ("new", "withdrawn", "old", 10, True, "FAIL"),
    ("new", "active", None, 15, True, "FAIL"),
    ("new", "active", None, 10, False, "FAIL"),
    ("new", "active", None, None, True, "INCOMPLETE"),
    ("new", "active", None, 10, True, "PASS"),
], ids=["reject-withdrawn-part", "resolve-cycle", "check-replacement-capacity",
        "missing-approval", "missing-rating", "approved-compatible-successor"])
def test_substitution(selected, status, successor, rating, approval, expected):
    from benches.x03_catalog_oracle import replacement_quality
    parts = {"old": dict(status="withdrawn", replacement="new"),
             "new": dict(status=status, replacement=successor, ratings={"amps": rating})}
    approvals = [["old", "new"], ["new", "old"]] if approval else []
    assert replacement_quality(parts, "old", selected, approvals, {"amps": (9, 12)}) == expected


@pytest.mark.parametrize("scenario, expected", [
    ("partial-retry", "PASS"), ("omitted", "INCOMPLETE"), ("wrong-revision", "FAIL"),
    ("duplicate-effect", "FAIL"), ("pending", "INCOMPLETE"),
])
def test_batch_item_delivery(scenario, expected):
    from benches.x03_issue_delivery_oracle import batch_ack_quality
    items = [dict(item=k, revision="2") for k in ("rack", "panel")]
    acks = [dict(items[1], state="pending"), *(dict(item, state="accepted") for item in items)]
    effects = [dict(item, state="committed") for item in items]
    if scenario == "omitted":
        effects.pop()
    if scenario == "pending":
        acks.pop()
    if scenario == "wrong-revision":
        effects[1]["revision"] = "1"
    if scenario == "duplicate-effect":
        effects.append(effects[1].copy())
    assert batch_ack_quality(items, acks, effects) == expected


@pytest.mark.parametrize("samples, step, expected", [
    ([0, 10, 10, 30], 10, "FAIL"), ([0, 9, 20, 30], 10, "FAIL"),
    ([30, 10, 0, 20], 10, "PASS"), ([0, 10, 20, 30], None, "INCOMPLETE"),
], ids=["flag-duplicate-temperature", "find-irregular-voltage-window", "reconcile-reordered-samples", "missing-policy"])
def test_voltage_series_cadence(samples, step, expected):
    from benches.x03_weather_oracle import weather_cadence_quality
    assert weather_cadence_quality(samples, 0, 30, step) == expected


@pytest.mark.parametrize("time, version, overlap, expected", [
    (10, "new", False, "PASS"), (10, "old", False, "FAIL"),
    (9, "new", False, "FAIL"), (10, "new", True, "FAIL"),
], ids=["use-replacement-on-effective-date", "reject-expired-part", "reject-future-part", "flag-overlapping-ratings"])
def test_design_time_ratings(time, version, overlap, expected):
    from benches.x03_catalog_oracle import effective_catalog_quality
    part = dict(part="module", unit="W")
    rows = [dict(part, version="old", rating=400, valid_from=0, valid_until=11 if overlap else 10),
            dict(part, version="new", rating=420, valid_from=10, valid_until=20)]
    result = dict(part, version=version, rating=420 if version == "new" else 400)
    assert effective_catalog_quality(rows, time, "module", version, "W", result) == expected


@pytest.mark.parametrize("links, final, expected", [
    ([("alpha", "zeta"), ("zeta", None)], "alpha", "PASS"),
    ([("red", None), ("blue", "red"), ("green", "red")], "blue", "FAIL"),
    ([("final", "draft")], "final", "INCOMPLETE"),
    ([("zeta", None), ("alpha", "zeta")], "zeta", "FAIL"),
], ids=["follow-nonnumeric-labels", "reject-fork", "require-predecessor", "reject-stale-final"])
def test_delivery_revision_chain(links, final, expected):
    from benches.x03_issue_delivery_oracle import predecessor_quality
    events = [dict(revision=child, predecessor=parent) for child, parent in links]
    assert predecessor_quality(events, final) == expected


@pytest.mark.parametrize("bounds, coefficient, limit, expected", [
    ((5, 35), -0.005, 105, "FAIL"), ((15, 45), 0.005, 105, "FAIL"),
    ((5, 35), -0.005, 110, "PASS"), (None, -0.005, 105, "INCOMPLETE"),
], ids=["check-cold-endpoint-at-110V", "check-hot-endpoint-at-110V", "check-exact-110V-limit", "missing-interval"])
def test_temperature_interval(bounds, coefficient, limit, expected):
    from benches.x03_weather_oracle import uncertain_voltage_quality
    weather = dict(unit="C", cell_temp=25, timestamp=10, site="roof", station="local", timezone="UTC")
    assert uncertain_voltage_quality(weather, bounds, limit, site="roof", station="local", as_of=10,
                                     max_age=0, voc_at_25=50, coefficient_per_c=coefficient, modules=2) == expected


@pytest.mark.parametrize("sequence,expected", [
    (["old","commit","new"], "PASS"), (["old","mixed","commit","new"], "FAIL"),
    (["old"], "INCOMPLETE"), (["old","commit"], "INCOMPLETE"), (["new","commit","new"], "FAIL"),
], ids=["resume-and-commit","torn-read-before-commit","interrupted-before-marker",
        "interrupted-before-readback","premature-publication"])
def test_atomic_artifact_visibility(sequence, expected):
    from benches.x03_issue_delivery_oracle import atomic_batch_quality
    old = {"panels": "1"*64, "report": "2"*64}
    new = {"panels": "3"*64, "report": "4"*64}
    events = {"old": dict(kind="snapshot", revision="old", members=old),
              "new": dict(kind="snapshot", revision="new", members=new),
              "mixed": dict(kind="snapshot", revision="old", members=dict(old, panels=new["panels"])),
              "commit": dict(kind="commit", revision="new")}
    assert atomic_batch_quality({"old": old, "new": new}, "old", "new", [events[s] for s in sequence]) == expected


@pytest.mark.parametrize("temperature,duty,expected", [
    (20, 100, "PASS"), (40, 80, "PASS"), (40, 81, "FAIL"), (61, 20, "FAIL"),
], ids=["nominal-knot", "exact-interpolated-rating", "derated-overload", "outside-envelope"])
def test_catalog_derating(temperature, duty, expected):
    from benches.x03_catalog_oracle import temperature_derating_quality
    curve = [(20,100), (60,60)]
    assert temperature_derating_quality(curve, temperature, duty, "linear-no-extrapolation") == expected
    assert temperature_derating_quality(curve, temperature, duty, None) == "INCOMPLETE"


@pytest.mark.parametrize("timestamp,reported,interp,extra,gap,expected", [
    (5, 100, "linear", "none", 10, "PASS"), (5, 100, "linear", "none", 9, "FAIL"),
    (12, 90, "linear", "none", 10, "FAIL"), (12, 90, "none", "hold", 2, "PASS"),
    (5, 100, None, "none", 10, "INCOMPLETE"), (5, 110, "linear", "none", 10, "FAIL"),
], ids=["midpoint-voltage", "gap-too-wide", "forbid-extrapolation", "bounded-hold", "missing-policy", "wrong-voltage"])
def test_voltage_fill_policy(timestamp, reported, interp, extra, gap, expected):
    from benches.x03_weather_oracle import filled_voltage_quality
    base = dict(site="s", station="roof", unit="C", timezone="UTC")
    samples = [dict(base, timestamp=10, cell_temp=35), dict(base, timestamp=0, cell_temp=15)]
    assert filled_voltage_quality(samples, timestamp, reported, interpolation=interp, extrapolation=extra,
                                  max_gap=gap, site="s", station="roof", as_of=12, max_age=12,
                                  modules=2, voc_at_25=50, coefficient_per_c=-0.01) == expected


@pytest.mark.parametrize("available,ids,version,expected", [
    (5, ["r1","r2"], "v1", "PASS"), (4, ["r1","r2"], "v1", "FAIL"),
    (5, ["r1","r1"], "v1", "FAIL"), (5, ["r1","r2"], "v2", "FAIL"),
], ids=["split-stock-between-rows","overallocate-one-lot","duplicate-reservation","reject-stale-inventory"])
def test_quantity_allocations(available, ids, version, expected):
    from benches.x03_catalog_oracle import stock_allocation_quality
    stock = dict(version="v1", lots={"warehouse":dict(part="panel", available=available)})
    demand = {"east":dict(part="panel",required=2),"west":dict(part="panel",required=3)}
    rows = [dict(id=i, line=k, lot="warehouse", quantity=v["required"]) for i,(k,v) in zip(ids,demand.items())]
    assert stock_allocation_quality(version, stock, demand, rows) == expected


@pytest.mark.parametrize("case,expected", [
    ("ignore-old","PASS"), ("credit-old","FAIL"), ("retry-new","PASS"), ("missing-generation","INCOMPLETE"),
], ids=["ignore-delayed-generation","reject-cross-generation-credit","dedupe-recreated-object","missing-identity"])
def test_recreation_receipt_sequence(case, expected):
    from benches.x03_issue_delivery_oracle import generation_receipt_quality
    def event(kind, generation, **kwargs):
        return dict(kind=kind, drawing="shared-id", generation=generation, **kwargs)
    events = [event("create","a"),event("receipt","a",effect="export",credited=True),
              event("delete","a"),event("create","b"),
              event("receipt","a",effect="late",credited=case=="credit-old"),
              event("receipt","b",effect="export",credited=True)]
    if case == "retry-new":
        events.append(event("receipt","b", effect="export", credited=False))
    if case == "missing-generation":
        del events[-1]["generation"]
    assert generation_receipt_quality(events) == expected


@pytest.mark.parametrize("case,expected", [
    ("ok", "PASS"), ("mating","FAIL"), ("protocol","FAIL"), ("unrecorded","INCOMPLETE"), ("stale","FAIL"),
], ids=["sensor-hub-controller","second-link-mate-mismatch","protocol-mismatch","missing-port-inventory","wrong-version"])
def test_assembly_interfaces(case, expected):
    from benches.x03_catalog_oracle import assembly_interface_quality
    plug = dict(connector="plug",mates=["socket"],protocols=["v2"])
    socket = dict(connector="socket",mates=["plug"],protocols=["v2"])
    target = dict(socket)
    if case == "mating":
        target["mates"] = ["other"]
    if case == "protocol":
        target["protocols"] = ["v1"]
    if case == "unrecorded":
        del target["protocols"]
    catalog = dict(version="7",parts={"sensor":{"out":plug},"hub":{"in":socket,"out":plug},"control":{"in":target}})
    links = [{"from":[a,"out"],"to":[b,"in"],"protocol":"v2"} for a,b in (("sensor","hub"),("hub","control"))]
    assert assembly_interface_quality("6" if case=="stale" else "7", catalog, links) == expected


@pytest.mark.parametrize("sent,budget,delay,expected", [
    (12,1,10,"PASS"), (11,1,10,"FAIL"), (12,0,10,"FAIL"),
    (12,None,10,"INCOMPLETE"), (12,1,None,"INCOMPLETE"),
], ids=["at-retry-boundary","early-from-response","budget-exhausted","missing-budget","missing-retry-after"])
def test_retry_schedule(sent, budget, delay, expected):
    from benches.x03_issue_delivery_oracle import retry_schedule_quality
    rows = [dict(sent=0,received=2,status=429,retry_after_seconds=delay), dict(sent=sent,received=sent+1,status=200)]
    assert retry_schedule_quality(rows,budget) == expected


@pytest.mark.parametrize("flag,allowed,expected", [
    ("invalid",["good"],"FAIL"), ("suspect",["good"],"FAIL"), ("suspect",["good","suspect"],"PASS"),
    ("good",["good"],"PASS"), ("good",None,"INCOMPLETE"),
], ids=["fresh-invalid","suspect-disallowed","suspect-explicitly-approved","approved-reading","missing-policy"])
def test_weather_flag_voltage(flag, allowed, expected):
    from benches.x03_weather_oracle import flagged_voltage_quality
    sample = dict(timestamp=5,unit="C",cell_temp=25,timezone="UTC",site="roof",station="north",quality_flag=flag)
    assert flagged_voltage_quality(sample,allowed,reported=100,site="roof",station="north",as_of=5,max_age=1,
                                  voc_at_25=50,modules=2,coefficient_per_c=-0.004) == expected


@pytest.mark.parametrize("at,duplicate,policy,expected", [
    (10,False,True,"FAIL"), (20,False,True,"PASS"), (9,False,True,"PASS"),
    (12,True,True,"FAIL"), (20,False,False,"INCOMPLETE"),
], ids=["active-hold-shortage","expired-at-boundary","future-hold","repeated-reservation","missing-expiry-policy"])
def test_expiring_stock(at, duplicate, policy, expected):
    from benches.x03_catalog_oracle import reserved_stock_quality
    inventory = dict(version="v2", lots={"rack":dict(part="module",available=10)})
    hold = dict(id="h",lot="rack",quantity=3,start=10,expiry=20)
    holds = [hold, hold.copy()] if duplicate else [hold]
    quantity = 1 if duplicate else 8
    assert reserved_stock_quality("v2", inventory, {"roof":dict(part="module",required=quantity)},
                                  [dict(id="new",lot="rack",line="roof",quantity=quantity)], holds, at,
                                  "start-inclusive-expiry-exclusive" if policy else None) == expected
    assert inventory["lots"]["rack"]["available"] == 10


@pytest.mark.parametrize("lot,serial,revision,expected", [
    ("A",10,"r2","FAIL"), ("A",21,"r2","PASS"), ("B",15,"r2","PASS"),
    ("A",20,"r2","FAIL"), ("A",21,"r1","FAIL"), (None,12,"r2","INCOMPLETE"),
], ids=["first-recalled-serial","adjacent-clear-serial","same-serial-different-lot","last-recalled-unit","stale-bulletin","missing-lot"])
def test_serial_recall(lot, serial, revision, expected):
    from benches.x03_catalog_oracle import lot_recall_quality
    bulletin = dict(revision="r2",ranges=[dict(part="module",lot="A",first=10,last=20)])
    assert lot_recall_quality([dict(part="module",lot=lot,serial=serial)],bulletin,revision) == expected


@pytest.mark.parametrize("changed", ["payload_digest", "destination", "revision"])
def test_idempotency_namespace_collisions(changed):
    from benches.x03_issue_delivery_oracle import idempotency_collision_quality
    row = dict(key="reserved",payload_digest="digest-a",destination="inbox-a",revision="r1",
               outcome="committed",receipt="ack",new_effects=1)
    conflict = dict(row, outcome="conflict",receipt=None,new_effects=0)
    conflict[changed] = "other"
    replay = dict(row,outcome="replayed",new_effects=0)
    assert idempotency_collision_quality([row,conflict,replay]) == "PASS"
    assert idempotency_collision_quality([row,dict(conflict,receipt="ack")]) == "FAIL"
    assert idempotency_collision_quality([row,dict(conflict,outcome="replayed",receipt="ack")]) == "FAIL"
    assert idempotency_collision_quality([row,dict(conflict,new_effects=1)]) == "FAIL"


@pytest.mark.parametrize("raw,offset,corrected", [(11, 1, 12), (24.5, -0.5, 24), (0, 0.25, 0.25)])
def test_voltage_uses_pinned_valid_calibration(raw, offset, corrected):
    from benches.x03_weather_oracle import calibrated_voltage_quality
    record = dict(sensor="meter", revision="v2", start=2, end=10, offset_volts=offset)
    records = [dict(record, revision="v1", offset_volts=3), record]
    def check(at, rows, output):
        return calibrated_voltage_quality("meter", "v2", at, raw, rows, output)
    assert check(2, records, corrected) == "PASS"
    assert check(9, records, raw) == "FAIL"
    assert check(10, records, corrected) == "FAIL"
    assert check(9, records[:1], corrected) == "INCOMPLETE"


@pytest.mark.parametrize("lead,deadline,expected", [(1, 5, "FAIL"), (2, 9, "PASS"), (0, 4, "PASS")])
def test_procurement_delivery_deadline(lead, deadline, expected):
    from datetime import date
    from benches.x03_catalog_oracle import procurement_deadline_quality as grade
    days = [date(2026, 12, d) for d in (4, 8, 8, 9)]
    args = (8, 8, lead, days[0], date(2026, 12, deadline))
    assert grade(*args, days) == expected
    assert grade(7, *args[1:], days) == "FAIL"
    assert grade(*args, None) == "INCOMPLETE"


@pytest.mark.parametrize("model,location,mount", [("A","outdoor","roof"), ("B","indoor","wall"), ("C","marine","deck")])
def test_certified_installation_scope(model, location, mount):
    from benches.x03_catalog_oracle import certification_scope_quality as grade
    conditions = dict(location=location, mount=mount)
    record = dict(revision="r3", scopes=[dict(model=model, conditions=dict(location=[location], mount=[mount]))])
    assert grade(record, "r3", model, conditions) == "PASS"
    assert grade(record, "r3", "different", conditions) == "FAIL"
    assert grade(record, "r3", model, dict(conditions, mount="pole")) == "FAIL"
    assert grade(dict(revision="r3"), "r3", model, conditions) == "INCOMPLETE"


@pytest.mark.parametrize("states,station,temp,volts", [
    ([True, True, True], "local", 25, 100), ([False, True, True], "ridge", 15, 110),
    ([False, False, True], "valley", 35, 90)])
def test_voltage_station_priority(states, station, temp, volts):
    from benches.x03_weather_oracle import failover_voltage_quality as grade
    priority = ["local", "ridge", "valley"]
    state = dict(zip(priority, states))
    sample = dict(station=station, site="roof", unit="C", timezone="UTC", cell_temp=temp, timestamp=10)
    kwargs = dict(site="roof", reported=volts, as_of=10, max_age=1, voc_at_25=50, modules=2, coefficient_per_c=-0.01)
    assert grade(priority, state, sample, **kwargs) == "PASS"
    assert grade(priority, state, dict(sample, station="unapproved"), **kwargs) == "FAIL"
    if station != "valley":
        assert grade(priority, state, dict(sample, station="valley"), **kwargs) == "FAIL"
    assert grade(None, state, sample, **kwargs) == "INCOMPLETE"


@pytest.mark.parametrize("action,effective,at,expected", [("revoke", 9, 8, "PASS"), ("revoke", 9, 9, "FAIL"),
                                                          ("supersede", 7, 10, "FAIL")])
def test_adjudicated_acknowledgement(action, effective, at, expected):
    from benches.x03_issue_delivery_oracle import revocation_quality as grade
    event = dict(receipt="ack", action=action, effective_at=effective, replacement="ack2")
    history = dict(complete_through=10, events=[event])
    assert grade("ack", 5, at, history) == expected
    assert grade("other", 5, at, history) == "PASS"
    assert grade("ack", 5, at, dict(history, complete_through=at-1)) == "INCOMPLETE"
    assert grade("ack", 5, at, None) == "INCOMPLETE"


@pytest.mark.parametrize("weights,volts", [([0.5,0.5],100), ([0.25,0.75],95), ([0.75,0.25],105)])
def test_voltage_from_weighted_station_temperatures(weights, volts):
    from benches.x03_weather_oracle import spatial_voltage_quality as grade
    samples = {s:dict(station=s, site="roof", unit="C", timezone="UTC", timestamp=5, cell_temp=t)
               for s,t in [("north",15),("south",35)]}
    conditions = dict(reported=volts, site="roof", as_of=5, max_age=1, voc_at_25=50, modules=2, coefficient_per_c=-0.01)
    policy = dict(zip(samples, weights))
    assert grade(samples, policy, **conditions) == "PASS"
    assert grade(samples, dict(north=-0.1, south=1.1), **conditions) == "FAIL"
    assert grade(samples, dict(north=1, south=1), **conditions) == "FAIL"
    assert grade({"north":samples["north"]}, policy, **conditions) == "INCOMPLETE"


@pytest.mark.parametrize("air,rh,surface,risk", [(20,50,9,True), (20,50,10,False), (12,100,12,True)])
def test_condensation_at_recorded_surface(air,rh,surface,risk):
    from benches.x03_weather_oracle import condensation_quality as grade
    # At 20 C and 50% RH the declared rule gives a dew point of about 9.26 C.
    rule = dict(method="magnus-water",a=17.62,b_c=243.12)
    assert grade(air,rh,surface,rule,risk) == "PASS"
    assert grade(air,rh,surface,rule,not risk) == "FAIL"
    assert grade(air,None,surface,rule,risk) == "INCOMPLETE"


@pytest.mark.parametrize("primary,condition,slot,part", [("panel","roof","mount","clip"),("drive","wet","seal","gland"),
                                                        ("rack","tall","brace","strut")])
def test_conditional_assembly_accessories(primary,condition,slot,part):
    from benches.x03_catalog_oracle import accessory_quality as grade
    rules = dict(revision="r1",assemblies={primary:[dict(when={"use":condition},slot=slot,compatible=[part])]})
    context = {"use":condition}
    assert grade(rules,"r1",primary,context,{slot:part}) == "PASS"
    assert grade(rules,"r1",primary,{"use":"other"},{}) == "PASS"
    assert grade(rules,"r1",primary,context,{}) == "FAIL"
    assert grade(rules,"r1",primary,context,{slot:"incompatible"}) == "FAIL"
    assert grade(rules,"r1",primary,{}, {}) == "INCOMPLETE"


@pytest.mark.parametrize("time,status", [(9,"replayed"), (10,"refused"), (20,"reconciled")])
def test_retry_before_at_and_after_key_expiry(time,status):
    from benches.x03_issue_delivery_oracle import retention_retry_quality as grade
    first = dict(delivery="job",key="K",time=0,expires=10,new_effects=1,status="committed")
    retry = dict(first,time=time,new_effects=0,status=status)
    assert grade([first,retry],10) == "PASS"
    assert grade([first,dict(retry,new_effects=1,status="committed")],10) == "FAIL"
    assert grade([first,retry],None) == "INCOMPLETE"


@pytest.mark.parametrize("days,inclusive,install,expected", [(2,True,2,"PASS"),(2,False,2,"FAIL"),(1,True,2,"FAIL")])
def test_stock_life_at_install(days,inclusive,install,expected):
    from datetime import date,timedelta
    from benches.x03_catalog_oracle import shelf_life_quality as grade
    made = date(2024,2,28)
    rule = dict(revision="r2",life_days=days,expiry_day_usable=inclusive)
    assert grade(made,made+timedelta(days=install),rule,"r2") == expected
    assert grade(made,made,rule,"other") == "FAIL"
    assert grade(made,made,dict(rule,expiry_day_usable=None),"r2") == "INCOMPLETE"


@pytest.mark.parametrize("old,new,worker", [(1,2,"B"),(5,9,"A"),(20,30,"C")])
def test_fence_after_worker_reassignment(old,new,worker):
    from benches.x03_issue_delivery_oracle import fencing_quality as grade
    original = dict(seq=1,kind="grant",resource="job",worker="A",epoch=old)
    granted = dict(original,seq=2,worker=worker,epoch=new)
    stale = dict(original,seq=3,kind="receive",committed=False)
    current = dict(granted,seq=4,kind="receive",committed=True)
    assert grade([original,granted,stale,current]) == "PASS"
    assert grade([original,granted,dict(stale,committed=True)]) == "FAIL"
    assert grade([original,dict(granted,epoch=old),current]) == "FAIL"
    assert grade([original,granted,dict(current,epoch=None)]) == "INCOMPLETE"


@pytest.mark.parametrize("a,b,exposure,allowed", [("Al","Cu","wet","sleeve"),("Zn","Cu","marine","barrier"),
                                                  ("steel","Al","rain","washer")])
def test_exposed_material_pair_requires_pinned_isolation(a,b,exposure,allowed):
    from benches.x03_catalog_oracle import galvanic_quality as grade
    catalog = dict(revision="sheet-1",rules=[dict(materials=[b,a],exposure=exposure,allowed_isolation=[allowed])])
    assert grade(a,b,exposure,allowed,catalog,"sheet-1") == "PASS"
    assert grade(a,b,exposure,"bare",catalog,"sheet-1") == "FAIL"
    assert grade(None,b,exposure,allowed,catalog,"sheet-1") == "INCOMPLETE"


@pytest.mark.parametrize("source,target,factor,expected", [(3,600,0.75,15),(600,3,1.4,28),(60,60,1,20)])
def test_only_declared_wind_period_conversions(source,target,factor,expected):
    from benches.x03_weather_oracle import wind_period_quality as grade
    rules = {(source,target):factor}
    assert grade(20,source,target,rules,expected) == "PASS"
    assert grade(20,source,target,rules,expected+1) == "FAIL"
    assert grade(20,3,600,{},15) == "FAIL"
    assert grade(20,None,target,rules,expected) == "INCOMPLETE"


@pytest.mark.parametrize("parts", [("rail", "adapter"), ("guard", "lid"), ("clamp", "spacer", "insert")])
def test_procure_compatible_accessories(parts):
    from itertools import combinations
    from benches.x03_catalog_oracle import accessory_compatibility_quality
    selected = set(parts)
    compatibility = {frozenset(pair): True for pair in combinations(parts, 2)}
    assembly = dict(revision="kit-v3", eligible=selected, compatible=compatibility)
    def grade():
        return accessory_compatibility_quality("kit-v3", assembly, selected)
    assert grade() == "PASS"
    pair = frozenset(parts[:2])
    compatibility[pair] = False
    assert grade() == "FAIL"
    del compatibility[pair]
    assert grade() == "INCOMPLETE"


@pytest.mark.parametrize("unit,value,impossible", [("C", -10, -274), ("F", 14, -460), ("K", 263.15, -1)])
def test_cold_string_voltage_across_weather_units(unit, value, impossible):
    from benches.x03_weather_oracle import temperature_unit_voltage_quality
    sample = dict(site="roof", station="mast", timezone="UTC", timestamp=1, unit=unit, cell_temp=value)
    def grade(record):
        return temperature_unit_voltage_quality(record, reported=108.75, site="roof", station="mast",
                                               as_of=1, max_age=0, voc_at_25=50, coefficient_per_c=-0.0025, modules=2)
    assert grade(sample) == "PASS"
    assert grade(dict(sample, cell_temp=impossible)) == "FAIL"
    assert grade(dict(sample, unit=None)) == "INCOMPLETE"


@pytest.mark.parametrize("assembly,opening,insert,tol,clearance", [
    ("gland", 12, 10, 0.5, 1), ("lug", 8, 7, 0.25, 0.5), ("frame", 60, 55, 2, 1),
])
def test_worst_case_mating_clearance(assembly, opening, insert, tol, clearance):
    from benches.x03_catalog_oracle import mating_clearance_quality
    record = dict(revision=assembly, opening_mm=opening, insert_mm=insert,
                  opening_minus_mm=tol, insert_plus_mm=tol)
    assert mating_clearance_quality(assembly, record, clearance) == "PASS"
    assert mating_clearance_quality(assembly, record, clearance+0.1) == "FAIL"
    del record["opening_minus_mm"]
    assert mating_clearance_quality(assembly, record, clearance) == "INCOMPLETE"


@pytest.mark.parametrize("ambient,irradiance,voltage,ambient_only", [
    (25, 500, 98, 100), (15, 1000, 98, 102), (5, 250, 103, 104),
])
def test_voltage_uses_irradiance_heated_cell(ambient, irradiance, voltage, ambient_only):
    from benches.x03_weather_oracle import irradiance_voltage_quality
    sample = dict(site="roof", station="mast", timezone="UTC", timestamp=1, ambient_c=ambient)
    model = dict(method="linear-rise", rise_c_per_w_m2=0.02)
    def grade(reported, rule=model):
        return irradiance_voltage_quality(sample, irradiance, rule, reported=reported, site="roof",
                                        station="mast", as_of=1, max_age=0, voc_at_25=50,
                                        coefficient_per_c=-0.002, modules=2)
    assert grade(voltage) == "PASS"
    assert grade(ambient_only) == "FAIL"
    assert grade(voltage, {}) == "INCOMPLETE"


@pytest.mark.parametrize("material,agent,pct,temp", [("liner","wash",10,20), ("seal","cleaner",2,40), ("housing","brine",5,35)])
def test_exposure_grade_at_recorded_conditions(material, agent, pct, temp):
    from benches.x03_catalog_oracle import chemical_exposure_quality as grade
    catalog = dict(revision="v2", exposures={(material,agent): [(0,pct,0,temp,True), (0,pct,temp+1,80,False)]})
    assert grade("v2",catalog,material,agent,pct,temp) == "PASS"
    assert grade("v2",catalog,material,agent,pct,temp+1) == "FAIL"
    assert grade("v2",catalog,material,agent,pct+1,temp) == "INCOMPLETE"


@pytest.mark.parametrize("first,last,split,mean", [(0,10,2,8), (80,40,7,68), (-5,15,1,13)])
def test_time_weighted_site_readings(first, last, split, mean):
    from benches.x03_weather_oracle import interval_weather_quality
    def grade(rows, value):
        return interval_weather_quality((0,10), rows, "duration-weighted-mean", value)
    rows = [(split,10,last), (0,split,first)]
    assert grade(rows, mean) == "PASS"
    assert grade(rows, (first+last)/2) == "FAIL"
    for offset in (-0.5,0.5):
        assert grade([(0,split,first),(split+offset,10,last)], mean) == "INCOMPLETE"


@pytest.mark.parametrize("stamp,near,far,skew", [(10,11,20,1), (20,18,29,2), (1.2,1.3,2,0.1)])
def test_pair_voltage_with_nearby_temperature_not_unrelated_extreme(stamp,near,far,skew):
    from benches.x03_weather_oracle import voltage_temperature_alignment_quality as grade
    temp = {near:20,far:-15}
    assert grade({stamp:47},temp,skew,{stamp:near}) == "PASS"
    assert grade({stamp:47},temp,skew,{stamp:far}) == "FAIL"
    assert grade({stamp:47},{far:-15},skew,{stamp:far}) == "INCOMPLETE"


@pytest.mark.parametrize("part,rating,opening,history,remaining", [
    ("plug",100,70,[(1,12),(2,8)],10), ("coupler",50,40,[(1,9)],1), ("socket",200,150,[(1,30),(2,20)],0),
])
def test_remaining_connector_cycles(part,rating,opening,history,remaining):
    from benches.x03_catalog_oracle import mating_life_quality as grade
    catalog = dict(revision="v1",cycle_ratings={part:rating})
    assert grade("v1",catalog,part,opening,history,remaining) == "PASS"
    assert grade("v1",catalog,part,opening,history,remaining+1) == "FAIL"
    assert grade("v1",catalog,part,opening,None,remaining) == "INCOMPLETE"


@pytest.mark.parametrize("sensor,target,speed,alpha,expected", [(10,40,5,0.5,10), (80,5,8,0.25,4), (3,24,6,1/3,12)])
def test_adjust_wind_reading_for_terrain_and_height(sensor,target,speed,alpha,expected):
    from benches.x03_weather_oracle import wind_height_quality as grade
    model = dict(revision="terrain",model="power-law",exponent=alpha)
    assert grade("terrain",model,sensor,target,speed,expected) == "PASS"
    assert grade("terrain",model,sensor,target,speed,speed) == "FAIL"
    assert grade("terrain",dict(model,exponent=None),sensor,target,speed,expected) == "INCOMPLETE"


@pytest.mark.parametrize("mount,low,mid,high,limit", [("rail",10,20,30,2),("wall",15,25,45,3),("frame",2,10,18,1.5)])
def test_vibration_across_operating_band(mount,low,mid,high,limit):
    from benches.x03_catalog_oracle import vibration_qualification_quality as grade
    sheet = dict(revision="test",complete_mount_list=True,mounts={mount:[(low,mid,limit+1),(mid,high,limit)]})
    assert grade("test",sheet,mount,(low,high),limit) == "PASS"
    assert grade("test",sheet,mount,(low,high),limit+0.1) == "FAIL"
    assert grade("test",sheet,"other",(low,high),limit) == "FAIL"
    assert grade("test",sheet,mount,(low,high+1),limit) == "INCOMPLETE"


@pytest.mark.parametrize("rows,direction,arithmetic", [
    ([(350,1),(10,1)],0,180), ([(359,2),(1,2),(0,1)],0,144), ([(270,1),(0,1)],315,135),
])
def test_weighted_wind_direction_crosses_north(rows,direction,arithmetic):
    from benches.x03_weather_oracle import circular_wind_quality as grade
    def check(samples, result):
        return grade(samples,"weighted-vector-mean",result,0.0001)
    assert check(rows,direction) == "PASS"
    assert check(rows,arithmetic) == "FAIL"
    assert check([(90,1),(270,1)],direction) == "INCOMPLETE"
    assert check([(0,2),(90,1)],26.565051) == "PASS"


@pytest.mark.parametrize("part,exposures,residual", [
    ("rail",[("salt",10)],3), ("rack",[("sheltered",10),("salt",5)],3.5),
    ("liner",[("sheltered",20)],4),
])
def test_residual_material_after_recorded_exposure(part,exposures,residual):
    from benches.x03_catalog_oracle import corrosion_allowance_quality as grade
    sheet = dict(revision="corrosion",loss_mm_per_year={(part,"salt"):0.2,(part,"sheltered"):0.05})
    assert grade("corrosion",sheet,part,exposures,5,residual) == "PASS"
    assert grade("corrosion",sheet,part,exposures,5,residual+0.01) == "FAIL"
    assert grade("corrosion",sheet,part,[("unknown",1)],5,residual) == "INCOMPLETE"


@pytest.mark.parametrize("pressure,kelvin,speed,dynamic", [(86100,300,10,50),(71750,250,20,200),(86100,250,5,15)])
def test_wind_pressure_at_recorded_density(pressure,kelvin,speed,dynamic):
    from benches.x03_weather_oracle import wind_dynamic_pressure_quality as grade
    model = dict(method="dry-ideal-gas",specific_r_j_kg_k=287)
    assert grade(pressure,kelvin,speed,model,dynamic) == "PASS"
    assert grade(pressure,kelvin,speed,model,1.225*speed**2/2) == "FAIL"
    assert grade(pressure,None,speed,model,dynamic) == "INCOMPLETE"


@pytest.mark.parametrize("zenith,sun,tilt,face,expected", [(60,0,0,90,500), (0,0,90,0,140), (60,180,90,0,100)])
def test_isotropic_plane_irradiance(zenith,sun,tilt,face,expected):
    from benches.x03_weather_oracle import plane_irradiance_quality as grade
    model = dict(kind="isotropic",revision="iso1",ground_albedo=0.2)
    def check(dni,reported):
        return grade("iso1",model,dni,100,zenith,sun,tilt,face,reported)
    assert check(800,expected) == "PASS"
    assert check(800,expected+1) == "FAIL"
    assert check(None,expected) == "INCOMPLETE"


@pytest.mark.parametrize("duty,maximum", [(25,9),(50,8),(75,7)])
def test_component_operates_within_joint_pressure_temperature_limit(duty,maximum):
    from benches.x03_catalog_oracle import duty_pressure_quality as grade
    curve = dict(revision="pt",celsius_max_bar=[(0,10),(50,8),(100,6)])
    assert grade("pt",curve,[(0,10),(duty,maximum)]) == "PASS"
    assert grade("pt",curve,[(duty,maximum+0.5)]) == "FAIL"
    assert grade("pt",{},[(duty,maximum)]) == "INCOMPLETE"


@pytest.mark.parametrize("package,shape,duration,peak", [
    ("crate","half-sine",10,20), ("foam","sawtooth",5,30), ("pallet","trapezoid",15,12),
])
def test_shipping_shock_at_recorded_packaging_and_pulse(package,shape,duration,peak):
    from benches.x03_catalog_oracle import transport_shock_quality as grade
    sheet = dict(revision="q",exhaustive=True,ratings={(package,shape):[(duration-1,duration+1,peak)]})
    assert grade("q",sheet,package,shape,duration,peak) == "PASS"
    assert grade("q",sheet,package,shape,duration,peak+1) == "FAIL"
    assert grade("q",sheet,package,shape,duration+2,peak) == "FAIL"
    assert grade("q",sheet,"unpacked",shape,duration,peak) == "FAIL"
    assert grade("q",dict(sheet,ratings={}),package,shape,None,peak) == "INCOMPLETE"
    assert grade("q",dict(sheet,exhaustive=False,ratings={}),package,shape,duration,peak) == "INCOMPLETE"




@pytest.mark.parametrize("powers,radius,expected", [
    ([80],1,69.008),([80,80],1,72.018),([80,70],2,63.401),
])
def test_receiver_sound_budget(powers,radius,expected):
    from benches.x03_catalog_oracle import acoustic_quality as grade
    rows = [("r1",p,radius) for p in powers]
    args = (rows,"r1","free-field-incoherent-v1")
    assert grade(*args,expected,expected+1) == "PASS"
    assert grade(*args,expected,expected-1) == "FAIL"
    assert grade(*args,sum(powers),200) == "FAIL"
    assert grade(rows,"r2",args[2],expected,200) == "INCOMPLETE"


@pytest.mark.parametrize("initial,rain,warm,expected", [(0,6,1,4),(5,2,2,3),(1,1,3,0)])
def test_retained_freezing_rain(initial,rain,warm,expected):
    from benches.x03_weather_oracle import ice_history_quality as grade
    rows = [(0,1,"freezing-rain",-2,rain),(1,3,"dry",warm,0)]
    model = "freezing-rain-degree-hour-v1"
    assert grade(0,3,initial,rows,1,expected,model) == "PASS"
    assert grade(0,3,initial,rows,1,expected-1,model) == "FAIL"
    assert grade(0,3,initial,rows[1:],1,expected,model) == "INCOMPLETE"


@pytest.mark.parametrize("dry,wet,pressure,rh", [(20,20,100,100),(20,15,100,58.8),(30,20,90,41.06)])
def test_ventilated_wet_bulb_weather_report(dry,wet,pressure,rh):
    from benches.x03_weather_oracle import psychrometric_quality as grade
    model = dict(revision="station",kind="magnus-psychrometer",a=17.625,b_c=243.04,
                 base_kpa=0.61094,coefficient_per_c=0.00066)
    assert grade("station",model,dry,wet,pressure,rh,0.01) == "PASS"
    assert grade("station",model,dry,wet,pressure,rh-2,0.01) == "FAIL"
    assert grade("station",model,dry,dry+1,pressure,rh,0.01) == "FAIL"
    assert grade("station",model,dry,wet,None,rh,0.01) == "INCOMPLETE"


@pytest.mark.parametrize("history,opening,limit", [
    ([(0,3,"exposed",None),(3,6,"sealed",None)],2,5),
    ([(0,2,"bake","oven"),(2,6,"exposed",None)],9,4),
    ([(0,1,"exposed",None),(1,4,"sealed",None),(4,6,"exposed",None)],0,3),
])
def test_floor_life_at_component_use(history,opening,limit):
    from benches.x03_catalog_oracle import floor_life_quality as grade
    sheet = dict(revision="spec",floor_hours=limit,bake_min_hours={"oven":2})
    assert grade("spec",sheet,(0,6),opening,history) == "PASS"
    assert grade("spec",dict(sheet,floor_hours=limit-1),(0,6),opening,history) == "FAIL"
    assert grade("spec",sheet,(0,6),opening,history[:-1]) == "INCOMPLETE"
    assert grade("spec",sheet,(0,1),opening,[(0,1,"bake","oven")]) == "FAIL"


@pytest.mark.parametrize("unit,depth,expected", [("mm",5,6),("cm",0.3,4),("in",0.5,13.7)])
def test_precipitation_complete_interval_sum(unit,depth,expected):
    from benches.x03_weather_oracle import precipitation_quality as grade
    rows = [(0,1,depth,unit,"increment"),(1,3,1,"mm","resolved-reset")]
    def check(data,total):
        return grade((0,3),data,"complete-interval-increments",total)
    assert check(rows,expected) == "PASS"
    assert check(rows+[rows[0]],expected+1) == "FAIL"
    assert check(rows[:1],expected) == "INCOMPLETE"
    assert check([rows[0],(1,3,1,"mm","unknown-reset")],expected) == "INCOMPLETE"


@pytest.mark.parametrize("qualified,outside", [
    ((600,10000,5),(601,10000,5)),
    ((400,20000,8),(400,20001,8)),
    ((500,9000,4),(500,9000,6)),
])
def test_fuse_requires_one_joint_dc_envelope(qualified,outside):
    from benches.x03_catalog_oracle import dc_fuse_quality as grade
    sheet = dict(revision="dc2",exhaustive=True,dc_envelopes_v_a_ms=[(600,10000,5),(400,20000,8)])
    assert grade("dc2",sheet,qualified) == "PASS"
    assert grade("dc2",sheet,outside) == "FAIL"
    assert grade("dc2",dict(sheet,exhaustive=False),outside) == "INCOMPLETE"


@pytest.mark.parametrize("terminal,wire,tool,minimum", [
    ("lug","16 mm2","hex",400),("pin","1 mm2","indent",75),("splice","4 mm2","oval",150),
])
def test_crimp_requires_qualified_tool_and_each_pull(terminal,wire,tool,minimum):
    from benches.x03_catalog_oracle import crimp_pull_quality as grade
    sheet = dict(revision="p1",exhaustive=True,minimum_pull_n={(terminal,wire,tool):minimum})
    args = ("p1",sheet,terminal,wire)
    assert grade(*args,tool,[minimum,minimum+20]) == "PASS"
    assert grade(*args,tool,[minimum-1,minimum+20]) == "FAIL"
    assert grade(*args,"unlisted",[minimum+20]) == "FAIL"
    assert grade(*args,tool,[]) == "INCOMPLETE"


@pytest.mark.parametrize("azimuth,height,beam", [(360,15,850),(90,25,700),(-90,5,500)])
def test_horizon_masks_obstructed_direct_beam(azimuth,height,beam):
    from benches.x03_weather_oracle import horizon_beam_quality as grade
    profile = dict(revision="survey",sectors_deg=[(0,90,15),(90,180,25),(270,360,5)])
    args = ("survey",profile,azimuth)
    assert grade(*args,height,beam,0) == "PASS"
    assert grade(*args,height-1,beam,beam) == "FAIL"
    assert grade(*args,height+1,beam,beam) == "PASS"
    assert grade("survey",profile,200,height,beam,0) == "INCOMPLETE"


@pytest.mark.parametrize("material,thickness,required", [("PC",2,"V-0"),("ABS",3,"V-1"),("PA",1.5,"HB")])
def test_fire_rating_is_bound_to_grade_and_thickness(material,thickness,required):
    from benches.x03_catalog_oracle import flammability_quality as grade
    sheet = {"revision":"v3","exhaustive":True,"qualified_classes":{(material,thickness):[required]}}
    assert grade("v3",sheet,material,thickness,required) == "PASS"
    assert grade("v3",sheet,material,thickness/2,required) == "FAIL"
    assert grade("v3",sheet,"other",thickness,required) == "FAIL"
    assert grade("v3",dict(sheet,exhaustive=False),material,thickness/2,required) == "INCOMPLETE"


@pytest.mark.parametrize("depth,unit,rows,density,water", [
    (20,"cm",[(0,10,100),(10,20,300)],"kg/m3",40),
    (0.5,"m",[(0,0.5,0.2)],"g/cm3",100),
    (300,"mm",[(0,200,150),(200,300,400)],"kg/m3",70),
])
def test_snowpack_layer_mass_as_water_depth(depth,unit,rows,density,water):
    from benches.x03_weather_oracle import snow_water_quality as grade
    assert grade(depth,unit,rows,density,water) == "PASS"
    assert grade(depth,unit,rows,density,depth) == "FAIL"
    assert grade(depth,unit,rows[:-1],density,water) == "INCOMPLETE"


@pytest.mark.parametrize("seal,groove,allowed", [
    ((4,5),(3,3.6),(0.1,0.4)),((10,10),(7,8),(0.2,0.3)),((2,2.5),(1.5,1.8),(0.1,0.4)),
])
def test_gasket_squeeze_at_both_tolerance_extremes(seal,groove,allowed):
    from benches.x03_catalog_oracle import gasket_squeeze_quality as grade
    sheet = dict(revision="drawing",seal_height_mm=seal,squeeze_fraction=allowed)
    shallow,deep = groove
    assert grade("drawing",sheet,groove) == "PASS"
    assert grade("drawing",sheet,(shallow,deep+0.01)) == "FAIL"
    assert grade("drawing",sheet,(shallow-0.01,deep)) == "FAIL"
    assert grade("drawing",sheet,(shallow,None)) == "INCOMPLETE"


@pytest.mark.parametrize("width,intervals,resolution,peak", [
    (5,[(0,5,0),(5,10,5),(10,15,0)],5,60),
    (6,[(0,4,0),(4,8,8),(8,12,4),(12,16,0)],4,100),
    (5,[(0,4,4),(4,6,4),(6,9,9),(9,12,0)],4,156),
])
def test_peak_rain_intensity_between_gauge_readings(width,intervals,resolution,peak):
    from benches.x03_weather_oracle import rolling_rain_quality as grade
    rule = "continuous-uniform-interval-v1"
    assert grade(width,resolution,peak,intervals,rule) == "PASS"
    assert grade(width,resolution,peak-1,intervals,rule) == "FAIL"
    assert grade(width,resolution-1,peak,intervals,rule) == "INCOMPLETE"


@pytest.mark.parametrize("pollution,alt,creep,clear", [(2,2000,4,3),(2,3000,4,5),(3,2000,7,4)])
def test_spacing_depends_on_environment(pollution,alt,creep,clear):
    from benches.x03_catalog_oracle import insulation_spacing_quality as grade
    sheet = dict(revision="q",spacing_by_pollution={2:[(2000,4,3),(4000,4,5)],3:[(2000,7,4)]})
    assert grade("q",sheet,pollution,alt,creep,clear) == "PASS"
    assert grade("q",sheet,pollution,alt,creep-0.1,clear) == "FAIL"
    assert grade("q",sheet,pollution,alt,creep,clear-0.1) == "FAIL"
    assert grade("q",sheet,pollution,None,creep,clear) == "INCOMPLETE"


@pytest.mark.parametrize("tilt,facing,elev,sun,reading,expected", [
    (0,180,30,180,400,400),(60,90,30,90,800,400),(60,0,30,90,200,400),
])
def test_direct_beam_horizontal_projection(tilt,facing,elev,sun,reading,expected):
    from benches.x03_weather_oracle import pyranometer_horizontal_quality as grade
    model = "direct-only-cosine-v1"
    assert grade(model,tilt,facing,elev,sun,reading,expected,0.001) == "PASS"
    assert grade(model,tilt,facing,elev,sun,reading,expected+10,0.001) == "FAIL"
    assert grade(model,tilt,None,elev,sun,reading,expected,0.001) == "INCOMPLETE"
    assert grade("global-unseparated",tilt,facing,elev,sun,reading,expected,0.001) == "INCOMPLETE"


@pytest.mark.parametrize("material,history,dose", [
    ("jacket",[(4,10),(1,5)],45),("seal",[(1.5,8),(2,2)],16),("cover",[(0.5,6),(1,0)],3),
])
def test_material_uv_dose(material,history,dose):
    from benches.x03_catalog_oracle import uv_dose_quality as grade
    sheet = dict(revision="q",ingress="IP67",qualified_wh_m2={material:dose})
    assert grade("q",sheet,material,history,True) == "PASS"
    assert grade("q",sheet,material,history+[(1,1)],True) == "FAIL"
    assert grade("q",sheet,material,history,False) == "INCOMPLETE"


@pytest.mark.parametrize("rows,energy", [
    ([(0,1800,800,"W/m2")],400),
    ([(0,900,1,"kW/m2"),(900,2700,200,"W/m2")],350),
    ([(0,7200,0.25,"kW/m2")],500),
])
def test_energy_requires_full_daylight_integration(rows,energy):
    from benches.x03_weather_oracle import daylight_energy_quality as grade
    window = (0,rows[-1][1])
    assert grade(window,rows,energy) == "PASS"
    assert grade(window,rows,rows[0][2]) == "FAIL"
    assert grade((0,window[1]+1),rows,energy) == "INCOMPLETE"


@pytest.mark.parametrize("current,drop,max_resistance,temperature", [
    pytest.param(20, 4, 0.2, 45, id="qualify-control-cabinet-lug"),
    pytest.param(100, 2.5, 0.025, 65, id="accept-high-current-bus-joint"),
    pytest.param(2, 3, 1.5, 38, id="review-sensor-terminal-drop"),
])
def test_terminal_contact_acceptance(current, drop, max_resistance, temperature):
    from benches.x03_catalog_oracle import terminal_contact_quality as grade

    rule = dict(revision="dc1", test_current_a=current, max_joint_temp_c=temperature,
                max_resistance_mohm=max_resistance)
    assert grade("dc1", rule, current, temperature, drop) == "PASS"
    assert grade("dc1", rule, current, temperature + 1, drop) == "FAIL"
    assert grade("dc1", rule, current, temperature, drop + 0.01) == "FAIL"
    assert grade("dc1", rule, current / 2, temperature, drop) == "INCOMPLETE"
    assert grade("dc1", rule, current, None, drop) == "INCOMPLETE"
    assert grade("dc2", rule, current, temperature, drop) == "INCOMPLETE"


@pytest.mark.parametrize("ground,view,expected", [
    pytest.param({"soil": (800, 0.25)}, {"soil": 0.5}, 100, id="estimate-rear-ground-reflection"),
    pytest.param({"soil": (400, 0.2), "snow": (600, 0.8)}, {"soil": 0.4, "snow": 0.1}, 80,
                 id="compare-snow-and-soil"),
    pytest.param({"roof": (1000, 0.5), "shade": (0, 0.3)}, {"roof": 0.2, "shade": 0.7}, 100,
                 id="account-for-shaded-roof"),
])
def test_rear_reflected_component_counts_each_patch_once(ground, view, expected):
    from benches.x03_weather_oracle import rear_reflected_irradiance_quality as grade

    model = "lambertian-ground-patches-v1"
    assert grade(model, ground, view, expected) == "PASS"
    assert grade(model, ground, view, 2 * expected) == "FAIL"
    partial = dict(view)
    partial.pop(next(iter(partial)))
    assert grade(model, ground, partial, expected) == "INCOMPLETE"
    assert grade(None, ground, view, expected) == "INCOMPLETE"


@pytest.mark.parametrize("tips,bucket,dead,coverage,expected,uncorrected", [
    pytest.param([1, 3, 5], 0.2, 1, (2, 6), 0.8, 0.4, id="correct-a-cloudburst"),
    pytest.param([10, 13, 18], 0.24, 1, (3, 5), 0.66, 0.48, id="sum-variable-rain-intervals"),
    pytest.param([100, 106, 116], 0.4, 2, (6, 10), 1.1, 0.8, id="compare-two-gauge-periods"),
])
def test_tipping_bucket_dead_time_uses_each_calibrated_gap(tips, bucket, dead, coverage, expected, uncorrected):
    from benches.x03_weather_oracle import tipping_bucket_depth_quality as grade

    model = dict(rule="nonparalyzable-intertip-v1", bucket_mm=bucket, dead_s=dead,
                 min_gap_s=coverage[0], max_gap_s=coverage[1])
    assert grade(model, tips, expected) == "PASS"
    assert grade(model, tips, uncorrected) == "FAIL"
    assert grade(model, [0, coverage[1] + 1], expected) == "INCOMPLETE"
    assert grade(model, [0, dead], expected) == "INCOMPLETE"


@pytest.mark.parametrize("zero,direction,reading,expected", [
    pytest.param(90, "from", 45, 135, id="rotate-a-rooftop-vane-to-true-north"),
    pytest.param(270, "to", 90, 180, id="convert-an-outgoing-vector-to-wind-origin"),
    pytest.param(355, "from", 7, 2, id="audit-a-bearing-across-north"),
])
def test_wind_direction_rotates_sensor_frame(zero, direction, reading, expected):
    from benches.x03_weather_oracle import wind_direction_frame_quality as grade

    frame = {"model": "clockwise-true-north-v1", "zero_true_deg": zero, "sensor_direction": direction}
    assert grade(frame, reading, expected, 0) == "PASS"
    assert grade(frame, reading, (expected - 3) % 360, 3) == "PASS"
    assert grade(frame, reading, (expected + 180) % 360, 3) == "FAIL"
    for key in frame:
        assert grade({k: v for k, v in frame.items() if k != key}, reading, expected, 0) == "INCOMPLETE"


@pytest.mark.parametrize("pressure,elevation,temperature,expected", [
    pytest.param(900, 1000, 280, 1016.791621, id="normalize-mountain-station-pressure"),
    pytest.param(1020, -200, 290, 996.248605, id="reduce-a-below-sea-level-observation"),
    pytest.param(800, 1900, 275, 1012.979100, id="compare-high-plateau-pressure-to-sea-level"),
])
def test_sea_level_reduction_binds_datum(pressure, elevation, temperature, expected):
    from benches.x03_weather_oracle import sea_level_pressure_quality as grade

    station = dict(datum="station", pressure_hpa=pressure, elevation_m_asl=elevation, mean_column_k=temperature)
    report = dict(datum="sea-level", pressure_hpa=expected)
    model = "dry-isothermal-hypsometric-v1"
    assert grade(model, station, report, 0.000001) == "PASS"
    assert grade(model, station, dict(report, pressure_hpa=pressure), 0.000001) == "FAIL"
    assert grade(model, station, dict(report, datum="station"), 0.000001) == "FAIL"
    del station["elevation_m_asl"]
    assert grade(model, station, report, 0.000001) == "INCOMPLETE"


@pytest.mark.parametrize("winds,observed,step,rise,fall,mean,biased", [
    pytest.param([9, 5], [5, 9, 7], 1, 1, 0.5, 7, 8, id="remove-decay-bias-after-a-single-gust"),
    pytest.param([10, 6, 5], [8, 10, 9, 8], 2, 1, 0.25, 7, 9, id="reconstruct-a-two-step-lull"),
    pytest.param([6, 9, 0], [2, 5, 8, 6], 5, 0.75, 0.25, 5, 6.333333, id="recover-a-gust-followed-by-calm"),
])
def test_anemometer_mean_from_pinned_gust_response(winds, observed, step, rise, fall, mean, biased):
    from benches.x03_weather_oracle import anemometer_overspeed_quality as grade

    model = dict(rule="asymmetric-discrete-response-v1", step_s=step, rise_gain=rise,
                 fall_gain=fall, min_speed=0, max_speed=12)
    trace = [(i * step, speed) for i, speed in enumerate(observed)]
    assert sum(winds) == mean * len(winds)
    assert grade(model, trace, mean) == "PASS"
    assert grade(model, trace, biased) == "FAIL"
    assert grade(dict(model, max_speed=max(winds) - 1), trace, mean) == "INCOMPLETE"
    assert grade(model, [(0, 5), (step + 1, 6)], mean) == "INCOMPLETE"


@pytest.mark.parametrize("measurements,expected", [
    pytest.param([("rain", 2, 9)], 10, id="correct-a-liquid-gauge-reading"),
    pytest.param([("snow", 4, 3), ("snow", 0, 2)], 8, id="reconcile-windy-and-calm-snow"),
    pytest.param([("rain", 4, 4), ("snow", 2, 6)], 13, id="sum-a-rain-to-snow-transition"),
])
def test_undercatch_uses_phase_and_gauge_height_wind(measurements, expected):
    from benches.x03_weather_oracle import wind_undercatch_quality as grade

    calibration = dict(revision="wind-tunnel-4", phase_curves={
        "rain": [(0, 1), (4, 0.8)], "snow": [(0, 1), (4, 0.5)],
    })
    intervals = [dict(phase=p, gauge_height_wind_m_s=w, caught_mm=d) for p, w, d in measurements]
    assert grade("wind-tunnel-4", calibration, intervals, expected) == "PASS"
    assert grade("wind-tunnel-4", calibration, intervals, sum(d for _, _, d in measurements)) == "FAIL"
    assert grade("wind-tunnel-4", calibration, [dict(intervals[0], gauge_height_wind_m_s=5)], expected) == "INCOMPLETE"
    assert grade("wind-tunnel-3", calibration, intervals, expected) == "INCOMPLETE"


@pytest.mark.parametrize("curve,angle,raw,corrected", [
    pytest.param([(0, 1), (60, 0.8)], 60, 400, 500, id="correct-late-afternoon-underresponse"),
    pytest.param([(0, 1), (40, 1.2)], 20, 550, 500, id="remove-interpolated-overresponse"),
    pytest.param([(5, 1), (45, 0.9), (85, 0.6)], 65, 150, 200, id="reconcile-a-high-incidence-reading"),
])
def test_cosine_response_uses_calibrated_angle(curve, angle, raw, corrected):
    from benches.x03_weather_oracle import cosine_response_quality as grade

    calibration = dict(revision="lab-12", model="linear-measured-to-ideal-v1", angle_ratio=curve)
    assert grade("lab-12", calibration, angle, raw, corrected) == "PASS"
    assert grade("lab-12", calibration, angle, raw, raw) == "FAIL"
    assert grade("lab-12", calibration, curve[-1][0] + 1, raw, corrected) == "INCOMPLETE"
    assert grade("lab-13", calibration, angle, raw, corrected) == "INCOMPLETE"
