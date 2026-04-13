"""
Convert a SWE-bench Pro instance into the artifact text expected by the
parser agent.

SWE-bench Pro exposes three fields to the model:
- ``problem_statement``: the issue description
- ``requirements``: behavioral requirements for the solution
- ``interface``: (optional) class/function names the test suite expects

``FAIL_TO_PASS`` / ``PASS_TO_PASS`` are evaluation-only oracle data and
must NOT be included in the artifact text.
"""

from __future__ import annotations


def instance_to_artifact_text(instance: dict) -> str:
    """Build the artifact text that is fed to the parser agent.

    Includes ``problem_statement``, ``requirements``, and ``interface``
    (plus metadata like instance_id / repo / base_commit for context).
    Test oracle fields (FAIL_TO_PASS, PASS_TO_PASS) are deliberately excluded.

    Section names (``=== problem_statement.md ===``, ``=== requirements.md ===``,
    ``=== new_interfaces.md ===``) match those referenced in the parser agent's
    system prompt so that its extraction guidelines apply directly.
    """
    ps = instance.get("problem_statement", "").strip()
    iid = instance.get("instance_id", "")
    repo = instance.get("repo", "")
    base_commit = instance.get("base_commit", "")
    requirements = instance.get("requirements", "").strip()
    interface = instance.get("interface", "").strip()

    parts = [
        f"=== problem_statement.md ===\n"
        f"# Problem Statement\n"
        f"\n"
        f"**Instance ID:** `{iid}`  \n"
        f"**Repository:** `{repo}`  \n"
        f"**Base Commit:** `{base_commit}`  \n"
        f"\n"
        f"{ps}\n",
    ]

    if requirements:
        parts.append(
            f"=== requirements.md ===\n"
            f"# Requirements\n"
            f"\n"
            f"{requirements}\n"
        )

    if interface:
        parts.append(
            f"=== new_interfaces.md ===\n"
            f"# New Interfaces\n"
            f"\n"
            f"{interface}\n"
        )

    return "\n".join(parts)
