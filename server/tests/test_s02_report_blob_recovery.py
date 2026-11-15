"""Verified read recovery uses actual synthetic PDF bytes, never a replacement hash."""
from copy import deepcopy
from hashlib import sha256
import pytest
import solar_project_solaredge as service
import solar_project_read as reads
from solar_design_graph import GraphValidationError
from test_s02_partial_tracking import mixed  # noqa: F401
from test_w1_design_graph import graph  # noqa: F401
import test_sip_r3b_tools as tools

REAL_PARSE = service.reports.parse_source


def synthetic_pdf():
    commands=[]
    for x,y in ((100,500),(140,500),(600,100)):
        commands.extend([f'0.9 0.95 1 rg {x} {y} 30 60 re f',f'1 0 0 RG {x+10} {y+25} 10 10 re S'])
    commands.extend(['1 0 0 RG 115 530 m 125 530 145 530 155 530 c S','615 130 m 630 130 640 130 650 130 c S'])
    for text,x,y in [('1.1',112,528),('7',152,528),('1.2',612,128)]:
        commands.append(f'BT /F1 4 Tf {x} {y} Td ({text}) Tj ET')
    content='\n'.join(commands).encode('ascii')
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1000 1000] /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Contents 4 0 R >>',
        b'<< /Length '+str(len(content)).encode()+b' >>\nstream\n'+content+b'\nendstream']
    out=bytearray(b'%PDF-1.4\n');offsets=[]
    for i,body in enumerate(objects,1):
        offsets.append(len(out));out+=f'{i} 0 obj\n'.encode()+body+b'\nendobj\n'
    xref=len(out);out+=f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode()
    for offset in offsets:out+=f'{offset:010d} 00000 n \n'.encode()
    out+=f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return bytes(out)


@pytest.fixture
def accepted(mixed,monkeypatch):
    s=mixed
    monkeypatch.setattr(service.reports,'parse_source',REAL_PARSE)
    s.source_id=service.import_source(s.tenant,s.project,s.drawing,s.ctx.parent_version_id,synthetic_pdf())['source']['artifact_id']
    result=service.build_report(s.tenant,s.project,s.drawing,s.ctx.parent_version_id,
        {'source_artifact_id':s.source_id,'alignment_tolerance':0.1,'selection_order':'unknown'})
    prepared=tools.prepare(s,service.ACCEPT_TOOL,{'expected_rev':s.ctx.graph['rev'],
        'report_artifact_id':result['report']['artifact_id'],'accept_matched_only':True},s.ctx)
    ctx=tools.output(s,tools.publish(s,prepared))
    meta,body=service.artifacts.read_project_artifact(str(s.org),s.project,s.drawing,result['report']['artifact_id'])
    scope={key:meta[key] for key in ('organization_id','project_id','drawing_id')}
    return s,ctx,meta,body,scope


def read(s,ctx,section='strings'):
    return reads.run_project_read(s.tenant,s.project,s.drawing,ctx.parent_version_id,
        service.READ_TOOL,{'section':section},catalog_digest=tools.manifest(service.READ_TOOL))['result']['output']


@pytest.mark.parametrize('section',['strings','frames','unassigned'])
def test_missing_blob_exact_reproduction_keeps_saved_tracking_readable(accepted,monkeypatch,section):
    s,ctx,meta,body,scope=accepted
    before=deepcopy(ctx.intake);baseline=read(s,ctx,section)
    writes=len(s.writes);head=s.head;versions=set(s.versions)
    key=service.artifacts._key(scope,meta['content_sha256'],blob=True)
    monkeypatch.delitem(s.blobs,key)
    recovered=read(s,ctx,section)
    assert recovered['accepted'] is True and recovered['labels_only'] is True
    assert recovered['items']==baseline['items'] and recovered['provenance']==baseline['provenance']
    assert recovered['acceptance']==baseline['acceptance']
    assert recovered['report_download']['available'] is False
    assert 'unavailable' in recovered['one_liner'].lower()
    assert ctx.intake==before and s.head==head and set(s.versions)==versions and len(s.writes)==writes
    assert key not in s.blobs
    with pytest.raises(GraphValidationError,match='ARTIFACT_CORRUPT'):
        service.artifacts.read_project_artifact(str(s.org),s.project,s.drawing,meta['artifact_id'])


@pytest.mark.parametrize('replacement',[b'x',b'%PDF-1.4\nnot the saved report'])
def test_present_corrupt_blob_never_uses_recovery(accepted,monkeypatch,replacement):
    s,ctx,meta,body,scope=accepted
    monkeypatch.setitem(s.blobs,service.artifacts._key(scope,meta['content_sha256'],blob=True),replacement)
    def forbidden(*args,**kwargs):raise AssertionError('present corrupt blob must not trigger parse recovery')
    monkeypatch.setattr(service.reports,'parse_source',forbidden)
    with pytest.raises(GraphValidationError,match='SOLAREDGE_IMPORT_RECORD_INVALID'):read(s,ctx)


def test_missing_metadata_cannot_claim_verified_recovery(accepted,monkeypatch):
    s,ctx,meta,body,scope=accepted
    monkeypatch.delitem(s.blobs,service.artifacts._key(scope,meta['artifact_id']))
    with pytest.raises(GraphValidationError,match='SOLAREDGE_IMPORT_RECORD_INVALID'):read(s,ctx)


def test_missing_report_with_bad_source_refuses(accepted,monkeypatch):
    s,ctx,meta,body,scope=accepted
    record=ctx.graph['extra'][service.tracking.STORE_KEY]
    monkeypatch.delitem(s.blobs,service.artifacts._key(scope,meta['content_sha256'],blob=True))
    monkeypatch.setitem(s.blobs,service.artifacts._key(scope,record['source']['content_sha256'],blob=True),b'changed source')
    with pytest.raises(GraphValidationError,match='SOLAREDGE_IMPORT_RECORD_INVALID'):read(s,ctx)


def test_recreated_different_report_cannot_replace_original_hash(accepted,monkeypatch):
    s,ctx,meta,body,scope=accepted
    monkeypatch.delitem(s.blobs,service.artifacts._key(scope,meta['content_sha256'],blob=True))
    monkeypatch.setattr(service.reports,'parse_source',lambda data:[])
    with pytest.raises(GraphValidationError,match='SOLAREDGE_IMPORT_RECORD_INVALID'):read(s,ctx)


def test_normal_read_output_does_not_gain_recovery_state(accepted):
    s,ctx,meta,body,scope=accepted
    output=read(s,ctx)
    assert 'report_download' not in output
    assert sha256(body).hexdigest()==meta['content_sha256']
