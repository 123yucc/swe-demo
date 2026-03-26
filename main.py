"""主入口文件。

提供命令行接口来运行 SWE-Bench Pro 实例修复。
v0.5.0 改进版本：
- 支持动态调度器
- 支持 --resume/--max-parallel/--fail-fast/--from-phase
- 集成 Memory 管理和 LLM 强化层
"""

import asyncio
import argparse
from pathlib import Path

# 使用新的实现
from src import (
    run_phase1_parsing, run_phase2_extraction_dynamic,
    Scheduler, create_default_registry, MemoryManager
)


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

    # 创建 memory 目录
    (workspace / ".memory" / "longterm").mkdir(parents=True, exist_ok=True)

    # 创建调度器目录
    (workspace / ".workflow").mkdir(parents=True, exist_ok=True)
    (workspace / "logs").mkdir(exist_ok=True)
    (workspace / "plan").mkdir(exist_ok=True)

    return str(workspace)


async def run_phase1_only(instance_id: str, workspace_dir: str):
    """只运行 Phase 1: Artifact Parsing (LLM驱动版本)。"""
    print(f"Running Phase 1 (LLM-driven) for instance {instance_id}...")
    print(f"Workspace: {workspace_dir}")

    try:
        result = await run_phase1_parsing(workspace_dir, instance_id)

        print("\n[OK] Phase 1 Complete!")
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
        print(f"\n[FAIL] Error: {e}")
        print("\nPlease ensure the following files exist in the artifacts directory:")
        print("  - artifacts/problem_statement.md (required)")
        print("  - artifacts/requirements.md (optional)")
        print("  - artifacts/interface.md or new_interfaces.md (optional)")
        print("  - artifacts/expected_and_current_behavior.md (optional)")
    except Exception as e:
        print(f"\n[FAIL] Unexpected error: {e}")
        import traceback
        traceback.print_exc()


async def run_phase2_only(instance_id: str, workspace_dir: str):
    """只运行 Phase 2: Evidence Extraction (动态版本)。"""
    print(f"Running Phase 2 (Dynamic) for instance {instance_id}...")
    print(f"Workspace: {workspace_dir}")

    try:
        result = run_phase2_extraction_dynamic(workspace_dir, instance_id)

        print("\n[OK] Phase 2 Complete!")
        print("\nUpdated Cards:")
        for card_type, card in result.items():
            print(f"  - {card_type}_card.json (v{card.version})")
            print(f"    Sufficiency: {card.sufficiency_status.value}")
            print(f"    Notes: {card.sufficiency_notes[:60]}...")

    except FileNotFoundError as e:
        print(f"\n[FAIL] Error: {e}")
        print("\nPlease ensure Phase 1 has been run and evidence cards exist.")
    except Exception as e:
        print(f"\n[FAIL] Unexpected error: {e}")
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


async def run_dynamic_workflow(
    instance_id: str,
    workspace_dir: str,
    resume: bool = False,
    fail_fast: bool = False,
    from_phase: str = None,
    max_parallel: int = 1
):
    """运行动态调度工作流。

    Args:
        instance_id: 实例 ID
        workspace_dir: 工作目录
        resume: 是否从上次中断处恢复
        fail_fast: 是否在失败时立即停止
        from_phase: 从指定阶段开始
        max_parallel: 最大并行数（当前版本暂不支持真正的并行）
    """
    print(f"Running Dynamic Workflow for instance {instance_id}...")
    print(f"Workspace: {workspace_dir}")
    print(f"Resume: {resume}, Fail-fast: {fail_fast}, From-phase: {from_phase}")

    # 创建调度器 - workspace_dir 已经包含 instance_id
    # 调度器会自动检测并直接使用 workspace_dir 作为实例目录
    registry = create_default_registry()
    scheduler = Scheduler(workspace_dir, instance_id, registry)

    # 加载或初始化状态
    if resume:
        if scheduler.load_state():
            print(f"Resumed from phase: {scheduler.state.current_phase}")
        else:
            print("No previous state found, starting fresh")
            scheduler.init_state()
    else:
        scheduler.init_state()

    # 运行调度循环
    print("\n" + "="*60)
    print("DYNAMIC SCHEDULER STARTING")
    print("="*60)

    result = await scheduler.run(
        max_iterations=100,
        fail_fast=fail_fast,
        from_phase=from_phase
    )

    # 输出结果
    print("\n" + "="*60)
    print("SCHEDULER RESULT")
    print("="*60)
    print(f"Success: {result.success}")
    print(f"Final Phase: {result.final_phase}")
    print(f"Final Status: {result.final_status.value}")
    print(f"\nCompleted Workers: {len(result.completed_workers)}")
    for w in result.completed_workers:
        print(f"  [OK] {w}")
    print(f"\nFailed Workers: {len(result.failed_workers)}")
    for w in result.failed_workers:
        print(f"  [FAIL] {w}")
    print(f"\nTodos Generated: {len(result.todos_generated)}")
    print(f"Todos Resolved: {len(result.todos_resolved)}")

    if result.error:
        print(f"\nError: {result.error}")

    # 统计信息
    stats = scheduler.get_statistics()
    print(f"\nMetrics:")
    print(f"  Iterations: {result.metrics.get('iterations', 0)}")
    print(f"  Card Versions: {stats.get('card_versions', {})}")
    print(f"  Pending Todos: {stats.get('todos', {}).get('pending', 0)}")


def main():
    """主函数。"""
    parser = argparse.ArgumentParser(
        description="Evidence-Closure-Aware Software Engineering Repair Agent (v0.5.0)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run Phase 1 only (LLM-driven artifact parsing)
  python main.py instance_001 --phase1-only

  # Run Phase 2 only (dynamic evidence extraction)
  python main.py instance_001 --phase2-only

  # Run full workflow (Phase 1 + Phase 2)
  python main.py instance_001 --problem "Fix the authentication bug..."

  # Run with dynamic scheduler
  python main.py instance_001 --dynamic

  # Resume interrupted workflow
  python main.py instance_001 --dynamic --resume

  # Run from specific phase
  python main.py instance_001 --dynamic --from-phase phase2

  # Run with fail-fast mode
  python main.py instance_001 --dynamic --fail-fast
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
    # 新增动态调度参数
    parser.add_argument(
        "--dynamic",
        action="store_true",
        help="Run with dynamic scheduler"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from previous state (requires --dynamic)"
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop immediately on failure (requires --dynamic)"
    )
    parser.add_argument(
        "--from-phase",
        type=str,
        help="Start from specific phase (requires --dynamic)"
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=1,
        help="Maximum parallel workers (default: 1, requires --dynamic)"
    )

    args = parser.parse_args()

    # 设置工作空间
    workspace_dir = setup_workspace(args.instance_id, args.workspace)

    if args.dynamic:
        # 运行动态调度工作流
        asyncio.run(run_dynamic_workflow(
            args.instance_id,
            workspace_dir,
            resume=args.resume,
            fail_fast=args.fail_fast,
            from_phase=args.from_phase,
            max_parallel=args.max_parallel
        ))
    elif args.phase1_only:
        # 只运行 Phase 1
        asyncio.run(run_phase1_only(args.instance_id, workspace_dir))
    elif args.phase2_only:
        # 只运行 Phase 2
        asyncio.run(run_phase2_only(args.instance_id, workspace_dir))
    else:
        # 运行完整工作流
        if not args.problem:
            print("Error: --problem is required for full workflow (or use --dynamic)")
            parser.print_help()
            return

        asyncio.run(run_full_workflow(args.instance_id, args.problem, workspace_dir))


if __name__ == "__main__":
    main()
