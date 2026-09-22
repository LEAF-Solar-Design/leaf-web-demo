"""Leaf Automation solar proposal capability. Execution belongs to the broker."""


def run(intake, params):
    # A direct tool-loader invocation must never synthesize a local solution.
    raise RuntimeError("solar-solve-proposal requires the cloud broker")
