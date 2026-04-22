"""
Convert a SWE-bench Pro instance into the artifact text expected by the
parser agent.

SWE-bench Pro exposes three fields to the model (see
eval/SWE-bench_Pro-os/helper_code/create_problem_statement.py for the
official format used by SWE-agent):
- ``problem_statement``: the issue description
- ``requirements``: behavioral requirements for the solution
- ``interface``: (optional) class/function names the test suite expects

``FAIL_TO_PASS`` / ``PASS_TO_PASS`` are evaluation-only oracle data and
must NOT be included in the artifact text.
"""

from __future__ import annotations


def instance_to_artifact_text(instance: dict) -> str:
    """Build the artifact text that is fed to the parser agent.

    Follows the official SWE-bench Pro problem statement format from
    eval/SWE-bench_Pro-os/helper_code/create_problem_statement.py.
    Metadata (instance_id, repo, base_commit) is prepended for context.
    Test oracle fields (FAIL_TO_PASS, PASS_TO_PASS) are deliberately excluded.
    """
    ps = instance.get("problem_statement", "").strip()
    iid = instance.get("instance_id", "")
    repo = instance.get("repo", "")
    base_commit = instance.get("base_commit", "")
    requirements = instance.get("requirements", "").strip()
    interface = instance.get("interface", "").strip()

    parts = [
        f"Instance ID: {iid}",
        f"Repository: {repo}",
        f"Base Commit: {base_commit}",
        f"",
        ps,
    ]

    if requirements:
        parts += ["", "Requirements:", requirements]

    if interface:
        parts += ["", "New interfaces introduced:", interface]

    return "\n".join(parts)
