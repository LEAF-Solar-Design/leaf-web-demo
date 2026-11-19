"""Read planar block-reference clipping from the DXF extension dictionary chain."""
import math


def _extension(tags):
    for i, tag in enumerate(tags):
        if tag == (102, "{ACAD_XDICTIONARY"):
            if i+2 >= len(tags) or tags[i+1][0] != 360 or tags[i+2] != (102, "}"):
                raise ValueError("Malformed clip extension dictionary.")
            return tags[i+1][1].upper()
    return None


def _points(tags):
    points = []
    for code, value in tags:
        if code == 10:
            points.append([float(value)])
        elif code in (20, 30):
            if not points or len(points[-1]) != code // 10 - 1:
                raise ValueError("Incomplete clip coordinate.")
            points[-1].append(float(value))
    if (len(points) < 2 or any(len(p) < 2 or not all(map(math.isfinite, p))
                               or (len(p) == 3 and p[2] != 0) for p in points)):
        raise ValueError("Invalid planar clip coordinates.")
    return [tuple(p[:2]) for p in points]


def _polygon(points, matrix):
    if len(points) == 2:
        (x, y), (u, v) = points
        points = [(x, y), (u, y), (u, v), (x, v)]
    else:
        end = len(points)
        while end > 1 and points[end-1] == points[0]:
            end -= 1
        points = points[:end]
    if len(points) < 3:
        raise ValueError("Invalid clip polygon.")
    a, b, _, dx, c, d, _, dy = matrix[:8]
    result = [(a*x+b*y+dx, c*x+d*y+dy) for x, y in points]
    if not all(math.isfinite(v) for p in result for v in p):
        raise ValueError("Non-finite clip boundary.")
    # A simple boundary cannot contain repeated vertices, touching or crossing
    # non-neighbour edges. Bound quadratic validation on uploaded geometry.
    if len(result) > 512 or len(set(result)) != len(result):
        raise ValueError("Invalid or oversized clip boundary.")
    def orient(p, q, r):
        return (q[0]-p[0])*(r[1]-p[1]) - (q[1]-p[1])*(r[0]-p[0])
    def overlaps(p, q, r, s):
        return max(min(p,q), min(r,s)) <= min(max(p,q), max(r,s))
    # Remove only intermediate points on straight edges. A reversal is
    # overlapping boundary geometry, even if its vertices are distinct.
    corners = []
    for i, q in enumerate(result):
        p, r = result[i-1], result[(i+1) % len(result)]
        if orient(p,q,r) != 0:
            corners.append(q)
        elif not all(min(a,b) <= v <= max(a,b) for a,b,v in zip(p,r,q)):
            raise ValueError("Degenerate or retraced clip boundary.")
    if len(corners) < 3:
        raise ValueError("Degenerate clip boundary.")
    result = corners
    edges = list(zip(result, result[1:]+result[:1]))
    if not any(orient(result[0], p, q) for p, q in edges):
        raise ValueError("Degenerate clip boundary.")
    for i, (p, q) in enumerate(edges):
        for j in range(i+2, len(edges)):
            if i == 0 and j == len(edges)-1:
                continue
            r, s = edges[j]
            if (overlaps(p[0], q[0], r[0], s[0]) and overlaps(p[1], q[1], r[1], s[1])
                    and orient(p,q,r)*orient(p,q,s) <= 0 and orient(r,s,p)*orient(r,s,q) <= 0):
                raise ValueError("Self-intersecting clip boundary.")
    return result


def _boundary_to_block(matrices, fields):
    # The first matrix maps original world coordinates into the block. The
    # second maps world coordinates into the coordinate system of the boundary.
    block = [matrices[i:i+4] for i in (0,4,8)]
    clip = [matrices[i:i+4] for i in (12,16,20)]
    def dot(a, b):
        return math.fsum(x*y for x,y in zip(a,b))
    axes = [row[:3] for row in clip]
    for i,a in enumerate(axes):
        for j,b in enumerate(axes):
            if not math.isclose(dot(a,b), float(i == j), abs_tol=1e-12):
                raise ValueError("Unresolved clip coordinate frame.")
    normal = [float(fields.get(k,v)) for k,v in ((210,0),(220,0),(230,1))]
    size = math.hypot(*normal)
    if not size or not math.isfinite(size) or math.dist([v/size for v in normal], axes[2]) > 1e-9:
        raise ValueError("Clip normal conflicts with its coordinate frame.")
    origin = [float(fields.get(k,0)) for k in (11,21,31)]
    if any(not math.isclose(dot(row,origin) + row[3], 0., abs_tol=1e-9)
           for row in clip):
        raise ValueError("Clip origin conflicts with its coordinate frame.")
    # Invert the orthonormal boundary axes, then apply the historical INSERT inverse.
    rows = [[dot(row, axis) for axis in axes[:2]] + [0, dot(row,origin) + row[3]] for row in block]
    for vector,offset in ((axes[0],0), (axes[1],0), (origin,block[2][3])):
        terms = [a*b for a,b in zip(block[2],vector)] + [offset]
        if abs(math.fsum(terms)) > 16*math.ulp(1.)*math.fsum(abs(term) for term in terms):
            raise ValueError("Clip and block footprint planes are not coplanar.")
    return rows[0] + rows[1]


def _read_clip(owner, extension, objects):
    def obj(handle, kind, parent):
        found, tags = objects.get(handle, (None, []))
        # Persistent reactors can contribute 330 before the actual owner.
        owners = [v.upper() for c, v in tags if c == 330]
        if found != kind or not owners or owners[-1] != parent:
            raise ValueError("Unresolved clip object or incorrect owner.")
        return tags

    def entry(handle, parent, key):
        tags = obj(handle, "DICTIONARY", parent)
        indices = [i for i, tag in enumerate(tags) if tag == (3, key)]
        if not indices:
            return None
        if len(indices) != 1:
            raise ValueError("Ambiguous clip dictionary entry.")
        i = indices[0]
        if i+1 >= len(tags) or tags[i+1][0] not in (350, 360):
            raise ValueError("Invalid clip dictionary entry.")
        return tags[i+1][1].upper()

    sub = entry(extension, owner, "ACAD_FILTER")
    if sub is None:
        return None
    spatial = entry(sub, extension, "SPATIAL")
    if spatial is None:
        raise ValueError("Missing spatial clip dictionary entry.")
    tags = obj(spatial, "SPATIAL_FILTER", sub)
    fields = dict(tags)
    enabled = int(fields.get(71, 1))
    if enabled == 0:
        return None
    if enabled != 1 or int(fields.get(72, 0)) or int(fields.get(73, 0)):
        raise ValueError("Unsupported nonplanar clip filter.")
    points = _points(tags)
    matrices = [float(v) for c, v in tags if c == 40]
    if (len(points) != int(fields.get(70, -1)) or len(matrices) != 24
            or not all(map(math.isfinite, matrices))):
        raise ValueError("Invalid clip vertex count or transformation.")
    inverted = False
    ext = _extension(tags)
    if ext:
        handle = entry(ext, spatial, "ACAD_XREC_ROUNDTRIP")
        if handle:
            sections = {}
            current = ""
            for code, val in obj(handle, "XRECORD", ext):
                if code == 102:
                    current = val
                else:
                    sections.setdefault(current, []).append((code,val))
            key = "ACAD_INVERTEDCLIP_ROUNDTRIP"
            if key in sections or key+"_COMPARE" in sections:
                if _points(sections.get(key+"_COMPARE", [])) != points:
                    raise ValueError("Inverted clip compare boundary disagrees with the spatial filter.")
                points = _points(sections.get(key, []))
                inverted = True
    if int(fields.get(290, int(inverted))) != int(inverted):
        raise ValueError("Unresolved clip inversion flag.")
    return {"points": _polygon(points, _boundary_to_block(matrices, fields)), "inverted": inverted}


def attach_clips(pairs, out):
    if not out.get("inserts"):
        return
    clips = out["insert_clips"] = {}
    if (102, "{ACAD_XDICTIONARY") not in pairs:
        return
    objects, references = {}, {}
    section = ""
    i = 0
    while i < len(pairs):
        code, kind = pairs[i]
        end = i + 1
        while end < len(pairs) and pairs[end][0] != 0:
            end += 1
        tags = pairs[i+1:end]
        if code == 0:
            if kind == "SECTION":
                section = dict(tags).get(2, "")
            elif kind == "ENDSEC":
                section = ""
            elif section == "OBJECTS" and kind in ("DICTIONARY", "SPATIAL_FILTER", "XRECORD"):
                objects[dict(tags).get(5, "").upper()] = (kind, tags)
            elif section in ("ENTITIES", "BLOCKS") and kind == "INSERT":
                handle = dict(tags).get(5, "").upper()
                try:
                    ext = _extension(tags)
                    if ext:
                        references[handle] = ext
                except ValueError as exc:
                    clips[handle] = {"error": str(exc)}
        i = end
    for handle, ext in references.items():
        try:
            clip = _read_clip(handle, ext, objects)
            if clip:
                clips[handle] = clip
        except (TypeError, ValueError, OverflowError) as exc:
            clips[handle] = {"error": "Invalid clip: " + str(exc)}
