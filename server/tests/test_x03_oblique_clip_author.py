"""Paper-independent 3D surface area from OCS boundary vertices and DXF frames."""
import pytest

from test_x03_oblique_spline_author import section
from test_x03_offline_drawing_author import invoke, tools_fallback


ASKS = [
    ("Measure the clipped area of each panel in its tilted plane.", (0,.6,.8), (2,3), 1),
    ("Sum visible PV module footprint area after the oblique XCLIP.", (.8,0,.6), (-3,.5), 2),
    ("Report each panel's retained area on the vertical clipping plane.", (0,1,0), (1,4), 3),
]


def basis(n):
    # These three normals have exactly one horizontal component. Express
    # AutoCAD's arbitrary X and Y axes explicitly for this family of fixtures.
    if n[0]:
        return (0,1,0), (-n[2],0,n[0])
    return (-1,0,0), (0,-n[2],n[1])


def dot(a,b):
    return sum(x*y for x,y in zip(a,b))


def drawing(normal,scale,right,offset=0,bad_normal=False):
    u,v = basis(normal)
    old_origin = (2,-3,4)
    axes = (u,v,normal)
    # The clip system is rotated 90 degrees in the original tilted plane:
    # clip X follows block Y, and clip Y follows minus block X.
    clip_basis = (v,tuple(-x for x in u),normal)
    original_inverse = [x for axis in axes for x in (*axis,-dot(axis,old_origin))]
    clip_origin = tuple(x+offset*n for x,n in zip(old_origin,normal))
    boundary_matrix = [x for axis in clip_basis for x in (*axis,-dot(axis,clip_origin))]
    outline = [(0,"LWPOLYLINE"),(5,"D1"),(70,1),(90,3)]
    for x,y in [(0,0),(4,0),(0,4)]:
        outline.extend([(10,x),(20,y)])
    block = [(0,"BLOCK"),(2,"PV Panel"),(10,0),(20,0),*outline,(0,"ENDBLK")]
    root = [(0,"INSERT"),(2,"PV Panel"),(5,"B1"),(8,"Roof"),(10,5),(20,8),(30,1),
            (41,scale[0]),(42,scale[1]),*zip((210,220,230),normal),
            (102,"{ACAD_XDICTIONARY"),(360,"E1"),(102,"}")]
    # Clip rectangle is [0,right] in block X and [0,3] in block Y.
    clips = [(0,"DICTIONARY"),(5,"E1"),(330,"B1"),(3,"ACAD_FILTER"),(360,"E2"),
             (0,"DICTIONARY"),(5,"E2"),(330,"E1"),(3,"SPATIAL"),(360,"E3"),
             (0,"SPATIAL_FILTER"),(5,"E3"),(330,"E2"),(70,2),(10,0),(20,-right),(10,3),(20,0),
             *zip((210,220,230), (0,0,1) if bad_normal else normal),
             *zip((11,21,31),clip_origin),(71,1),*((40,val) for val in original_inverse+boundary_matrix)]
    tags = section("BLOCKS",block)+section("ENTITIES",root)+section("OBJECTS",clips)+[(0,"EOF")]
    return "".join(f"{c}\n{v}\n" for c,v in tags).encode()


def world_area(normal,scale,right):
    u,v = basis(normal)
    def world(x,y):
        return tuple(a*(5+scale[0]*x)+b*(8+scale[1]*y)+c for a,b,c in zip(u,v,normal))
    polygon = [world(0,0),world(4,0),world(0,4)]
    # Intersect the world triangle with four half planes perpendicular to its
    # surface. Then project its 3D vector area onto its known unit normal.
    for axis,low,high in ((u,0,right*scale[0]),(v,0,3*scale[1])):
        start = dot(world(0,0),axis)
        for limit,sign in ((min(low,high)+start,1),(max(low,high)+start,-1)):
            clipped = []
            for a,b in zip(polygon,polygon[1:]+polygon[:1]):
                da,db = sign*(dot(a,axis)-limit),sign*(dot(b,axis)-limit)
                if (da>=0) != (db>=0):
                    clipped.append(tuple(x+da/(da-db)*(y-x) for x,y in zip(a,b)))
                if db>=0:
                    clipped.append(b)
            polygon = clipped
    vector = [sum(a[(i+1)%3]*b[(i+2)%3]-a[(i+2)%3]*b[(i+1)%3]
                  for a,b in zip(polygon,polygon[1:]+polygon[:1])) for i in range(3)]
    return abs(dot(vector,normal))/2


@pytest.mark.parametrize("ask,normal,scale,right", ASKS)
def test_retained_surface_area(ask,normal,scale,right):
    _, source, _ = tools_fallback.author_tool(ask)
    status,payload = invoke(source,drawing(normal,scale,right))
    assert status == "ok", payload
    expected = world_area(normal,scale,right)
    assert expected > 0
    assert payload[0]["visible_panels"] == 1
    assert payload[0]["area_by_layer"] == pytest.approx({"Roof":expected},rel=1e-12)


@pytest.mark.parametrize("offset,bad_normal", [(1,False),(0,True)])
def test_inconsistent_or_noncoplanar_clip_refused(offset,bad_normal):
    ask,normal,scale,right = ASKS[0]
    _, source, _ = tools_fallback.author_tool(ask)
    status,payload = invoke(source,drawing(normal,scale,right,offset,bad_normal))
    assert status == "tool_error", payload
    assert "coplanar" in str(payload) or "conflicts" in str(payload)
