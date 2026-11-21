"""Recorded release examples. All package bytes are local synthetic fixtures."""
import hashlib
import pytest


@pytest.mark.parametrize("tool", ["panel-count", "beam-length", "fan-table"])
def test_package_target_and_rollback_pair(tmp_path, tool):
    package = tmp_path / tool
    package.write_bytes(tool.encode())
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    target = "private-test"
    def valid_release(receipt):
        return (receipt["target"] == target and receipt["tool"] == tool
                and receipt["digest"] == hashlib.sha256(package.read_bytes()).hexdigest())
    release = {"target":target, "tool":tool, "digest":digest}
    assert valid_release(release)
    assert not valid_release(dict(release, target="other"))
    package.write_bytes(b"tampered")
    assert not valid_release(release)
    before = {"tool":tool, "revision":"v1", "drawing":"d1", "output":hashlib.sha256(b"first").hexdigest()}
    def restored(receipt):
        return all(receipt.get(k) == v for k, v in before.items())
    assert restored(dict(before))
    for key in before:
        assert not restored(dict(before, **{key:"mismatch"}))


@pytest.mark.parametrize("tool", ["panels","beams","routes"])
def test_package_members_checked_without_extraction(tmp_path, tool):
    import stat
    from zipfile import ZipFile, ZipInfo
    from benches.x03_package_oracle import package_quality
    package = tmp_path / "candidate.zip"
    filename = tool+"/tool.json"
    manifest = {"members":[filename], "sha256":{filename:hashlib.sha256(b"{}").hexdigest()}}
    for bad in ("clean", "parent", "link", "alias"):
        with ZipFile(package, "w") as archive:
            archive.writestr(filename, "{}")
            if bad == "link":
                entry = ZipInfo(tool+"/ref")
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(entry, "../other")
            elif bad == "alias":
                archive.writestr(tool.upper()+"/./tool.json", "{}")
            elif bad == "parent":
                archive.writestr(tool+"/../other", "")
        assert package_quality(package,manifest) == ("PASS" if bad == "clean" else "FAIL")
    assert list(tmp_path.iterdir()) == [package]


@pytest.mark.parametrize("name", ["panels","beams","ducts"])
def test_bounded_member_hash_and_crc(tmp_path, name):
    from zipfile import ZipFile
    from benches.x03_package_oracle import package_quality
    package = tmp_path / "tool.zip"
    data = b"synthetic member payload"
    manifest = {"members":[name], "sha256":{name:hashlib.sha256(data).hexdigest()}}
    with ZipFile(package, "w") as archive:
        archive.writestr(name, data)
    assert package_quality(package,manifest) == "PASS"
    assert package_quality(package,manifest,max_member_bytes=len(data)-1) == "FAIL"
    assert package_quality(package,manifest,max_total_bytes=len(data)-1) == "FAIL"
    raw = package.read_bytes()
    package.write_bytes(raw.replace(data,b"X"+data[1:],1))
    assert package_quality(package,manifest) == "FAIL"
    with ZipFile(package, "w") as archive:
        archive.writestr(name,data+b"changed")
    assert package_quality(package,manifest) == "FAIL"


@pytest.mark.parametrize("method,flags", [(0,1), (93,0), (99,1)])
def test_package_unreadable_encoding(tmp_path,method,flags):
    import struct
    from zipfile import ZipFile
    from benches.x03_package_oracle import package_quality
    path = tmp_path/"package.zip"
    with ZipFile(path,"w") as z:
        z.writestr("part",b"payload")
    data = bytearray(path.read_bytes())
    central = data.index(b"PK\x01\x02")
    for offset in (6,central+8):
        struct.pack_into("<HH",data,offset,flags,method)
    path.write_bytes(data)
    manifest = {"members":["part"],"sha256":{"part":hashlib.sha256(b"payload").hexdigest()}}
    assert package_quality(path,manifest) in {"FAIL","INCOMPLETE"}


@pytest.mark.parametrize("prefix", ["lib","LIB","./lib"])
def test_package_file_cannot_contain_members(tmp_path,prefix):
    from zipfile import ZipFile
    from benches.x03_package_oracle import package_quality
    path = tmp_path/"package.zip"
    names = [prefix, "lib/tool.py"]
    for order in (names,names[::-1]):
        with ZipFile(path,"w") as archive:
            for name in order:
                archive.writestr(name,b"data")
        manifest = {"members":order,"sha256":{n:hashlib.sha256(b"data").hexdigest() for n in order}}
        assert package_quality(path,manifest) == "FAIL"


@pytest.mark.parametrize("count", [2,9,16])
def test_member_count_includes_directories_before_reads(tmp_path,monkeypatch,count):
    from zipfile import ZipFile
    from benches.x03_package_oracle import package_quality
    path = tmp_path/"bag.zip"
    with ZipFile(path,"w") as z:
        z.writestr("tool", b"")
        for i in range(count-1):
            z.writestr(f"dir{i}/", b"")
    manifest = {"members":["tool"],"sha256":{"tool":hashlib.sha256(b"").hexdigest()}}
    assert package_quality(path,manifest,max_members=count) == "PASS"
    monkeypatch.setattr(ZipFile,"open",lambda *a,**kw: pytest.fail("content opened over budget"))
    assert package_quality(path,manifest,max_members=count-1) == "FAIL"


@pytest.mark.parametrize("name", ["Nul.txt","d/tool. ","d/ascii"+chr(9)+"name"])
def test_windows_invalid_members(tmp_path,name):
    from zipfile import ZipFile
    from benches.x03_package_oracle import package_quality
    path = tmp_path/"tool.zip"
    with ZipFile(path,"w") as z:
        z.writestr(name,b"")
    manifest = {"members":[name],"sha256":{name:hashlib.sha256(b"").hexdigest()}}
    assert package_quality(path,manifest) == "FAIL"


@pytest.mark.parametrize("count", [2,4,8])
def test_aggregate_limit_checked_before_read(tmp_path,monkeypatch,count):
    from zipfile import ZipFile
    from benches.x03_package_oracle import package_quality
    path = tmp_path/"tools.zip"
    names = [str(i) for i in range(count)]
    with ZipFile(path,"w") as z:
        for name in names:
            z.writestr(name,b"ab")
    manifest = dict(members=names,sha256={n:hashlib.sha256(b"ab").hexdigest() for n in names})
    assert package_quality(path,manifest,max_total_bytes=count*2) == "PASS"
    monkeypatch.setattr(ZipFile,"open",lambda *a,**kw:pytest.fail("early content read"))
    assert package_quality(path,manifest,max_total_bytes=count*2-1) == "FAIL"

@pytest.mark.parametrize(("first", "alias", "distinct"), [
    ("panels/Table.csv", "panels/TABLE.csv", "panels/tables.csv"),
    ("beams/caf\u00e9.py", "beams/cafe\u0301.py", "beams/cafe.py"),
    ("ducts/Stra\u00dfe.dat", "ducts/STRASSE.dat", "ducts/strase.dat"),
])
def test_canonical_package_collisions(tmp_path, first, alias, distinct):
    from zipfile import ZipFile
    from benches.x03_package_oracle import package_quality

    package = tmp_path / "package.zip"
    digest = hashlib.sha256(b"tool").hexdigest()

    def manifest(names):
        return {"members": list(names), "sha256": dict.fromkeys(names, digest)}

    for second, verdict in ((alias, "FAIL"), (distinct, "PASS")):
        for names in ((first, second), (second, first)):
            with ZipFile(package, "w") as archive:
                for name in names:
                    archive.writestr(name, b"tool")
            assert package_quality(package, manifest(names)) == verdict
            if second == alias:
                assert package_quality(package, manifest([first])) == "FAIL"
    assert list(tmp_path.iterdir()) == [package]


@pytest.mark.parametrize("name", ["panels/tool.json", "beams/length.csv", "ducts/report.txt"])
def test_manifest_inventory_before_content(tmp_path, name):
    from zipfile import ZipFile
    from benches.x03_package_oracle import package_quality
    path = tmp_path / "release.zip"
    directory = name.split("/")[0] + "/"
    manifest = dict(members=[name], sha256={name: hashlib.sha256(b"data").hexdigest()})
    for files, verdict in (([directory, name], "PASS"), ([directory], "FAIL"),
                           ([directory, name, "extra.exe"], "FAIL")):
        with ZipFile(path, "w") as archive:
            for member in files:
                archive.writestr(member, b"" if member.endswith("/") else b"data")
        assert package_quality(path, manifest) == verdict
    with ZipFile(path, "w") as archive:
        archive.writestr(directory, b"")
    assert package_quality(path, dict(members=[], sha256={})) == "INCOMPLETE"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("change,expected", [
    ({}, "PASS"), ({"version":"3.11"}, "FAIL"), ({"architecture":"x64"}, "FAIL"),
    ({"abi":"cp313"}, "FAIL"), ({"abi":None}, "INCOMPLETE"),
], ids=["pinned-runtime", "wrong-version", "wrong-architecture", "wrong-abi", "undeclared-abi"])
def test_package_target_runtime(change, expected):
    from benches.x03_package_oracle import runtime_abi_quality
    profile = dict(runtime="python", version="3.12", architecture="arm64", abi="cp312")
    assert runtime_abi_quality(dict(profile, **change), [profile]) == expected


def test_runtime_capabilities_must_coexist_on_one_target():
    from benches.x03_package_oracle import runtime_abi_quality
    needed = dict(runtime="engine", version="2", architecture="arm64", abi="v2")
    profiles = [dict(needed, abi="v1"), dict(needed, architecture="x64")]
    assert runtime_abi_quality(needed, profiles) == "FAIL"


@pytest.mark.parametrize("case,expected", [
    ("shared", "PASS"), ("missing", "INCOMPLETE"), ("conflict", "FAIL"), ("cycle", "FAIL"),
], ids=["resolve-shared-transitive-library", "missing-transitive-pin", "incompatible-versions", "cycle-in-lock"])
def test_dependency_inventory(case, expected):
    from benches.x03_package_oracle import dependency_closure_quality
    inventory = {"a": dict(version="1", requires={"lib":"2"}), "b": dict(version="1", requires={"lib":"2"}),
                 "lib": dict(version="2", requires={})}
    if case == "missing":
        del inventory["lib"]
    if case == "conflict":
        inventory["b"]["requires"]["lib"] = "1"
    if case == "cycle":
        inventory["lib"]["requires"]["a"] = "1"
    assert dependency_closure_quality({"a":"1", "b":"1"}, inventory) == expected


@pytest.mark.parametrize("declaration,records,expected", [
    ("pkg.main:run", [("pkg.main",["run","inspect"])], "PASS"),
    ("pkg.missing:run", [("pkg.main",["run"])], "FAIL"),
    ("pkg.main:build", [("pkg.main",["run"])], "FAIL"),
    ("pkg.main:run", [("pkg.main",["run"]),("pkg.main",["run"])], "FAIL"),
    ("pkg.main:run", [("pkg.main",None)], "INCOMPLETE"),
], ids=["resolve-static-export","missing-module","absent-symbol","ambiguous-target","missing-exports"])
def test_recorded_entrypoint(declaration, records, expected):
    from benches.x03_package_oracle import entrypoint_quality
    modules = [dict(module=name, callable_exports=exports) for name,exports in records]
    assert entrypoint_quality(declaration, modules) == expected


@pytest.mark.parametrize("change,ignore,expected", [
    ("order", ["member-order"], "PASS"), ("time", ["timestamps"], "PASS"),
    ("payload", ["member-order","timestamps"], "FAIL"), ("time", [], "FAIL"),
    ("comment", ["member-order","timestamps"], "FAIL"),
], ids=["reordered-members","rebuilt-later","altered-payload","undeclared-time-change","preserve-comments"])
def test_reproducible_content_zip(tmp_path, change, ignore, expected):
    from zipfile import ZipFile, ZipInfo
    from benches.x03_package_oracle import reproducible_zip_quality
    paths = [tmp_path / "a.zip", tmp_path / "b.zip"]
    for index, path in enumerate(paths):
        names = ["module", "manifest"]
        if index and change == "order":
            names.reverse()
        with ZipFile(path, "w") as archive:
            for name in names:
                year = 2021 if index and change == "time" else 2020
                entry = ZipInfo(name, (year,1,1,0,0,0))
                archive.writestr(entry, b"changed" if index and change == "payload" else name.encode())
            if index and change == "comment":
                archive.comment = b"changed"
    assert reproducible_zip_quality(*paths, ignore) == expected


@pytest.mark.parametrize("name,size", [("drawing",8),("schedule",32),("catalog",128)])
def test_zip_ratio_gate_before_any_member_read(tmp_path,monkeypatch,name,size):
    from hashlib import sha256
    from zipfile import ZipFile, ZIP_STORED, ZIP_DEFLATED
    from benches.x03_package_oracle import compression_ratio_quality as grade
    path = tmp_path / "fixture.zip"
    data = b"x" * size
    with ZipFile(path,"w",ZIP_STORED if size == 8 else ZIP_DEFLATED) as archive:
        archive.writestr("empty", b"")
        archive.writestr(name,data)
    manifest = {"members":["empty",name],"sha256":{"empty":sha256(b"").hexdigest(),name:sha256(data).hexdigest()}}
    with ZipFile(path) as archive:
        packed = archive.getinfo(name).compress_size
    from math import ceil
    assert grade(path,manifest,ceil(size/packed)) == "PASS"
    def unexpected_read(*args, **kwargs):
        pytest.fail("content opened before checking every member ratio")
    monkeypatch.setattr(ZipFile,"open",unexpected_read)
    assert grade(path,manifest,(size-1)/packed) == "FAIL"
    for limit in (None,0,False,float("nan")):
        assert grade(path,manifest,limit) == "INCOMPLETE"


@pytest.mark.parametrize("dep,fact,target,other", [
    ("native-reader", "os", "windows", "linux"), ("arm-kernel", "arch", "arm64", "amd64"),
    ("renderer", "backend", "software", "gpu"),
])
def test_dependency_resolution_uses_recorded_target(dep, fact, target, other):
    from benches.x03_package_oracle import target_dependency_quality
    markers = {dep: {fact: target}}
    def grade(facts, packaged):
        return target_dependency_quality(markers, facts, packaged)
    assert grade({fact: target}, {dep}) == "PASS"
    assert grade({fact: target}, set()) == "FAIL"
    assert grade({fact: other}, set()) == "PASS"
    assert grade({}, {dep}) == "INCOMPLETE"


@pytest.mark.parametrize("name,expression,expected", [
    ("viewer", "MIT OR GPL-3.0-only AND Apache-2.0", "PASS"),
    ("exporter", "(MIT OR Apache-2.0) AND GPL-3.0-only", "FAIL"),
    ("parser", "MIT AND (GPL-3.0-only OR Apache-2.0)", "PASS"),
])
def test_distribution_dependency_license_policy(name,expression,expected):
    from benches.x03_package_oracle import dependency_license_quality as grade
    policy = dict(revision="dist-v2",licenses={"MIT":True,"Apache-2.0":True,"GPL-3.0-only":False})
    def check(expr):
        return grade("dist-v2",policy,{name},{name:expr})
    assert check(expression) == expected
    assert check("MIT AND GPL-3.0-only") == "FAIL"
    assert check("MIT OR LicenseRef-unknown") == "INCOMPLETE"
    assert check("(MIT AND)") == "INCOMPLETE"
