"""Affine block placement primitives to embed in sandboxed drawing tools."""

TRANSFORM_SOURCE = """
def identity_frame():
    return ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1))


def transform_vector(frame, point):
    return tuple(sum(frame[j+1][i] * point[j] for j in range(3)) for i in range(3))


def transform_point(frame, point):
    vector = transform_vector(frame, point)
    return tuple(frame[0][i] + vector[i] for i in range(3))


def unit_vector(vector):
    size = math.hypot(*vector)
    if not size or not math.isfinite(size):
        raise ValueError("Invalid block extrusion normal.")
    return tuple(component / size for component in vector)


def extrusion_axes(normal):
    nx, ny, nz = unit_vector(normal)
    horizontal = (nz, 0, -nx) if abs(nx) < 1/64 and abs(ny) < 1/64 else (-ny, nx, 0)
    ax, ay, az = unit_vector(horizontal)
    return ((ax, ay, az), (ny*az - nz*ay, nz*ax - nx*az, nx*ay - ny*ax), (nx, ny, nz))


def insert_frame(parent, insert, base, row=0, column=0):
    x_axis, y_axis, z_axis = extrusion_axes(insert.get("nrm", (0, 0, 1)))
    angle = insert.get("rot", 0)
    cosine, sine = math.cos(angle), math.sin(angle)
    rx = tuple(cosine*x_axis[i] + sine*y_axis[i] for i in range(3))
    ry = tuple(-sine*x_axis[i] + cosine*y_axis[i] for i in range(3))
    sx, sy, sz = insert.get("scale", (1, 1, 1))
    # INSERT positions arrive in the parent block's Cartesian frame. Array
    # spacing follows the rotated OCS axes, independent of symbol scaling.
    dx = column*insert.get("column_spacing", 0) - base[0]*sx
    dy = row*insert.get("row_spacing", 0) - base[1]*sy
    local_origin = tuple(insert.get(key, 0) + dx*rx[i] + dy*ry[i] - base[2]*sz*z_axis[i]
                         for i, key in enumerate(("x", "y", "z")))
    origin = transform_point(parent, local_origin)
    axes = tuple(transform_vector(parent, tuple(value*scale for value in axis))
                 for axis, scale in ((rx, sx), (ry, sy), (z_axis, sz)))
    return (origin, *axes)
"""
