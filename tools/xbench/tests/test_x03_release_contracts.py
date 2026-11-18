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
