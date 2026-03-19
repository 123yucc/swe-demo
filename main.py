"""主入口文件。

提供命令行接口来运行 SWE-Bench Pro 实例修复。
改进版本：支持Phase 1 LLM解析和Phase 2动态提取
"""

import asyncio
import argparse
from pathlib import Path

# 使用新的实现
from src import run_phase1_parsing, run_phase2_extraction_dynamic


def setup_workspace(instance_id: str, base_dir: str = "workdir") -> str:
    """设置工作空间。"""
    workspace = Path(base_dir) / instance_id
    workspace.mkdir(parents=True, exist_ok=True)

    # 创建子目录
    (workspace / "artifacts").mkdir(exist_ok=True)
    (workspace / "artifacts" / "tests" / "fail2pass").mkdir(parents=True, exist_ok=True)
    (workspace / "artifacts" / "tests" / "pass2pass").mkdir(parents=True, exist_ok=True)
    (workspace / "evidence").mkdir(exist_ok=True)
    (workspace / "evidence" / "card_versions").mkdir(exist_ok=True)
    (workspace / "repo").mkdir(exist_ok=True)

    return str(workspace)


async def run_phase1_only(instance_id: str, workspace_dir: str):
    """只运行 Phase 1: Artifact Parsing (LLM驱动版本)。"""
    print(f"Running Phase 1 (LLM-driven) for instance {instance_id}...")
    print(f"Workspace: {workspace_dir}")

    try:
        result = await run_phase1_parsing(workspace_dir, instance_id)

        print("\n✓ Phase 1 Complete!")
        print("\nGenerated Cards:")
        for card_type, card in result["cards"].items():
            print(f"  - {card_type}_card.json (v{card.version})")

        summary = result["summary"]
        print(f"\nSummary:")
        print(f"  Artifacts used: {summary['artifacts_used']}")
        print(f"  Artifacts missing: {summary['artifacts_missing']}")
        print(f"  Next phase: {summary['next_phase']}")

        print("\nEvidence card statuses:")
        for card_type, info in summary.get("evidence_cards", {}).items():
            print(f"  - {card_type}: {info['sufficiency_status']} - {info['sufficiency_notes'][:50]}...")

    except FileNotFoundError as e:
        print(f"\n✗ Error: {e}")
        print("\nPlease ensure the following files exist in the artifacts directory:")
        print("  - artifacts/problem_statement.md (required)")
        print("  - artifacts/requirements.md (optional)")
        print("  - artifacts/interface.md or new_interfaces.md (optional)")
        print("  - artifacts/expected_and_current_behavior.md (optional)")
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()


async def run_phase2_only(instance_id: str, workspace_dir: str):
    """只运行 Phase 2: Evidence Extraction (动态版本)。"""
    print(f"Running Phase 2 (Dynamic) for instance {instance_id}...")
    print(f"Workspace: {workspace_dir}")

    try:
        result = run_phase2_extraction_dynamic(workspace_dir, instance_id)

        print("\n✓ Phase 2 Complete!")
        print("\nUpdated Cards:")
        for card_type, card in result.items():
            print(f"  - {card_type}_card.json (v{card.version})")
            print(f"    Sufficiency: {card.sufficiency_status.value}")
            print(f"    Notes: {card.sufficiency_notes[:60]}...")

    except FileNotFoundError as e:
        print(f"\n✗ Error: {e}")
        print("\nPlease ensure Phase 1 has been run and evidence cards exist.")
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()


async def run_full_workflow(instance_id: str, problem_statement: str, workspace_dir: str):
    """运行完整的修复工作流 (Phase 1 + Phase 2)。"""
    print(f"Starting repair workflow for instance {instance_id}...")
    print(f"Problem: {problem_statement[:100]}...")

    # Phase 1: Artifact Parsing
    print("\n" + "="*60)
    print("PHASE 1: Artifact Parsing")
    print("="*60)
    await run_phase1_only(instance_id, workspace_dir)

    # Phase 2: Evidence Extraction
    print("\n" + "="*60)
    print("PHASE 2: Evidence Extraction")
    print("="*60)
    await run_phase2_only(instance_id, workspace_dir)

    print("\n" + "="*60)
    print("WORKFLOW COMPLETE")
    print("="*60)
    print(f"\nEvidence cards saved to: {Path(workspace_dir) / 'evidence'}")


def main():
    """主函数。"""
    parser = argparse.ArgumentParser(
        description="Evidence-Closure-Aware Software Engineering Repair Agent (v0.2.0)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run Phase 1 only (LLM-driven artifact parsing)
  python main.py instance_001 --phase1-only

  # Run Phase 2 only (dynamic evidence extraction)
  python main.py instance_001 --phase2-only

  # Run full workflow (Phase 1 + Phase 2)
  python main.py instance_001 --problem "Fix the authentication bug..."

  # Run with custom workspace
  python main.py instance_001 --workspace ./my_workdir --phase1-only
        """
    )
    parser.add_argument(
        "instance_id",
        help="SWE-Bench Pro instance ID"
    )
    parser.add_argument(
        "--workspace",
        default="workdir",
        help="Base workspace directory (default: workdir)"
    )
    parser.add_argument(
        "--phase1-only",
        action="store_true",
        help="Only run Phase 1 (Artifact Parsing with LLM)"
    )
    parser.add_argument(
        "--phase2-only",
        action="store_true",
        help="Only run Phase 2 (Dynamic Evidence Extraction)"
    )
    parser.add_argument(
        "--problem",
        type=str,
        help="Problem statement (for full workflow)"
    )

    args = parser.parse_args()

    # 设置工作空间
    workspace_dir = setup_workspace(args.instance_id, args.workspace)

    if args.phase1_only:
        # 只运行 Phase 1
        asyncio.run(run_phase1_only(args.instance_id, workspace_dir))
    elif args.phase2_only:
        # 只运行 Phase 2
        asyncio.run(run_phase2_only(args.instance_id, workspace_dir))
    else:
        # 运行完整工作流
        if not args.problem:
            print("Error: --problem is required for full workflow")
            parser.print_help()
            return

        asyncio.run(run_full_workflow(args.instance_id, args.problem, workspace_dir))


if __name__ == "__main__":
    main()
