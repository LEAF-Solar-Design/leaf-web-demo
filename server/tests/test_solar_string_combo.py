"""The C# unit cases for StringComboFunc, run against the Python port.

Every case below is one of the plugin's own NUnit cases in
`Tests/Tests/StringComboCalculatorTests.cs` of the Branch2025 reference
checkout, kept in the same order and under the same names, plus the case the
licensed capture actually exercised: MULTISTRING over 27 freed panels at a
target of 14 cuts one string of 14 and one of 13.

One divergence, and it is the C# file's, not the port's. `PanelsLessThanTarget`
asserts that 15/10 conserves its panel count; the function it tests cannot,
because neither pass has a combination for 10 panels at that target, so it
returns the zero sentinel. The sentinel is the plugin's own documented answer
there (`BranchCmd.cs:17396` cancels MULTISTRING on it; `BranchCmdCore.cs:308`
writes the INFEASIBLE label from it), so this file pins the sentinel and says
why rather than porting an assertion the source cannot satisfy.
"""

from pathlib import Path
import sys

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

from solar_string_combo import (
    MAX_PANELS, MAX_TARGET, SENTINEL, StringComboError, is_sentinel, sequences, string_combo,
)


def conserved(target, panels):
    """The C# ConservationLaw: lengths times quantities is the panel count."""
    combo = string_combo(target, panels)
    return combo[0][0] * combo[1][0] + combo[0][1] * combo[1][1]


def test_the_captured_multistring_case_cuts_fourteen_and_thirteen():
    """receipts/w2-string-multi-add-20260922: 27 freed panels at target 14 -> 14 + 13.

    The licensed plugin committed exactly two circuits, of 14 and 13 panels, which
    is this combination and no other: spread is 2, so pass 1 lands at i = 1.
    """
    assert string_combo(14, 27) == [[14, 13], [1, 1]]
    assert sequences(14, 27) == [14, 13, 1, 1]
    assert conserved(14, 27) == 27


def test_exact_multiple():
    # C# ExactMultiple: 45/15 = 3 exactly, so 15*3 + 14*0 = 45.
    assert string_combo(15, 45) == [[15, 14], [3, 0]]


def test_one_short():
    # C# OneShort: spread 3, 15*2 + 14*1 = 44.
    assert string_combo(15, 44) == [[15, 14], [2, 1]]


def test_two_short():
    # C# TwoShort: spread 3, 15*1 + 14*2 = 43.
    assert string_combo(15, 43) == [[15, 14], [1, 2]]


def test_all_secondary():
    # C# AllSecondary: spread 3, 15*0 + 14*3 = 42.
    assert string_combo(15, 42) == [[15, 14], [0, 3]]
    # 42 is also 14*3 + 13*0, which is pass 2's very first candidate. Pass 1 runs
    # first and wins, so the order of the two passes is observable here.
    assert string_combo(15, 42) != [[14, 13], [3, 0]]


def test_falls_to_pass_two():
    # C# FallsToPass2: pass 1 spans 42..45 at spread 3 and cannot make 41; pass 2
    # reuses that same spread and lands at 14*2 + 13*1.
    assert string_combo(15, 41) == [[14, 13], [2, 1]]
    assert conserved(15, 41) == 41


def test_panels_fewer_than_the_target_return_the_sentinel():
    """C# PanelsLessThanTarget, corrected to what the ported source does.

    Spread is 1, so pass 1 can only make 14 or 15 and pass 2 only 13 or 14.
    Ten panels are unreachable, and the function answers with the zero sentinel
    the plugin cancels MULTISTRING on.
    """
    assert string_combo(15, 10) == SENTINEL
    assert is_sentinel(string_combo(15, 10))
    assert conserved(15, 10) == 0


def test_large_panel_count():
    # C# LargePanelCount: spread 20, and 20 strings of 15 is exactly 300.
    assert string_combo(15, 300) == [[15, 14], [20, 0]]
    assert conserved(15, 300) == 300


def test_a_target_of_one_uses_a_zero_second_length():
    # C# StringLen1: the second length is target-1, which is 0 here, so the
    # quantity beside it is 0 too and five single-panel strings carry the count.
    assert string_combo(1, 5) == [[1, 0], [5, 0]]
    assert conserved(1, 5) == 5


def test_panels_equal_to_the_target():
    # C# PanelsEqualsTarget: one string of 15.
    combo = string_combo(15, 15)
    assert combo[0][0] == 15 and combo[1][0] == 1
    assert combo == [[15, 14], [1, 0]]


def test_exact_multiple_small():
    # C# ExactMultipleSmall.
    assert string_combo(3, 9) == [[3, 2], [3, 0]]


def test_odd_panel_count():
    # C# OddPanelCount: spread 4, 12*3 + 11*1 = 47.
    assert string_combo(12, 47) == [[12, 11], [3, 1]]
    assert conserved(12, 47) == 47


@pytest.mark.parametrize("target,panels", [
    # The C# [TestCase] rows of ConservationLaw, verbatim.
    (15, 45), (15, 44), (15, 43), (15, 42), (15, 41),
    (12, 100), (10, 77), (8, 48), (3, 9), (15, 300),
])
def test_conservation_law(target, panels):
    combo = string_combo(target, panels)
    assert not is_sentinel(combo)
    assert conserved(target, panels) == panels
    # A quantity never rides a zero length: that is the shape the wire contract
    # (MatrixJson.coherent_matrix) refuses.
    assert all(length > 0 or count == 0 for length, count in zip(combo[0], combo[1]))


def test_sequences_flattens_in_the_plugins_order():
    # BranchCmd.cs:17446 writes [firstLen, secondLen, firstQ, secondQ] into the request.
    combo = string_combo(12, 100)
    assert sequences(12, 100) == [combo[0][0], combo[0][1], combo[1][0], combo[1][1]]
    assert sequences(12, 100) == [12, 11, 1, 8]


def test_sequences_conserves_the_panel_count():
    first_length, second_length, first_quantity, second_quantity = sequences(10, 77)
    assert first_length * first_quantity + second_length * second_quantity == 77


def test_the_sentinel_is_named_by_the_plugins_own_test():
    # BranchCmd.cs:17396 tests firstLen and firstQ only, so that is what is_sentinel does.
    assert is_sentinel(SENTINEL)
    assert is_sentinel([[0, 7], [0, 3]])
    assert not is_sentinel([[15, 14], [1, 0]])
    assert not is_sentinel([[15, 0], [1, 0]])


def test_a_zero_panel_count_needs_no_strings():
    # Spread is 0, so pass 1's only candidate is the empty sum, which matches.
    assert string_combo(15, 0) == [[15, 14], [0, 0]]
    assert not is_sentinel(string_combo(15, 0))


def test_the_result_is_a_fresh_list_each_call():
    """The module constant is never handed out, so a caller cannot poison the next."""
    first = string_combo(15, 10)
    first[0][0] = 99
    assert SENTINEL == [[0, 0], [0, 0]]
    assert string_combo(15, 10) == [[0, 0], [0, 0]]


@pytest.mark.parametrize("target,panels", [
    (0, 10), (-1, 10), (MAX_TARGET + 1, 10),
    (15, -1), (15, MAX_PANELS + 1),
    (15.0, 10), (15, 10.0), (True, 10),
])
def test_arguments_that_are_not_two_ints_in_range_are_refused(target, panels):
    with pytest.raises(StringComboError):
        string_combo(target, panels)
    with pytest.raises(StringComboError):
        sequences(target, panels)
