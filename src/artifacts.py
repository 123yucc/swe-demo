"""
Convert a SWE-bench Pro instance into the artifact text expected by the
parser agent.

SWE-bench Pro only exposes ``problem_statement`` to the model.
``FAIL_TO_PASS`` / ``PASS_TO_PASS`` are evaluation-only oracle data and
must NOT be included in the artifact text.
"""

from __future__ import annotations


def instance_to_artifact_text(instance: dict) -> str:
    """Build the artifact text that is fed to the parser agent.

    Only ``problem_statement`` (plus metadata like instance_id / repo /
    base_commit for context) is visible to the model.  Test oracle fields
    (FAIL_TO_PASS, PASS_TO_PASS) are deliberately excluded.
    """
    ps = instance.get("problem_statement", "").strip()
    iid = instance.get("instance_id", "")
    repo = instance.get("repo", "")
    base_commit = instance.get("base_commit", "")

    return (
        f"=== problem_statement.md ===\n"
        f"# Problem Statement\n"
        f"\n"
        f"**Instance ID:** `{iid}`  \n"
        f"**Repository:** `{repo}`  \n"
        f"**Base Commit:** `{base_commit}`  \n"
        f"\n"
        f"{ps}\n"
    )
