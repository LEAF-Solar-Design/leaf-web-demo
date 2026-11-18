import hashlib
import pytest
from benches.x03_catalog_oracle import catalog_quality
from benches.x03_issue_delivery_oracle import delivery_quality
from benches.x03_weather_oracle import weather_quality


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
