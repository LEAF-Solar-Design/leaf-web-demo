"""Compare original AutoCAD layer rows against exported layer evidence."""

from .x12_evidence import compare, field, indexed, name


def _parse(row, path):
    return {
        "color": field(row, "color", path, lambda v: type(v) is int, "integer CAD color"),
        "linetype": name(row, "linetype", path),
        "plot": field(row, "plot", path, lambda v: type(v) is bool, "boolean"),
    }


def compare_layers(before, after):
    """Report missing original names and changed symbol properties."""
    old = indexed(before, "before.layers", "name", _parse)
    new = indexed(after, "after.layers", "name", _parse)
    return compare(old, new, "layers", ("color", "linetype", "plot"))
