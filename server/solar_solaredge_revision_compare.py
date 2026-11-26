"""Compare verified saved tracking assignments by stable panel identity."""

from typing import NamedTuple

from solar_design_graph import GraphValidationError

PAGE_ITEMS = 250


class Assignment(NamedTuple):
    frame_ref: str
    pdf_matrix: int
    pdf_inverter_id: int
    pdf_string_input: int


def _assignments(record, source_graph):
    frames = {panel: frame["id"] for frame in source_graph["frames"]
              for panel in frame["panel_refs"]}
    assignments = {}
    for row in record["strings"]:
        for panel in row["panel_refs"]:
            frame = frames.get(panel)
            if frame is None:
                raise GraphValidationError("SOLAREDGE_IMPORT_RECORD_INVALID")
            assignments[panel] = Assignment(frame, row["pdf_matrix"], row["pdf_inverter_id"],
                                            row["pdf_string_input"])
    return assignments


def compare(before_record, before_graph, after_record, after_graph, offset):
    """Return one page of changes, with totals independent of the page."""
    before = _assignments(before_record, before_graph)
    after = _assignments(after_record, after_graph)
    counts = dict(retained=0, newly_assigned=0, removed=0, reassigned=0)
    items, changed_frames = [], set()
    total = 0
    for panel in sorted(before.keys() | after.keys()):
        previous, current = before.get(panel), after.get(panel)
        if previous == current:
            counts["retained"] += 1
            continue
        if previous is None:
            change = "newly_assigned"
        elif current is None:
            change = "removed"
        else:
            change = "reassigned"
        counts[change] += 1
        changed_frames.update(assignment.frame_ref for assignment in (previous, current)
                              if assignment is not None)
        if offset <= total < offset + PAGE_ITEMS:
            items.append({"panel_ref": panel, "change": change,
                          "before": previous._asdict() if previous is not None else None,
                          "after": current._asdict() if current is not None else None})
        total += 1
    end = offset + len(items)
    return {"counts": counts, "changed_frame_count": len(changed_frames), "items": items,
            "page": {"offset": offset, "total": total, "returned": len(items),
                     "next_offset": end if end < total else None}}
