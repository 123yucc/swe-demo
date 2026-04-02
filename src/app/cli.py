"""CLI entrypoint for dynamic orchestrator workflow."""

from __future__ import annotations

import argparse
import asyncio

from ..pipelines import run_repair_workflow
from .bootstrap import setup_workspace


async def run_dynamic_workflow(
    instance_id: str,
    workspace_dir: str,
    resume: bool = False,
    fail_fast: bool = False,
    from_phase: str | None = None,
) -> None:
    """Run dynamic orchestration workflow."""
    print(f"Running Dynamic Workflow for instance {instance_id}...")
    print(f"Workspace: {workspace_dir}")
    print(f"Resume: {resume}, Fail-fast: {fail_fast}, From-phase: {from_phase}")

    if resume or from_phase or fail_fast:
        print("Warning: --resume/--from-phase/--fail-fast are not supported in orchestration-only mode and will be ignored.")

    print("\n" + "=" * 60)
    print("DYNAMIC ORCHESTRATION STARTING")
    print("=" * 60)

    result = await run_repair_workflow(
        workspace_dir,
        instance_id,
        prompt="run dynamic workflow from CLI",
    )

    print("\n" + "=" * 60)
    print("ORCHESTRATION RESULT")
    print("=" * 60)
    print(f"Success: {result['success']}")
    print(f"Final State: {result['final_state']}")
    print(f"\nCompleted Workers: {len(result['completed_workers'])}")
    for worker in result["completed_workers"]:
        print(f"  [OK] {worker}")
    print(f"\nFailed Workers: {len(result['failed_workers'])}")
    for worker in result["failed_workers"]:
        print(f"  [FAIL] {worker}")
    print("\nMetrics:")
    print(f"  Iterations: {result.get('iterations', 0)}")
    print(f"  Log File: {result.get('log_file', '')}")


def main() -> None:
    """CLI main function."""
    parser = argparse.ArgumentParser(
        description="Evidence-Closure-Aware Software Engineering Repair Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py instance_001 --dynamic
  python main.py instance_001 --dynamic --resume
  python main.py instance_001 --dynamic --from-phase phase2
  python main.py instance_001 --dynamic --fail-fast
        """,
    )

    parser.add_argument("instance_id", help="SWE-Bench Pro instance ID")
    parser.add_argument("--workspace", default="workdir", help="Base workspace directory (default: workdir)")
    parser.add_argument("--dynamic", action="store_true", help="Run with dynamic orchestrator")
    parser.add_argument("--resume", action="store_true", help="Resume from previous state")
    parser.add_argument("--fail-fast", action="store_true", help="Stop immediately on failure")
    parser.add_argument("--from-phase", type=str, help="Start from specific phase")

    args = parser.parse_args()
    workspace_dir = setup_workspace(args.instance_id, args.workspace)

    if not args.dynamic:
        print("Error: only --dynamic mode is supported after cleanup.")
        parser.print_help()
        return

    asyncio.run(
        run_dynamic_workflow(
            args.instance_id,
            workspace_dir,
            resume=args.resume,
            fail_fast=args.fail_fast,
            from_phase=args.from_phase,
        )
    )
