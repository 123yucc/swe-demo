"""External system adapters."""

from .llm_client import (
	create_claude_options,
	create_evidence_extraction_options,
	get_project_cwd,
)

__all__ = [
	"create_claude_options",
	"create_evidence_extraction_options",
	"get_project_cwd",
]
