"""Backward-compatible import shim for workflow pipeline."""

from .pipelines.run_repair_workflow import run_repair_workflow

__all__ = ["run_repair_workflow"]
