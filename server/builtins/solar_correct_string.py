"""Solar corrections produce one graph candidate for the existing write lane."""
from solar_solve_results import correct_graph


def run(intake, params):
    return correct_graph(intake, params)
