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
    old = dict(destination="primary",drawing=drawing,revision="1",payload_digest="first")
    new = dict(old,revision="2",payload_digest="second")
    attempts = [dict(old,status="timeout"),dict(old,status="ack"),dict(new,status="ack")]
    assert delivery_quality(attempts,[old,new]) == "PASS"
    for effects in ([old], [old,old,new], [old,dict(new,payload_digest="changed")]):
        assert delivery_quality(attempts,effects) == "FAIL"
    assert delivery_quality(attempts+[dict(old,payload_digest="conflict")],[old,new]) == "FAIL"


@pytest.mark.parametrize("temp,modules,expected", [(-25,10,460),(0,5,215),(50,2,74)])
def test_voltage_reference(temp, modules, expected):
    weather = dict(site="roof",station="local",cell_temp=temp,unit="C",timestamp=90)
    def check(data, result):
        return weather_quality(data,site="roof",station="local",as_of=100,max_age=10,voc_at_25=40,
                              coefficient_per_c=-0.003,modules=modules,reported=result)
    assert check(weather,expected) == "PASS"
    assert check(weather,expected+1) == "FAIL"
    assert check({},expected) == "INCOMPLETE"
    for changes in (dict(timestamp=89), dict(unit="F"), dict(site="roof",station="local",cell_temp=float("nan"))):
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
    assert weather_quality(dict(site="roof",station="local",cell_temp=0,unit="C",timestamp=10),**args) == "FAIL"


@pytest.mark.parametrize("site", ["roof","yard","hangar"])
def test_weather_location_and_delivery_destination(site):
    weather = dict(site=site,station="s1",cell_temp=25,unit="C",timestamp=5)
    def check(data):
        return weather_quality(data,site=site,station="s1",as_of=5,max_age=1,voc_at_25=40,
                               coefficient_per_c=-0.003,modules=2,reported=80)
    assert check(weather) == "PASS"
    assert check(dict(weather,site="other")) == "FAIL"
    assert check(dict(weather,station="other")) == "FAIL"
    assert check(dict(weather,site=None)) == "INCOMPLETE"
    a = dict(destination="primary",drawing=site,revision="one",payload_digest="abc")
    b = dict(a,destination="backup")
    assert delivery_quality([a,a,b],[a,b]) == "PASS"
    assert delivery_quality([a,b],[a]) == "FAIL"
    assert delivery_quality([a,b],[a,a]) == "FAIL"
