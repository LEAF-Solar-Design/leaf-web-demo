import hashlib
import pytest
from benches.x03_catalog_oracle import catalog_quality
from benches.x03_issue_delivery_oracle import delivery_quality
from benches.x03_weather_oracle import weather_quality


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
