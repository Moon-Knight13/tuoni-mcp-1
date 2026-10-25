"""Deterministic native/dispatch workflows against a synthetic Tuoni server.

The scripted client follows returned continuations and template references over
the real in-memory MCP transport. CI checks completion, schema validity, output
bounds, and interface costs without a model or live C2.
"""

from evaluation.portable.runner import run_portable_evaluation

__all__ = ["run_portable_evaluation"]
