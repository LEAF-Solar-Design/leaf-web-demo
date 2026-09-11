import copy
import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
import pytest
s=importlib.util.spec_from_file_location('author',Path(__file__).parents[1]/'campaign_native_author.py')
a=importlib.util.module_from_spec(s); s.loader.exec_module(a)
U=['00000000-0000-4000-8000-%012d'%n for n in range(1,8)]
R=dict(bucket='evidence',key='evidence/admission/authority',version_id='v1',sha256='a'*64)
@pytest.fixture
def data():
 m=dict(version=1,org_id=U[0],project_id=U[1],tenant_id=U[0],principal_binding_id=U[2],operator_arn='arn:aws:sts::807034087062:assumed-role/Operator/session',authority_ref=R.copy(),machine_id='configured-machine',campaign=dict(title='Native',prompt='Saved-plan native change',idempotency_key='campaign'),task=dict(task_key='native',title='Change',spec='Use trusted apply gate',source_sha='b'*40,owned_paths=['terraform'],verify_command='metadata only',declared_artifacts=['receipt'],idempotency_key='task'))
 i=dict(enabled=True,jobs=[dict(org_id=U[0],project_id=U[1],commit_sha='b'*40,reservation_microusd=1250000,acceptance_bucket='evidence',acceptance_prefix='evidence/admission/',job_name='arlo-worker',repository_id='owner/terraform',environment='staging')])
 return m,i
class Fake:
 def __init__(self): self.rows={}; self.events=[]; self.active=False; self.valid=True; self.saved=None
 @contextmanager
 def guard(self,m,read_only=False):
  self.events.append('guard'); a.require(self.valid); self.active=True
  try: yield
  finally: self.active=False; self.events.append('release')
 def inspect(self,m):
  assert self.active
  if self.saved: a.require(self.saved==m)
  return dict(self.rows)
 def put(self,key,value,event):
  assert self.active; self.events.append(event); self.rows[key]=value; return value
 def campaign(self,m):
  self.saved=copy.deepcopy(m); return self.put('campaign_id',U[3],'campaign')
 def enrollment(self,m,c): return self.put('enrollment_id',U[4],'enrollment')
 def task(self,m,c): return self.put('task_id',U[5],'task')
 def allocate(self,m,c,aid,amount,ref):
  assert amount==1250000; self.put('allocation_id',aid,'allocate')
 def enable(self,*args):
  assert self.active; self.events.append('enable')
def grant(m,i,ids):
 j=i['jobs'][0]
 return dict(version=1,org_id=m['org_id'],project_id=m['project_id'],tenant_id=m['tenant_id'],principal_binding_id=m['principal_binding_id'],operator_arn=m['operator_arn'],authority_ref=m['authority_ref'],**ids,source_sha=j['commit_sha'],job_name=j['job_name'],repository_id=j['repository_id'],environment=j['environment'],limit_microusd=1250000,max_active=1,allocation_id='reserved-original')
def reader(g):
 b=a.raw(g); return dict(R,key='evidence/admission/grant',sha256=a.hashlib.sha256(b).hexdigest()),lambda _:b
def test_prepare_activate_replay(data):
 m,i=data; f=Fake(); ids=a.run('prepare',m,i,f)
 assert f.events==['guard','campaign','enrollment','task','release']
 assert a.run('prepare',m,i,f)==ids
 ref,read=reader(grant(m,i,ids))
 result=a.run('activate',m,i,f,identities=ids,funding_ref=ref,reader=read)
 assert f.events[-4:]==['guard','allocate','enable','release']
 assert a.run('activate',m,i,f,identities=ids,funding_ref=ref,reader=read)==result
 assert len({f.rows['task_id']})==1
@pytest.mark.parametrize('field,value',[('org_id',U[6]),('task_id',U[6]),('principal_binding_id',U[6]),('operator_arn','arn:aws:iam::807034087062:role/Other'),('source_sha','c'*40),('limit_microusd',1250001),('max_active',2),('job_name','other'),('prepared_input_digest','d'*64),('allocation_id','')])
def test_grant_drift(data,field,value):
 m,i=data; f=Fake(); ids=a.run('prepare',m,i,f); g=grant(m,i,ids); g[field]=value
 ref,read=reader(g); f.events.clear()
 with pytest.raises(a.Refused): a.run('activate',m,i,f,identities=ids,funding_ref=ref,reader=read)
 assert not f.events
@pytest.mark.parametrize('kind',['bytes','oversize','bucket','prefix','version','unknown'])
def test_reference(data,kind):
 m,i=data; f=Fake(); ids=a.run('prepare',m,i,f); ref,read=reader(grant(m,i,ids)); f.events.clear()
 if kind=='bytes': read=lambda _:b'{}'
 if kind=='oversize': read=lambda _:b'x'*(a.LIMIT+1)
 if kind=='bucket': ref['bucket']='other'
 if kind=='prefix': ref['key']='outside/grant'
 if kind=='version': ref['version_id']='latest'
 if kind=='unknown': ref['sql']='override'
 with pytest.raises(a.Refused): a.run('activate',m,i,f,identities=ids,funding_ref=ref,reader=read)
 assert not f.events
@pytest.mark.parametrize('stage',['prepare','activate'])
def test_revoked(data,stage):
 m,i=data; f=Fake(); ids=a.run('prepare',m,i,f); ref,read=reader(grant(m,i,ids)); f.valid=False; f.events.clear()
 with pytest.raises(a.Refused): a.run(stage,m,i,f,**(dict(identities=ids,funding_ref=ref,reader=read) if stage=='activate' else {}))
 assert f.events==['guard']
def test_read_and_missing_funding(data):
 m,i=data; f=Fake(); assert a.run('inspect',m,i,f)=={}; assert f.events==['guard','release']
 with pytest.raises(a.Refused): a.run('activate',m,i,f)
def test_changed_input(data):
 m,i=data; f=Fake(); ids=a.run('prepare',m,i,f); m['campaign']['prompt']='changed'
 with pytest.raises(a.Refused): a.run('prepare',m,i,f)
 m['campaign']['prompt']='Saved-plan native change'; ref,read=reader(grant(m,i,ids)); f.rows['task_id']=U[6]; f.events.clear()
 with pytest.raises(a.Refused): a.run('activate',m,i,f,identities=ids,funding_ref=ref,reader=read)
 assert f.events==['guard','release']
@pytest.mark.parametrize('kind',['unknown','uuid','boolean','oversize','source'])
def test_input_rejection(data,kind):
 m,i=data; f=Fake()
 if kind=='unknown': m['sql']='override'
 if kind=='uuid': m['principal_binding_id']='invented'
 if kind=='boolean': m['version']=True
 if kind=='oversize': m['task']['spec']='x'*16385
 if kind=='source': i['jobs'][0]['commit_sha']='c'*40
 with pytest.raises(ValueError): a.run('prepare',m,i,f)
 assert not f.events
def test_cli(data,tmp_path,capsys):
 m,i=data; mp=tmp_path/'m.json'; ip=tmp_path/'i.json'; mp.write_text(json.dumps(m)); ip.write_text(json.dumps(i)); f=Fake()
 args=['prepare','--manifest',str(mp),'--installation',str(ip)]
 assert a.main(args,dependencies=lambda:(f,lambda v:v))==0
 assert json.loads(capsys.readouterr().out)['result']['task_id']==U[5]
 assert a.main(['activate',*args[1:]],dependencies=lambda:(f,lambda v:v))==2
 with pytest.raises(a.Refused): a.decode(b'{"x":1,"x":2}')
 with pytest.raises(a.Refused): a.decode(b' '*(a.LIMIT+1))
def test_real_guard(data):
 m,_=data; events=[]
 class Cursor:
  def execute(self,sql,params=None): events.append(sql)
  def fetchone(self): return {'binding_id':U[2]}
  def __enter__(self): return self
  def __exit__(self,*args): events.append('cursor_exit')
 class Conn:
  def cursor(self): return Cursor()
  def __enter__(self): return self
  def __exit__(self,*args): events.append('connection_exit')
 class DB:
  def connection(self): return Conn()
 class E:
  def allowed_machines(self): return [m['machine_id']]
 f=a.Stores.__new__(a.Stores); f.db=DB(); f.e=E()
 with f.guard(m):
  assert 'FOR SHARE OF m,b,p' in events[-1]
  assert "m.role IN ('owner','editor')" in events[-1]
  assert "b.status='active'" in events[-1]
  events.append('write')
 assert events[-3:]==['write','cursor_exit','connection_exit']
