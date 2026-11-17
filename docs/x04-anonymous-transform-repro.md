# Anonymous block transforms

Parent: Studio X-bench X04 raw drawing intake, anonymous-geometry product PR3261.

Source: ce199e80eb2e03a7bf0e8c30e93cd55ccb75c466. Offline synthetic DXF only, executed sequentially on leaf-svc through offhost.py --scope verify --no-local --fallback none. No customer drawings, network to staging, upload, deploy or full CI.

## Observed

Expected world position is insertion + rotation(scale(local vertex minus block base)). Base is [1,2,0], insertion [100,200,0], local outline [1,2],[3,2],[3,3],[1,3]. Base, reflection and unequal scale match exactly. Rotation is rounded to six decimal radians by intake; 90 degree controls differ by at most 0.0000014 world units from ideal vertices. The raw actual vertices below preserve that difference rather than declaring exact equality. Each selection counts1 and highlights A1 before/after emission. Inputs remain unchanged.

```json
[
  {
    "actual": [
      [
        100,
        200,
        0
      ],
      [
        102,
        200,
        0
      ],
      [
        102,
        201,
        0
      ],
      [
        100,
        201,
        0
      ]
    ],
    "case": "base",
    "degrees": 0,
    "expected": [
      [
        100,
        200,
        0
      ],
      [
        102,
        200,
        0
      ],
      [
        102,
        201,
        0
      ],
      [
        100,
        201,
        0
      ]
    ],
    "input_unchanged": true,
    "matches": true,
    "raw_sha256": "f01609ade4f109757c16a4c408c47bea23cd8b49e1beec9d2ad890618071e21f",
    "roundtrip_ok": true,
    "roundtrip_world": [
      [
        100,
        200,
        0
      ],
      [
        102,
        200,
        0
      ],
      [
        102,
        201,
        0
      ],
      [
        100,
        201,
        0
      ]
    ],
    "scale": [
      1,
      1,
      1
    ],
    "selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      }
    ],
    "source_handle": "A1"
  },
  {
    "actual": [
      [
        100,
        200,
        0
      ],
      [
        100.0000006535898,
        201.9999999999999,
        0
      ],
      [
        99.00000065358985,
        202.00000032679478,
        0
      ],
      [
        99.00000000000006,
        200.0000003267949,
        0
      ]
    ],
    "case": "rotate",
    "degrees": 90,
    "expected": [
      [
        100,
        200,
        0
      ],
      [
        100,
        202,
        0
      ],
      [
        99,
        202,
        0
      ],
      [
        99,
        200,
        0
      ]
    ],
    "input_unchanged": true,
    "matches": false,
    "raw_sha256": "778a9655333cfe5990bce4cb7619b955e92de07fa9ad19e1f38453c61d6a8353",
    "roundtrip_ok": true,
    "roundtrip_world": [
      [
        100,
        200,
        0
      ],
      [
        100.0000006535898,
        201.9999999999999,
        0
      ],
      [
        99.00000065358985,
        202.00000032679478,
        0
      ],
      [
        99.00000000000006,
        200.0000003267949,
        0
      ]
    ],
    "scale": [
      1,
      1,
      1
    ],
    "selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      }
    ],
    "source_handle": "A1"
  },
  {
    "actual": [
      [
        100,
        200,
        0
      ],
      [
        98,
        200,
        0
      ],
      [
        98,
        201,
        0
      ],
      [
        100,
        201,
        0
      ]
    ],
    "case": "reflect",
    "degrees": 0,
    "expected": [
      [
        100,
        200,
        0
      ],
      [
        98,
        200,
        0
      ],
      [
        98,
        201,
        0
      ],
      [
        100,
        201,
        0
      ]
    ],
    "input_unchanged": true,
    "matches": true,
    "raw_sha256": "b428f1cc039dd589aac97474bf08e27b951825dfe5d0f720eae0732104ac3a34",
    "roundtrip_ok": true,
    "roundtrip_world": [
      [
        100,
        200,
        0
      ],
      [
        98,
        200,
        0
      ],
      [
        98,
        201,
        0
      ],
      [
        100,
        201,
        0
      ]
    ],
    "scale": [
      -1,
      1,
      1
    ],
    "selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      }
    ],
    "source_handle": "A1"
  },
  {
    "actual": [
      [
        100,
        200,
        0
      ],
      [
        104,
        200,
        0
      ],
      [
        104,
        203,
        0
      ],
      [
        100,
        203,
        0
      ]
    ],
    "case": "unequal",
    "degrees": 0,
    "expected": [
      [
        100,
        200,
        0
      ],
      [
        104,
        200,
        0
      ],
      [
        104,
        203,
        0
      ],
      [
        100,
        203,
        0
      ]
    ],
    "input_unchanged": true,
    "matches": true,
    "raw_sha256": "748ffaded80bf5dd471a6de43bb5355dd53430505b267372583f9bf129c7bb3e",
    "roundtrip_ok": true,
    "roundtrip_world": [
      [
        100,
        200,
        0
      ],
      [
        104,
        200,
        0
      ],
      [
        104,
        203,
        0
      ],
      [
        100,
        203,
        0
      ]
    ],
    "scale": [
      2,
      3,
      1
    ],
    "selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      }
    ],
    "source_handle": "A1"
  },
  {
    "actual": [
      [
        100,
        200,
        0
      ],
      [
        99.99999869282041,
        196.00000000000023,
        0
      ],
      [
        96.99999869282057,
        196.0000009803849,
        0
      ],
      [
        97.00000000000016,
        200.0000009803847,
        0
      ]
    ],
    "case": "combined",
    "degrees": 90,
    "expected": [
      [
        100,
        200,
        0
      ],
      [
        100,
        196,
        0
      ],
      [
        97,
        196,
        0
      ],
      [
        97,
        200,
        0
      ]
    ],
    "input_unchanged": true,
    "matches": false,
    "raw_sha256": "049e04b7c0977bf37a2d9b3ac6dbefb56b8ad9ac818ab31d6d85609a385691f9",
    "roundtrip_ok": true,
    "roundtrip_world": [
      [
        100,
        200,
        0
      ],
      [
        99.99999869282041,
        196.00000000000023,
        0
      ],
      [
        96.99999869282057,
        196.0000009803849,
        0
      ],
      [
        97.00000000000016,
        200.0000009803847,
        0
      ]
    ],
    "scale": [
      -2,
      3,
      1
    ],
    "selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      }
    ],
    "source_handle": "A1"
  }
]
```

Captured result SHA-256: `8668bd7418f1302b52ade64d4b7b0b290e1baab30e075c8bb48f38e21d348679`. Original capture: `D:/agents/production-lanes-2026-11-10/XBENCH/status/X04-anon-repro-results.json`. Initial missing-three setup and overstrict numeric-threshold failures are retained separately; neither is represented as a passed run.

## Replay

Use the source revision above, its locked web dependencies and Python3/Node22. Save the following script outside the checkout and run it from the repository root on an allocated off-host verify seat. It invokes the actual reader, emitter, counter and Node projectServerBlocks, then prints all six result sections as JSON. If three is absent it installs locked web dependencies with npm ci --ignore-scripts. Capture stdout and compare semantic fields; single-run timing values vary. This is a direct reproduction, not a new product harness.

```python
import sys,json,copy,hashlib,time,tracemalloc,subprocess,importlib.util
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'server'))
import dxf_intake,intake_dxf
s=importlib.util.spec_from_file_location('counter',Path('server/builtins/count_modules.py'))
c=importlib.util.module_from_spec(s); s.loader.exec_module(c)
def outline(w=2):
 return [(0,'LWPOLYLINE'),(8,'0'),(90,4),(70,1),(10,1),(20,2),(10,1+w),(20,2),(10,1+w),(20,3),(10,1),(20,3)]
def definition(name='*U1',children=None,base=(1,2,0)):
 return [(0,'BLOCK'),(2,name),(70,1),(10,base[0]),(20,base[1]),(30,base[2])]+(outline() if children is None else children)+[(0,'ENDBLK')]
def insert(name='*U1',handle='A1',scale=(1,1,1),rot=0):
 return [(0,'INSERT'),(5,handle),(2,name),(8,'Panels'),(10,100),(20,200),(30,0),(41,scale[0]),(42,scale[1]),(43,scale[2]),(50,rot)]
def drawing(defs,refs):
 pairs=[(0,'SECTION'),(2,'TABLES'),(0,'TABLE'),(2,'LAYER'),(0,'LAYER'),(2,'0'),(0,'LAYER'),(2,'Panels'),(0,'ENDTAB'),(0,'ENDSEC'),(0,'SECTION'),(2,'BLOCKS')]+defs+[(0,'ENDSEC'),(0,'SECTION'),(2,'ENTITIES')]+refs+[(0,'ENDSEC'),(0,'EOF')]
 return ('\n'.join(str(v) for p in pairs for v in p)+'\n').encode()
def counts(x):
 listing,_=c.run(x,{})
 selected=[]
 for row in listing['sets']:
  r,o=c.run(x,{'set_id':row['set_id']});selected.append({'set_id':row['set_id'],'count':r['count'],'handles':o['highlight_handles']})
 return selected
def emit(x):
 try:
  raw=intake_dxf.intake_to_dxf(x); y=dxf_intake.parse_dxf_bytes(raw)
  return {'ok':True,'blocks':{n:{'complete':b['complete'],'children':len(b['children']),'kinds':[v['kind'] for v in b['children']]} for n,b in y.get('blocks',{}).items()},'selected':counts(y)},y
 except Exception as e:return {'ok':False,'error_type':type(e).__name__,'error':str(e)},None
def projected(x):
 js="import {projectServerBlocks} from './web/src/cadedit/engineIntake.js'; let b=''; for await (const t of process.stdin) b+=t; console.log(JSON.stringify(projectServerBlocks(JSON.parse(b))));"
 p=subprocess.run(['node','--input-type=module','-e',js],input=json.dumps(x),text=True,capture_output=True)
 if p.returncode:raise RuntimeError(p.stderr)
 return json.loads(p.stdout)
if not Path('web/node_modules/three/package.json').exists():
 p=subprocess.run(['npm','ci','--ignore-scripts','--no-audit','--no-fund'],cwd='web',capture_output=True,text=True)
 if p.returncode:raise RuntimeError(p.stderr[-2000:])
out={}
trans=[]
for label,scale,rot,expected in [('base',[1,1,1],0,[[100,200,0],[102,200,0],[102,201,0],[100,201,0]]),('rotate',[1,1,1],90,[[100,200,0],[100,202,0],[99,202,0],[99,200,0]]),('reflect',[-1,1,1],0,[[100,200,0],[98,200,0],[98,201,0],[100,201,0]]),('unequal',[2,3,1],0,[[100,200,0],[104,200,0],[104,203,0],[100,203,0]]),('combined',[-2,3,1],90,[[100,200,0],[100,196,0],[97,196,0],[97,200,0]])]:
 raw=drawing(definition(),insert(scale=scale,rot=rot)); x=dxf_intake.parse_dxf_bytes(raw); before=copy.deepcopy(x); view=projected(x); report,y=emit(x)
 actual=view['polylines'][0]['pts']; same=all(abs(a-b)<1e-8 for pa,pb in zip(expected,actual) for a,b in zip(pa,pb))
 trans.append({'case':label,'scale':scale,'degrees':rot,'expected':expected,'actual':actual,'matches':same,'source_handle':view['polylines'][0]['sourceHandle'],'selected':counts(x),'roundtrip_ok':report['ok'],'roundtrip_world':projected(y)['polylines'][0]['pts'] if y else None,'input_unchanged':x==before,'raw_sha256':hashlib.sha256(raw).hexdigest()})
 assert x==before
out['transforms']=trans
raw=drawing(definition('*U1')+definition('*U2'),insert('*U1','A1')+insert('*U1','A2')+insert('*U2','B1'))
x=dxf_intake.parse_dxf_bytes(raw); er,y=emit(x); listing,_=c.run(x,{}); ids=[s['set_id'] for s in listing['sets']]; union,overlay=c.run(x,{'set_ids':ids})
out['competing']={'before':counts(x),'roundtrip':er,'union_count':union['count'],'union_handles':overlay['highlight_handles'],'input_sha256':hashlib.sha256(raw).hexdigest()}
out['children']=[]
for label,extra in [('nested',insert('*U2','C1')),('arc',[(0,'ARC'),(8,'0'),(10,1),(20,2),(40,1),(50,0),(51,90)]),('text',[(0,'TEXT'),(8,'0'),(10,1),(20,2),(40,1),(1,'synthetic')]),('all',insert('*U2','C1')+[(0,'ARC'),(10,1),(20,2),(40,1),(50,0),(51,90),(0,'TEXT'),(10,1),(20,2),(40,1),(1,'synthetic')])]:
 x=dxf_intake.parse_dxf_bytes(drawing(definition(children=outline()+extra)+definition('*U2'),insert()))
 b=x['blocks']['*U1']; er,_=emit(x); v=projected(x)
 out['children'].append({'case':label,'complete':b['complete'],'count':b['count'],'kinds':[p['kind'] for p in b['children']],'diagnostics':x.get('parseErrors',[]),'projected_paths':len(v['polylines']),'selected':counts(x),'emission':er})
out['caps']=[]
for n in [59,60,61]:
 x=dxf_intake.parse_dxf_bytes(drawing(definition(children=outline()*n),insert())); b=x['blocks']['*U1']; er,_=emit(x)
 out['caps'].append({'boundary':'children','n':n,'retained':len(b['children']),'complete':b['complete'],'declared_count':b['count'],'diagnostics':x.get('parseErrors',[]),'projected_paths':len(projected(x)['polylines']),'selected':counts(x),'emission_ok':er['ok'],'emission_error':er.get('error')})
for n in [199,200,201]:
 defs=sum((definition(f'*U{i}') for i in range(n)),[]); refs=insert('*U0','A1')+insert(f'*U{n-1}','B1')
 x=dxf_intake.parse_dxf_bytes(drawing(defs,refs)); er,_=emit(x)
 out['caps'].append({'boundary':'definitions','n':n,'retained':len(x['blocks']),'blocksCapped':x.get('blocksCapped'),'first_retained':'*U0' in x['blocks'],'last_retained':f'*U{n-1}' in x['blocks'],'all_retained_complete':all(b['complete'] for b in x['blocks'].values()),'diagnostics':x.get('parseErrors',[]),'selected':counts(x),'emission_ok':er['ok']})
out['collisions']=[]
for label,defs,refs in [('duplicate_small_last',definition(children=outline(8))+definition(children=outline(2)),insert()),('duplicate_large_last',definition(children=outline(2))+definition(children=outline(8)),insert()),('case_variants',definition('*U1',outline(2))+definition('*u1',outline(8)),insert('*U1','A1')+insert('*u1','B1')),('case_variants_reversed',definition('*u1',outline(8))+definition('*U1',outline(2)),insert('*U1','A1')+insert('*u1','B1'))]:
 x=dxf_intake.parse_dxf_bytes(drawing(defs,refs)); er,y=emit(x)
 out['collisions'].append({'case':label,'blocks':{n:b['children'][0]['pts'] for n,b in x['blocks'].items()},'world':[p['pts'] for p in projected(x)['polylines']],'diagnostics':x.get('parseErrors',[]),'selected':counts(x),'roundtrip_names':list(y['blocks']) if y else None,'roundtrip_selected':counts(y) if y else None})
out['profile']=[]
for n in [10,100,1000]:
 handles=[f'{i+1:X}' for i in range(n)]; raw=drawing(definition(),sum((insert(handle=h) for h in handles),[])); before=hashlib.sha256(raw).hexdigest()
 tracemalloc.start(); t=time.perf_counter(); x=dxf_intake.parse_dxf_bytes(raw); parse=time.perf_counter()-t
 t=time.perf_counter(); encoded=intake_dxf.intake_to_dxf(x); emission=time.perf_counter()-t
 t=time.perf_counter(); result=counts(x); counting=time.perf_counter()-t; current,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
 out['profile'].append({'inserts':n,'input_bytes':len(raw),'emitted_bytes':len(encoded),'parse_seconds':parse,'emit_seconds':emission,'count_seconds':counting,'python_peak_bytes':peak,'count':result[0]['count'],'all_handles_in_order':result[0]['handles']==handles,'handles_sha256':hashlib.sha256(json.dumps(result[0]['handles'],separators=(',',':')).encode()).hexdigest(),'original_bytes_unchanged':hashlib.sha256(raw).hexdigest()==before})
print(json.dumps(out,sort_keys=True))

```

This evidence does not establish live staging behavior or the ten held-out drawing grade.
