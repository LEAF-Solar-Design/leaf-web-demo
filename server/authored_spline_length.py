"""Source embedded in the offline tool for measuring positive-weight B-splines."""

SPLINE_LENGTH_SOURCE = """
def charge_spline_work(budget, count):
    # One evaluation blends two homogeneous control points or projects one pole.
    if count > budget[0]:
        raise ValueError("Spline length did not converge within the evaluation budget.")
    budget[0] -= count

def spline_blend(a, b, fraction):
    return tuple((1 - fraction)*x + fraction*y for x, y in zip(a, b))


def bezier_span(control, knots, degree, span, budget):
    # Polar form of de Boor evaluation. Choosing each span endpoint j times
    # gives the j-th homogeneous Bezier control of this knot interval.
    left, right = knots[span], knots[span + 1]
    charge_spline_work(budget, degree * (degree + 1)**2 // 2)
    result = []
    for j in range(degree + 1):
        net = control[span-degree:span+1]
        for r in range(1, degree + 1):
            u = left if r <= degree - j else right
            for i in range(degree, r - 1, -1):
                k = span - degree + i
                lo, hi = knots[k], knots[k + degree - r + 1]
                width = hi - lo
                if math.isfinite(width):
                    alpha = (u - lo) / width
                else:
                    # Opposite-sign finite knots can span over one float's range.
                    alpha = (.5*u - .5*lo) / (.5*hi - .5*lo)
                net[i] = spline_blend(net[i-1], net[i], alpha)
        result.append(net[-1])
    return result


def split_bezier(net):
    left, right = [net[0]], [net[-1]]
    while len(net) > 1:
        net = [spline_blend(a, b, .5) for a, b in zip(net, net[1:])]
        left.append(net[0])
        right.append(net[-1])
    return left, right[::-1]


def bezier_length(net, budget):
    # Positive rational Bezier curves are variation diminishing. Their
    # endpoint chord and projected control polygon enclose the arc length.
    pieces = [net]
    while len(pieces) <= 16384:
        charge_spline_work(budget, len(pieces) * len(net))
        lower, upper = 0.0, 0.0
        for piece in pieces:
            points = [tuple(x / p[3] for x in p[:3]) for p in piece]
            lower += math.dist(points[0], points[-1])
            upper += sum(math.dist(a, b) for a, b in zip(points, points[1:]))
        if upper - lower <= 2 * LENGTH_RELATIVE_TOLERANCE * lower:
            return (lower + upper) / 2
        if len(pieces) == 16384:
            break
        charge_spline_work(budget, len(pieces) * len(net) * (len(net)-1) // 2)
        pieces = [half for piece in pieces for half in split_bezier(piece)]
    raise ValueError("Spline beam length did not converge to the reported tolerance.")


def spline_jump(points, knots, degree, k, frame):
    # At a degree+1 interior knot the two one-sided limits coincide with
    # adjacent controls. Lower multiplicities guarantee positional continuity.
    if knots[k-degree] != knots[k]:
        return 0.0
    left, right = points[k-degree-1], points[k-degree]
    return math.hypot(*transform_vector(frame, tuple(b-a for a, b in zip(left, right))))


def spline_endpoints(path, frame, budget, require_continuity=False):
    degree, points, knots = path["degree"], path["control_points"], path["knots"]
    weights = path["weights"]
    scale = max(weights)
    net = [(*[v*(w/scale) for v in p], w/scale) for p, w in zip(points, weights)]
    spans = [k for k in range(degree, len(points)) if knots[k] < knots[k+1]]
    # A fully multiple interior knot can break a single entity into disjoint spans.
    for k in spans[1:]:
        if require_continuity and spline_jump(points, knots, degree, k, frame):
            raise ValueError("Discontinuous knot sequence within one spline segment.")
    ends = []
    for k, index in ((spans[0], 0), (spans[-1], -1)):
        charge_spline_work(budget, 1)
        p = bezier_span(net, knots, degree, k, budget)[index]
        end = transform_point(frame, tuple(x/p[3] for x in p[:3]))
        if not all(math.isfinite(x) for x in end):
            raise ValueError("Unresolved transformed spline endpoint.")
        ends.append(end)
    return ends


def spline_length(path, frame, factor, detail, budget):
    if path.get("spline_error"):
        raise ValueError(path["spline_error"])
    degree = path["degree"]
    knots = path["knots"]
    weights = path["weights"]
    points = path["control_points"]
    # Work relative to the first point so placement translations and large
    # coordinate offsets do not enter the length calculation.
    control = []
    scale = max(weights)
    for point, weight in zip(points, weights):
        local = tuple(x - base for x, base in zip(point, points[0]))
        vector = transform_vector(frame, local)
        w = weight / scale
        if w < 2.2250738585072014e-308:
            raise ValueError("Spline length did not converge: normalized weight below numeric precision.")
        control.append((*[x*w for x in vector], w))
    total = 0.0
    spans, jumps = [], []
    for k in range(degree, len(points)):
        if knots[k] == knots[k+1]:
            continue
        length = bezier_length(bezier_span(control, knots, degree, k, budget), budget)
        total += length
        if detail:
            if spans:
                distance = spline_jump(points, knots, degree, k, frame)
                if distance:
                    jumps.append({"knot": knots[k], "distance": distance*factor})
            spans.append({"interval": [knots[k], knots[k+1]], "length": length*factor})
    if total == 0 and any(point != points[0] for point in points):
        raise ValueError("Collapsed spline placement has no measurable beam length.")
    return total, {"spans": spans, "jumps": jumps}
"""
