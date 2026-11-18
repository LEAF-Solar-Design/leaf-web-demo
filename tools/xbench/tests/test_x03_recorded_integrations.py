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
