"""Independent small-graph oracles for constrained rooftop string planning."""
import copy
import itertools
import math

import pytest

from solar_string_partition import plan_partition, validate_partition


def exact_possible(points, minimum, maximum, step):
    """Enumerate permutations and all cuts, independently of planner traversal."""
    for order in itertools.permutations(range(len(points))):
        for cuts in itertools.product((False, True), repeat=max(0, len(order) - 1)):
            paths, current = [], [order[0]]
            for index, cut in enumerate(cuts):
                if cut:
                    paths.append(current)
                    current = []
                current.append(order[index + 1])
            paths.append(current)
            if all(minimum <= len(path) <= maximum and all(
                    math.dist(points[a], points[b]) <= step
                    for a, b in zip(path, path[1:])) for path in paths):
                return True
    return False


@pytest.mark.parametrize("points", [
    [(0, 0), (1, 0), (2, 0), (3, 0)],
    [(0, 0), (1, 0), (-0.5, 0.86), (-0.5, -0.86)],
    [(0, 0), (1, 0), (0, 1), (1, 1), (2, 1)],
    [(0, 0), (1, 0), (10, 0), (11, 0)],
])
@pytest.mark.parametrize("minimum,maximum", [(2, 2), (2, 3), (3, 4), (4, 4)])
def test_partition_matches_independent_permutation_and_cut_oracle(points, minimum, maximum):
    before = copy.deepcopy(points)
    result = plan_partition(list(range(len(points))), points, minimum, maximum, 1.01)
    possible = exact_possible(points, minimum, maximum, 1.01)
    assert result["status"] == ("complete" if possible else "infeasible")
    if possible:
        validate_partition(list(range(len(points))), points, result["paths"], minimum, maximum, 1.01)
    else:
        assert result["paths"] == []
    assert points == before


def test_exhaustive_obstruction_differs_from_budget_exhaustion():
    # A three-leaf star has four panels but no four-panel simple path.
    points = [(0, 0), (1, 0), (-0.5, 0.86), (-0.5, -0.86)]
    exhausted = plan_partition([0, 1, 2, 3], points, 4, 4, 1.01, state_budget=1)
    assert exhausted["status"] == "unresolved"
    assert exhausted["reason"] == "search_budget" and exhausted["paths"] == []
    proved = plan_partition([0, 1, 2, 3], points, 4, 4, 1.01)
    assert proved["status"] == "infeasible"
    assert proved["reason"] == "exhaustive_path_search" and proved["paths"] == []


@pytest.mark.parametrize("paths", [
    [[0, 1], [1, 2]], [[0, 1]], [[0, 3], [1, 2]],
    [[0], [1, 2, 3]], [[0, 1], [2, True]],
])
def test_complete_validator_rejects_wrong_coverage_sizes_or_edges(paths):
    with pytest.raises(ValueError, match="invalid string partition"):
        validate_partition([0, 1, 2, 3], [(0, 0), (1, 0), (2, 0), (3, 0)],
                           paths, 2, 2, 1.01)


def test_empty_and_reordered_inputs_are_immutable_and_deterministic():
    assert plan_partition([], [], 2, 3, 1.0)["status"] == "complete"
    points = [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)]
    order = [4, 3, 2, 1, 0]
    before = copy.deepcopy((order, points))
    first = plan_partition(order, points, 2, 3, 1.01)
    assert first == plan_partition(order, points, 2, 3, 1.01)
    assert (order, points) == before


@pytest.mark.parametrize("minimum,maximum,budget", [(True, 3, 1), (0, 3, 1),
    (4, 3, 1), (2, 3, -1), (2, 3, True)])
def test_invalid_planner_contract_refused(minimum, maximum, budget):
    with pytest.raises(ValueError, match="bounds or budget"):
        plan_partition([0], [(0, 0)], minimum, maximum, 1.0, state_budget=budget)


@pytest.mark.parametrize("paths,message", [
    ([[0, 1], [1, 2]], "coverage"),
    ([[0, 1]], "coverage"),
    ([[0, 1], [2, 4]], "coverage"),
    ([[0], [1, 2, 3]], "size"),
    ([[0, 2], [1, 3]], "adjacency"),
])
def test_independent_validator_rejects_corrupt_complete_paths(paths, message):
    points = [(0, 0), (1, 0), (2, 0), (3, 0)]
    with pytest.raises(ValueError, match=message):
        validate_partition([0, 1, 2, 3], points, paths, 2, 2, 1.01)


def test_anchor_may_be_inside_a_string_and_empty_component_is_complete():
    points = [(0, 0), (-1, 0), (1, 0)]
    found = plan_partition([0, 1, 2], points, 3, 3, 1.01)
    assert found["status"] == "complete"
    assert found["paths"][0][1] == 0
    empty = plan_partition([], [], 1, 2, 1.0)
    assert empty["status"] == "complete" and empty["paths"] == []
