"""Compare plot-style names. Running a plot requires AutoCAD."""

from .x12_evidence import compare, indexed, name


def _style(row, path):
    return {"plot_style_table": name(row, "plot_style_table", path)}


def compare_plot_styles(before, after):
    if before == []:
        raise ValueError("before.layouts: no plot-style evidence")
    old = indexed(before, "before.layouts", "name", _style)
    new = indexed(after, "after.layouts", "name", _style)
    return compare(old, new, "layouts", ("plot_style_table",))
