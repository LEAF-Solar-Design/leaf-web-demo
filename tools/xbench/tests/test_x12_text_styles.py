from tools.xbench.x12_text_styles import compare_text_styles


def style(font):
    return {"name": "Notes", "font_file": font}


def test_matching_windows_font_path():
    assert compare_text_styles([style("Fonts/ROMANS.SHX")], [style(r"fonts\romans.shx")]) == []


def test_missing_style():
    assert compare_text_styles([style("romans.shx")], []) == ["text_styles['Notes']: missing"]


def test_shx_substitution_identifies_expected_and_actual():
    issue, = compare_text_styles([style("romans.shx")], [style("simplex.shx")])
    assert all(word in issue for word in ("font_file", "SHX", "romans.shx", "simplex.shx"))


def test_matching_basenames_in_distinct_paths_are_different():
    issue, = compare_text_styles([style("A/Arial.ttf")], [style("B/Arial.ttf")])
    assert "font_file" in issue
