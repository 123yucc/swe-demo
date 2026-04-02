"""CLI entrypoint for dynamic orchestrator workflow."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .run_repair_workflow import run_repair_workflow
from .workers import registry as agent_config


def setup_workspace(instance_id: str, base_dir: str = "workdir") -> str:
    """Create and return workspace directory for one instance."""
    workspace = Path(base_dir) / instance_id
    workspace.mkdir(parents=True, exist_ok=True)

    (workspace / "artifacts").mkdir(exist_ok=True)
    (workspace / "artifacts" / "tests" / "fail2pass").mkdir(parents=True, exist_ok=True)
    (workspace / "artifacts" / "tests" / "pass2pass").mkdir(parents=True, exist_ok=True)
    (workspace / "evidence").mkdir(exist_ok=True)
    (workspace / "evidence" / "card_versions").mkdir(parents=True, exist_ok=True)
    (workspace / "repo").mkdir(exist_ok=True)

    (workspace / ".memory" / "longterm").mkdir(parents=True, exist_ok=True)
    (workspace / ".workflow").mkdir(parents=True, exist_ok=True)
    (workspace / "logs").mkdir(exist_ok=True)
    (workspace / "plan").mkdir(exist_ok=True)

    return str(workspace)


async def run_cli(
    instance_id: str,
    workspace_dir: str,
    phase1_only: bool = False,
    phase2_only: bool = False,
) -> None:
    """Run CLI wrapper for phase1/phase2 evidence workflow."""
    print(f"Running Dynamic Workflow for instance {instance_id}...")
    print(f"Workspace: {workspace_dir}")

    prompt = agent_config.MAIN_PROMPT_TEMPLATE.format(instance_id=instance_id)
    if phase1_only:
        prompt += agent_config.PHASE1_ONLY_SUFFIX
        print("Executing Phase 1 only.")
    elif phase2_only:
        prompt += agent_config.PHASE2_ONLY_SUFFIX
        print("Executing Phase 2 only.")
    else:
        print("Executing full workflow (Phase 1 and 2).")

    print("\n" + "=" * 60)
    print("DYNAMIC ORCHESTRATION STARTING")
    print("=" * 60)

    result = await run_repair_workflow(
        workspace_dir,
        instance_id,
        prompt=prompt,
        phase1_only=phase1_only,
        phase2_only=phase2_only,
    )

    print("\n" + "=" * 60)
    print("ORCHESTRATION RESULT")
    print("=" * 60)
    print(f"Success: {result.get('success')}")
    print(f"Final State: {result.get('final_state')}")
    print(f"Result: {result.get('result')}")


def main() -> None:
    """CLI main function."""
    parser = argparse.ArgumentParser(
        description="Evidence-Closure-Aware Software Engineering Repair Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.main instance_001
  python -m src.main instance_001 --phase1-only
  python -m src.main instance_001 --phase2-only
        """,
    )

    parser.add_argument("instance_id", help="SWE-Bench Pro instance ID")
    parser.add_argument("--workspace", default="workdir", help="Base workspace directory (default: workdir)")
    parser.add_argument("--phase1-only", action="store_true", help="Run only phase1")
    parser.add_argument("--phase2-only", action="store_true", help="Run only phase2")

    args = parser.parse_args()
    workspace_dir = setup_workspace(args.instance_id, args.workspace)

    asyncio.run(
        run_cli(
            args.instance_id,
            workspace_dir,
            phase1_only=args.phase1_only,
            phase2_only=args.phase2_only,
        )
    )


if __name__ == "__main__":
    main()
