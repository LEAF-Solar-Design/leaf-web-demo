"""Compare original text styles against exported font evidence."""

import ntpath

from .x12_evidence import indexed, name


def _font(row, path):
    # Compare the whole CAD path, including directories. CAD uses Windows
    # filenames and accepts both slash spellings.
    return name(row, "font_file", path)


def compare_text_styles(before, after):
    old = indexed(before, "before.text_styles", "name", _font)
    new = indexed(after, "after.text_styles", "name", _font)
    issues = []
    for style, font in old.items():
        path = f"text_styles[{style!r}]"
        if style not in new:
            issues.append(f"{path}: missing")
            continue
        actual = new[style]
        if ntpath.normcase(font) != ntpath.normcase(actual):
            change = "SHX font changed" if any(
                ntpath.splitext(value)[1].lower() == ".shx" for value in (font, actual)
            ) else "font changed"
            issues.append(f"{path}.font_file: {change}. Expected {font!r}, got {actual!r}")
    return issues
