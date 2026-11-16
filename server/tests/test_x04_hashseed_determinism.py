"""Ordered X04 output must not depend on a Python hash randomization seed."""
from test_x04_optimized_validation import child_result


def test_seeded_processes_preserve_ordered_report_listing_selection_and_highlights():
    body = '''
from drawing_dependency_report import from_pairs
intake={'inserts':[{'layer':'West','name':'Panel','handle':'B'},
                  {'layer':'East','name':'Panel','handle':'000a','rows':2,'columns':3},
                  {'layer':'West','name':'Panel','handle':'0xA','rows':2,'columns':3}]}
intake['dependency_report']=from_pairs([(0,'STYLE'),(3,'z.shx'),(0,'IMAGEDEF'),(1,'a.png')])
listing=tool_loader.run_tool_dynamic(TOOL,intake,{},aps_live=False)['result']
params={'set_ids':[s['set_id'] for s in listing['sets']]}
counted=tool_loader.run_tool_dynamic(TOOL,intake,params,aps_live=False)
print(json.dumps({'report':intake['dependency_report'],'listing':listing,
                  'selected':counted['result'],'overlay':counted['overlay']},sort_keys=True,allow_nan=False))
'''
    baseline = child_result(body, seed=0)
    assert baseline["selected"]["count"] == 7
    assert baseline["overlay"]["highlight_handles"] == ["A", "B"]
    for seed in (1, 42, 314159):
        assert child_result(body, seed=seed) == baseline
