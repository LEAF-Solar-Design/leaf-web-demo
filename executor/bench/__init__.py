"""Instant-execution benchmark harness and its result-contract validator.

This ``__init__.py`` also anchors ``bench/tests`` under a unique package name
for pytest: without it, ``bench/tests`` and the sibling ``executor/tests``
both resolve to a TOP-LEVEL package called ``tests`` (their parents are not
packages, so pytest's package-root walk stops at each ``tests/__init__.py``),
and whichever is collected first claims the name — co-collecting the whole
executor tree then fails to import the other (``No module named
'tests.test_bootstrap'``). With this file, the bench tests import as
``bench.tests.*`` and the collision is gone.
"""
