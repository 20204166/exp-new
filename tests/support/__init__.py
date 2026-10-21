"""Reusable test-support modules shared across the unittest suite.

Every factory returns fresh objects so tests stay isolated and safe for
sequential and (where practical) parallel execution. Support modules must
never be discovered as tests: their names do not match ``test*.py``.
"""
