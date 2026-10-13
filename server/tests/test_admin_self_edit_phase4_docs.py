"""Lock the future Phase 4 document structure and authorization boundary."""

from pathlib import Path


PHASE4_HEADING = "## Phase 4 — staged rollout specification (not activated)"
REQUIRED_HEADINGS = (
    "### Authorization boundary",
    "### Staging receipt",
    "### Exact deployment identity",
    "### Inline operator gates",
    "### Recovery",
    "### Rollback",
    "### Both UI copies",
    "### Open decisions before implementation or activation",
)
AUTHORIZATION_BOUNDARY = (
    "Deployment is outside the self-edit lane until a separate operator decision. "
    "This specification does not authorize implementation, credentials, activation, "
    "or deployment; live activation requires its own explicit operator yes."
)


def _phase4_section():
    path = Path(__file__).resolve().parents[2] / "docs" / "ADMIN-SELF-EDIT-LANE.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines.count(PHASE4_HEADING) == 1
    start = lines.index(PHASE4_HEADING) + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return lines[start:end]


def test_phase4_spec_has_required_sections():
    lines = _phase4_section()
    positions = []
    for heading in REQUIRED_HEADINGS:
        assert lines.count(heading) == 1, heading
        positions.append(lines.index(heading))
    assert positions == sorted(positions)
    for position in positions:
        end = next(
            (index for index in range(position + 1, len(lines))
             if lines[index].startswith("### ")),
            len(lines),
        )
        assert any(line.strip() for line in lines[position + 1:end]), lines[position]


def test_phase4_spec_keeps_deployment_outside_lane():
    section = "\n".join(_phase4_section())
    paragraphs = section.split("\n\n")
    assert AUTHORIZATION_BOUNDARY in paragraphs
