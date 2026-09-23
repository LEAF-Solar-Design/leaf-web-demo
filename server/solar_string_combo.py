"""Branch2025's string combination calculator, ported literally.

Source: `BranchCmd.StringComboCalculator.StringComboFunc` in the reference
checkout at `LeafSolarDesign.Core/BranchCmdCore.cs`, class 3161-3226 and the
function itself at 3173-3226. It answers one question: given a TARGET string
length and a panel count, which two adjacent lengths, and how many of each,
add up to exactly that count?

Two passes, in this order and no other:

* pass 1 tries `target` against `target - 1`,
* pass 2, only when pass 1 found nothing, tries `target - 1` against
  `target - 2`,

both over `spread = ceil(totalPanels / stringNumTarget)` strings, and both
taking the FIRST `i` that lands on the count exactly. When neither pass lands,
the zero sentinel `[[0, 0], [0, 0]]` comes back unchanged, which the plugin
itself reads as "no valid combo": `BranchCmd.cs:17396` cancels MULTISTRING on
it, and `BranchCmdCore.cs:308` writes the INFEASIBLE group label from it.

The callers that matter here are `BranchCmd.cs:17373` (MULTISTRING, through
`SolveManualStringAsync`) and `BranchCmd.cs:17446`, which flattens the result
into the solver request as `Sequences = [firstLen, secondLen, firstQ, secondQ]`.
`sequences()` below is that one line.

Pure and bounded: no I/O, no allocation per candidate beyond the loop counter,
and at most `spread + 1` iterations per pass, with `spread <= total_panels`
because the inputs are range-checked first. Fails closed on anything that is
not a plain int in range.
"""
from __future__ import annotations

# The graph contract caps a string at 900 modules (leaf_cloud_client.Panel.Seq),
# and no drawing this ports for carries more panels than a bounded selection.
# Both bounds exist to keep the two loops below finite on hostile input.
MAX_TARGET = 900
MAX_PANELS = 90000

SENTINEL = [[0, 0], [0, 0]]


class StringComboError(ValueError):
    """A bounded, payload-free refusal: the arguments are not two ints in range."""


def _checked(string_num_target: int, total_panels: int) -> None:
    # C# takes two ints and divides by the target, so a zero or negative target is
    # a caller error there too: BranchCmdCore.cs:276 guards it before calling.
    if type(string_num_target) is not int or not 1 <= string_num_target <= MAX_TARGET:
        raise StringComboError("string length target must be an integer in 1..%d" % MAX_TARGET)
    if type(total_panels) is not int or not 0 <= total_panels <= MAX_PANELS:
        raise StringComboError("panel count must be an integer in 0..%d" % MAX_PANELS)


def string_combo(string_num_target: int, total_panels: int) -> list[list[int]]:
    """StringComboFunc: `[[firstLen, secondLen], [firstQ, secondQ]]`, or the sentinel.

    A literal port, including the details that look like accidents and are not:
    the quantities are attached to the lengths of the pass that matched, pass 2
    reuses pass 1's `spread` rather than recomputing it, and a count that neither
    pass can express comes back as `[[0, 0], [0, 0]]` rather than an exception.
    """
    _checked(string_num_target, total_panels)
    num_p1 = string_num_target
    num_p2 = string_num_target - 1
    num_p3 = string_num_target - 2
    # C#: (int)Math.Ceiling((double)totalPanels / stringNumTarget), both non-negative.
    spread = -(-total_panels // string_num_target)

    string_combos = [[0, 0], [0, 0]]

    for i in range(spread + 1):
        if i * num_p1 + (spread - i) * num_p2 == total_panels:
            string_combos[0][0] = num_p1
            string_combos[0][1] = num_p2
            string_combos[1][0] = i
            string_combos[1][1] = spread - i
            return string_combos

    for i in range(spread + 1):
        if i * num_p2 + (spread - i) * num_p3 == total_panels:
            string_combos[0][0] = num_p2
            string_combos[0][1] = num_p3
            string_combos[1][0] = i
            string_combos[1][1] = spread - i
            break

    return string_combos


def is_sentinel(combo: list[list[int]]) -> bool:
    """The plugin's own test for "no valid combo" (BranchCmd.cs:17396: firstLen and firstQ)."""
    return combo[0][0] == 0 and combo[1][0] == 0


def sequences(string_num_target: int, total_panels: int) -> list[int]:
    """BranchCmd.cs:17446 flattened: `[firstLen, secondLen, firstQ, secondQ]`.

    The solver request carries exactly this list, so the conservation the combo
    guarantees (lengths times quantities equals the panel count) is also what the
    wire contract checks in `MatrixJson.coherent_matrix`.
    """
    combo = string_combo(string_num_target, total_panels)
    return [combo[0][0], combo[0][1], combo[1][0], combo[1][1]]
