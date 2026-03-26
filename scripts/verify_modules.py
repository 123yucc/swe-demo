#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证新模块可以正确导入和基本功能。

运行方式:
    python scripts/verify_modules.py
"""

import sys
import io
from pathlib import Path

# 设置 stdout 为 utf-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def test_memory_module():
    """测试 Memory 模块。"""
    print("\n" + "="*60)
    print("Testing Memory Module")
    print("="*60)

    from src.memory import (
        MemoryManager, LongTermMemory, ShortTermMemory,
        MemoryItem, WeightProfile, AntiPattern,
        SessionState, EvidenceGap, DecisionLog
    )
    from src.memory.shortterm import PhaseStatus, GapPriority

    # 创建临时目录测试
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        # 测试 LongTermMemory
        ltm = LongTermMemory(tmpdir)
        print("  [OK] LongTermMemory created")

        # 添加模式
        pattern = MemoryItem(
            id="test_pattern_1",
            topic="pattern",
            signal="authentication_error",
            action="check_token_validity",
            outcome="success",
            confidence=0.8
        )
        ltm.add_pattern(pattern)
        print("  [OK] Pattern added")

        # 查找模式
        matches = ltm.find_matching_patterns("authentication")
        print(f"  [OK] Found {len(matches)} matching patterns")

        # 测试 ShortTermMemory
        stm = ShortTermMemory(tmpdir, "test_instance")
        print("  [OK] ShortTermMemory created")

        # 初始化会话
        stm.init_session("phase1")
        print("  [OK] Session initialized")

        # 添加证据缺口
        gap = stm.add_evidence_gap(
            card_type="symptom",
            gap_type="missing",
            required_signal="stack_trace",
            description="Missing stack trace information",
            source_phase="phase2"
        )
        print(f"  [OK] Evidence gap added: {gap.gap_id}")

        # 记录决策
        decision = stm.log_decision(
            decision="Use LLM enhancement",
            rationale="Evidence insufficient for closure",
            phase="phase2"
        )
        print(f"  [OK] Decision logged: {decision.decision_id}")

        # 获取统计
        stats = stm.get_statistics()
        print(f"  [OK] Statistics: {stats}")

    print("\n  Memory module tests passed!")
    return True


def test_llm_enhancement_module():
    """测试 LLM Enhancement 模块。"""
    print("\n" + "="*60)
    print("Testing LLM Enhancement Module")
    print("="*60)

    from src.llm_enhancement import (
        LLMEnhancer, EnhancedCodebaseNavigator
    )
    from src.llm_enhancement.navigator_ext import ValidationStatus

    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        # 创建实例目录
        instance_dir = Path(tmpdir) / "test_instance"
        (instance_dir / "repo").mkdir(parents=True)
        (instance_dir / "evidence").mkdir(parents=True)

        # 创建测试文件
        test_file = instance_dir / "repo" / "test.py"
        test_file.write_text('''
def hello():
    """Say hello."""
    return "Hello, World!"

class Calculator:
    def add(self, a, b):
        return a + b
''', encoding='utf-8')

        # 测试 EnhancedCodebaseNavigator
        nav = EnhancedCodebaseNavigator(str(instance_dir / "repo"))
        print("  [OK] EnhancedCodebaseNavigator created")

        # 验证位置
        result = nav.validate_location("test.py", "hello", 2)
        print(f"  [OK] Validation: {result.status.value}")

        # AST 验证
        result = nav.validate_with_ast("test.py", "function", "hello")
        print(f"  [OK] AST validation: {result.status.value}")

        # 获取代码窗口
        window = nav.get_code_window("test.py", 2, context_lines=2)
        if window:
            print(f"  [OK] Code window: lines {window.start_line}-{window.end_line}")

        # 获取符号上下文
        context = nav.get_symbol_context("test.py", "hello")
        if context:
            print(f"  [OK] Symbol context: {context['name']} ({context['type']})")

        # 双通道验证
        dual_result = nav.dual_channel_validate("test.py", "hello", "hello")
        print(f"  [OK] Dual validation: grep={dual_result.get('grep_match')}, ast={dual_result.get('ast_match')}")

        # 置信度评估
        confidence = nav.assess_confidence(
            [{"source_type": "code", "match_type": "exact", "confidence_contribution": 0.9}],
            llm_signal=0.7
        )
        print(f"  [OK] Confidence: {confidence['confidence']}")

        # 测试 LLMEnhancer
        enhancer = LLMEnhancer(tmpdir, "test_instance")
        print("  [OK] LLMEnhancer created")

    print("\n  LLM Enhancement module tests passed!")
    return True


def test_scheduler_module():
    """测试 Scheduler 模块。"""
    print("\n" + "="*60)
    print("Testing Scheduler Module")
    print("="*60)

    from src.scheduler import (
        Scheduler, WorkflowState, TodoItem,
        TodoStatus, TodoPriority, PhaseStatus, TaskStatus,
        WorkerRegistry, create_default_registry
    )

    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        # 创建实例目录
        instance_dir = Path(tmpdir) / "test_instance"
        instance_dir.mkdir(parents=True)

        # 测试 WorkerRegistry
        registry = create_default_registry()
        workers = registry.get_all()
        print(f"  [OK] WorkerRegistry created with {len(workers)} workers")

        # 拓扑排序
        topo_order = registry.topological_sort()
        print(f"  [OK] Topological order: {topo_order[:3]}...")

        # 测试 TodoItem
        todo = TodoItem(
            todo_id="todo_test_1",
            source_type="closure",
            source_phase="phase3",
            priority=TodoPriority.P0,
            title="Test todo",
            description="Test description",
            action_type="verify"
        )
        print(f"  [OK] TodoItem created: {todo.todo_id}")

        # 测试 WorkflowState
        state = WorkflowState(instance_id="test_instance")
        state.add_phase_history("init", "completed")
        print("  [OK] WorkflowState created")

        # 添加 todo
        state.todos.append(todo)
        pending = state.get_pending_todos()
        print(f"  [OK] Pending todos: {len(pending)}")

        # 保存和加载
        state_path = instance_dir / "workflow_state.json"
        state.save(state_path)
        loaded_state = WorkflowState.load(state_path)
        print("  [OK] State saved and loaded")

        # 测试 Scheduler
        scheduler = Scheduler(tmpdir, "test_instance", registry)
        scheduler.init_state()
        print("  [OK] Scheduler initialized")

        # 获取统计
        stats = scheduler.get_statistics()
        print(f"  [OK] Scheduler stats: current_phase={stats['current_phase']}")

    print("\n  Scheduler module tests passed!")
    return True


def test_main_imports():
    """测试主模块导出。"""
    print("\n" + "="*60)
    print("Testing Main Module Imports")
    print("="*60)

    # 测试主模块可以正确导入所有组件
    import src
    print(f"  [OK] Module version: {src.__version__}")

    # 检查所有导出
    expected_exports = [
        # Evidence Cards
        "SymptomCard", "LocalizationCard", "ConstraintCard", "StructuralCard",
        # Memory
        "MemoryManager", "LongTermMemory", "ShortTermMemory",
        # LLM Enhancement
        "LLMEnhancer", "EnhancedCodebaseNavigator",
        # Scheduler
        "Scheduler", "WorkflowState", "TodoItem", "create_default_registry",
    ]

    for name in expected_exports:
        if hasattr(src, name):
            print(f"  [OK] {name} exported")
        else:
            print(f"  [FAIL] {name} NOT exported")
            return False

    print("\n  All expected exports found!")
    return True


def main():
    """运行所有测试。"""
    print("="*60)
    print("Module Verification Script")
    print("="*60)

    tests = [
        ("Memory Module", test_memory_module),
        ("LLM Enhancement Module", test_llm_enhancement_module),
        ("Scheduler Module", test_scheduler_module),
        ("Main Imports", test_main_imports),
    ]

    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n  [FAIL] {name} failed: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))

    # 汇总
    print("\n" + "="*60)
    print("RESULTS SUMMARY")
    print("="*60)
    passed = sum(1 for _, r in results if r)
    total = len(results)
    for name, result in results:
        status = "[PASS]" if result else "[FAIL]"
        print(f"  {status}: {name}")

    print(f"\nTotal: {passed}/{total} tests passed")
    return passed == total


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
