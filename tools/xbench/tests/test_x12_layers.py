import pytest

from tools.xbench.x12_layers import compare_layers


def layer(name, color=7, linetype="Continuous", plot=True):
    return dict(name=name, color=color, linetype=linetype, plot=plot)


def test_original_layers_match_by_name():
    original = [layer("Roof"), layer("Grid", 2)]
    assert compare_layers(original, [original[1], layer("Studio-Panels"), original[0]]) == []


@pytest.mark.parametrize("after", [[layer("Grid")], [layer("Rooftop"), layer("Grid")]])
def test_missing_original_cannot_be_replaced_by_merge_or_rename(after):
    issues = compare_layers([layer("Roof"), layer("Grid")], after)
    assert issues == ["layers['Roof']: missing"]


def test_each_changed_field_is_named():
    issues = compare_layers([layer("Roof")], [layer("Roof", 1, "Dashed", False)])
    assert [issue.split(":")[0] for issue in issues] == [
        "layers['Roof'].color", "layers['Roof'].linetype", "layers['Roof'].plot",
    ]


def test_duplicate_layer_names_fail_closed():
    with pytest.raises(ValueError, match=r"after.layers\[1\].name: duplicate"):
        compare_layers([], [layer("Roof"), layer("ROOF")])
