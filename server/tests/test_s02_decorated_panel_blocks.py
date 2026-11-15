"""S02 decorated module outlines, using only synthetic block geometry."""
from copy import deepcopy
import math
import pytest
import solar_panel_group_kernel as kernel

def poly(points, closed=True):
    return {'kind': 'LWPOLYLINE', 'closed': closed, 'nrm': [0,0,1], 'elev': 0, 'pts': points}

def source():
    outer=poly([[0,0],[100,0],[100,50],[0,50]])
    inner=poly([[5,5],[95,5],[95,45],[5,45]])
    mark=poly([[10,10],[90,10]],False)
    return {'polylines': [], 'blocks': {'Module': {'base': [0,0,0], 'count': 3,
            'complete': True, 'children': [inner,mark,outer]}},
            'inserts': [{'name':'Module','handle':'A1','layer':'Panels_new',
                         'x':200,'y':300,'z':0,'rot':0,'scale':[1,1,1],'nrm':[0,0,1]}]}

@pytest.mark.parametrize('kind', ['polyline','line'])
@pytest.mark.parametrize('rotation', [0, math.pi/2])
def test_unique_outer_rectangle_survives_internal_decoration_and_transform(kind, rotation):
    intake=source();insert=intake['inserts'][0];insert['rot']=rotation
    if kind=='line': intake['blocks']['Module']['children'][1]={'kind':'LINE','pts':[[10,10,0],[90,10,0]]}
    before=deepcopy(intake)
    result=kernel.panels_from_intake(intake,layer_contains='Panels_new')
    assert len(result)==1 and result[0]['handle']=='A1'
    assert result[0]['centre']==pytest.approx([250,325] if rotation==0 else [175,350])
    assert sorted([result[0]['column_dim'],result[0]['row_dim']])==pytest.approx([50,100])
    assert intake==before

@pytest.mark.parametrize('fault',['duplicate','disjoint','crossing','incomplete','count','unknown','normal','elevation','nonfinite','too-many'])
def test_ambiguous_or_unbounded_decorated_definition_refuses_without_mutating_source(fault):
    intake=source();block=intake['blocks']['Module'];children=block['children']
    if fault=='duplicate': children.append(deepcopy(children[-1]));block['count']+=1
    elif fault=='disjoint': children[0]['pts']=[[150,0],[250,0],[250,50],[150,50]]
    elif fault=='crossing': children[1]['pts']=[[10,10],[110,10]]
    elif fault=='incomplete': block['complete']=False
    elif fault=='count': block['count']=4
    elif fault=='unknown': children[1]={'kind':'OTHER','type':'SPLINE'}
    elif fault=='normal': children[1]['nrm']=[0,1,0]
    elif fault=='elevation': children[1]['elev']=1
    elif fault=='nonfinite': children[1]['pts'][0][0]=float('inf')
    else: block['children']=[deepcopy(children[0]) for _ in range(257)];block['count']=257
    before=deepcopy(intake)
    with pytest.raises(kernel.PanelGroupKernelError): kernel.panels_from_intake(intake,layer_contains='Panels_new')
    assert intake==before
