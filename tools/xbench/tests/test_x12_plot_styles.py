import pytest

from tools.xbench.x12_plot_styles import compare_plot_styles


@pytest.fixture
def plots():
    return [{"name": "A1", "plot_style_table": "solar.ctb"}]


def test_equal_plot_styles(plots):
    assert compare_plot_styles(plots, plots.copy()) == []


def test_missing_layout(plots):
    assert compare_plot_styles(plots, []) == ["layouts['A1']: missing"]


def test_ctb_to_stb_is_a_change(plots):
    issue, = compare_plot_styles(plots, [{"name": "A1", "plot_style_table": "solar.stb"}])
    assert all(word in issue for word in ("plot_style_table", "solar.ctb", "solar.stb"))


@pytest.mark.parametrize("after", [None, [{"name": "A1"}], [{"name": "A1", "plot_style_table": ""}]])
def test_absent_plot_evidence(plots, after):
    with pytest.raises(ValueError, match="after.layouts"):
        compare_plot_styles(plots, after)


def test_absent_original_plot_evidence():
    with pytest.raises(ValueError, match="no plot-style evidence"):
        compare_plot_styles([], [])
