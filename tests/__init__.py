"""Test package.

Makes ``tests`` a regular package so ``tests.support`` is importable from
any consumer (``python -m unittest tests.test_window``) and keeps the
documented ``python -m unittest discover -s tests`` command working.
"""
