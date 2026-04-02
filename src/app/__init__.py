"""Application entrypoint package."""

from .cli import main
from .bootstrap import setup_workspace

__all__ = ["main", "setup_workspace"]
