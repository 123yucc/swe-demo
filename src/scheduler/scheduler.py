"""Dynamic Scheduler Implementation.

核心调度器实现：
- DAG + 关键路径 + 状态机
- 动态 Todo 管理
- 失败重试和恢复
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Callable, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
import asyncio

from .models import (
    WorkflowState, WorkerSpec, TaskSpec, TodoItem,
    TodoStatus, TodoPriority, PhaseStatus, TaskStatus,
    WorkerRegistry, create_default_registry
)


# === 执行器函数映射 ===

def _get_executor_functions():
    """获取执行器函数映射。

    将 worker_id 映射到实际的执行函数。
    """
    executors = {}

    # Phase 1: Artifact Parsing
    async def run_phase1(inputs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """执行 Phase 1 解析。"""
        from ..artifact_parsers_llm import run_phase1_parsing
        workspace = inputs.get("workspace_dir") if inputs else None
        instance_id = inputs.get("instance_id") if inputs else None
        if not workspace or not instance_id:
            raise ValueError("Phase 1 requires workspace_dir and instance_id")
        result = await run_phase1_parsing(workspace, instance_id)
        return {"cards": list(result.get("cards", {}).keys()), "summary": result.get("summary", {})}

    executors["artifact-parser"] = run_phase1

    # Phase 2: Evidence Extraction Workers
    async def run_phase2_symptom(inputs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """执行症状证据提取。"""
        from ..evidence_extractors_phase2 import extract_symptom_evidence
        workspace = inputs.get("workspace_dir") if inputs else None
        instance_id = inputs.get("instance_id") if inputs else None
        if not workspace or not instance_id:
            raise ValueError("Phase 2 symptom requires workspace_dir and instance_id")
        result = extract_symptom_evidence(workspace, instance_id)
        return {"card": "symptom", "version": result.version if result else 1}

    executors["symptom-extractor"] = run_phase2_symptom

    async def run_phase2_localization(inputs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """执行定位证据提取。"""
        from ..evidence_extractors_phase2 import extract_localization_evidence
        workspace = inputs.get("workspace_dir") if inputs else None
        instance_id = inputs.get("instance_id") if inputs else None
        if not workspace or not instance_id:
            raise ValueError("Phase 2 localization requires workspace_dir and instance_id")
        result = extract_localization_evidence(workspace, instance_id)
        return {"card": "localization", "version": result.version if result else 1}

    executors["localization-extractor"] = run_phase2_localization

    async def run_phase2_constraint(inputs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """执行约束证据提取。"""
        from ..evidence_extractors_phase2 import extract_constraint_evidence
        workspace = inputs.get("workspace_dir") if inputs else None
        instance_id = inputs.get("instance_id") if inputs else None
        if not workspace or not instance_id:
            raise ValueError("Phase 2 constraint requires workspace_dir and instance_id")
        result = extract_constraint_evidence(workspace, instance_id)
        return {"card": "constraint", "version": result.version if result else 1}

    executors["constraint-extractor"] = run_phase2_constraint

    async def run_phase2_structural(inputs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """执行结构证据提取。"""
        from ..evidence_extractors_phase2 import extract_structural_evidence
        workspace = inputs.get("workspace_dir") if inputs else None
        instance_id = inputs.get("instance_id") if inputs else None
        if not workspace or not instance_id:
            raise ValueError("Phase 2 structural requires workspace_dir and instance_id")
        result = extract_structural_evidence(workspace, instance_id)
        return {"card": "structural", "version": result.version if result else 1}

    executors["structural-extractor"] = run_phase2_structural

    # Evidence Validator
    async def run_llm_enhancer(inputs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """执行证据验证。"""
        from ..evidence_extractors_phase2 import enhance_all_cards
        workspace = inputs.get("workspace_dir") if inputs else None
        instance_id = inputs.get("instance_id") if inputs else None
        if not workspace or not instance_id:
            raise ValueError("LLM enhancer requires workspace_dir and instance_id")
        result = enhance_all_cards(workspace, instance_id)
        return {"enhanced_cards": result}

    executors["llm-enhancer"] = run_llm_enhancer

    # Closure Checker
    async def run_closure_checker(inputs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """执行闭包检查。"""
        from ..closure_checker import check_evidence_closure
        workspace = inputs.get("workspace_dir") if inputs else None
        instance_id = inputs.get("instance_id") if inputs else None
        if not workspace or not instance_id:
            raise ValueError("Closure checker requires workspace_dir and instance_id")
        result = check_evidence_closure(workspace, instance_id)
        return {"passed": result.get("passed", False), "gaps": result.get("gaps", [])}

    executors["closure-checker"] = run_closure_checker

    # Patch Planner
    async def run_patch_planner(inputs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """执行 patch 规划。"""
        from ..patch_planner import create_patch_plan
        workspace = inputs.get("workspace_dir") if inputs else None
        instance_id = inputs.get("instance_id") if inputs else None
        if not workspace or not instance_id:
            raise ValueError("Patch planner requires workspace_dir and instance_id")
        result = create_patch_plan(workspace, instance_id)
        return {"plan": result}

    executors["patch-planner"] = run_patch_planner

    # Patch Executor
    async def run_patch_executor(inputs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """执行 patch。"""
        from ..patch_executor import execute_patch
        workspace = inputs.get("workspace_dir") if inputs else None
        instance_id = inputs.get("instance_id") if inputs else None
        if not workspace or not instance_id:
            raise ValueError("Patch executor requires workspace_dir and instance_id")
        result = execute_patch(workspace, instance_id)
        return {"result": result}

    executors["patch-executor"] = run_patch_executor

    # Validator
    async def run_validator(inputs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """执行验证。"""
        from ..validator import validate_patch
        workspace = inputs.get("workspace_dir") if inputs else None
        instance_id = inputs.get("instance_id") if inputs else None
        if not workspace or not instance_id:
            raise ValueError("Validator requires workspace_dir and instance_id")
        result = validate_patch(workspace, instance_id)
        return {"valid": result.get("valid", False)}

    executors["validator"] = run_validator

    return executors


@dataclass
class ScheduleResult:
    """调度结果。"""
    success: bool
    completed_workers: List[str]
    failed_workers: List[str]
    todos_generated: List[str]
    todos_resolved: List[str]
    final_phase: str
    final_status: PhaseStatus
    error: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)


class Scheduler:
    """动态调度器。

    职责：
    - 管理 workflow 状态
    - 计算 ready set
    - 执行 workers
    - 管理 todo 队列
    - 处理失败和重试
    """

    def __init__(
        self,
        workspace_dir: str,
        instance_id: str = None,
        registry: Optional[WorkerRegistry] = None
    ):
        self.workspace_dir = Path(workspace_dir)
        self.instance_id = instance_id or self.workspace_dir.name

        # 智能路径处理：
        # 如果 workspace_dir 名称等于 instance_id，则直接使用它
        # 否则，创建 instance_id 子目录
        if self.workspace_dir.name == self.instance_id:
            self.instance_dir = self.workspace_dir
        else:
            self.instance_dir = self.workspace_dir / self.instance_id

        # 目录设置
        self.state_dir = self.instance_dir / ".workflow"
        self.log_dir = self.instance_dir / "logs"
        self.plan_dir = self.instance_dir / "plan"

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.plan_dir.mkdir(parents=True, exist_ok=True)

        # Worker 注册表
        self.registry = registry or create_default_registry()

        # 工作流状态
        self.state_path = self.state_dir / "workflow_state.json"
        self.todo_path = self.plan_dir / "todo_queue.json"

        self.state: Optional[WorkflowState] = None

        # 运行时状态
        self._running_workers: Set[str] = set()
        self._completed_workers: Set[str] = set()
        self._failed_workers: Set[str] = set()

        # 事件日志
        self._events: List[Dict[str, Any]] = []

    # === 状态管理 ===

    def load_state(self) -> bool:
        """加载工作流状态。"""
        try:
            if self.state_path.exists():
                self.state = WorkflowState.load(self.state_path)
                # 恢复运行时状态
                for worker_id, status in self.state.worker_status.items():
                    if status == TaskStatus.COMPLETED:
                        self._completed_workers.add(worker_id)
                    elif status == TaskStatus.FAILED:
                        self._failed_workers.add(worker_id)
                return True
            return False
        except Exception as e:
            self._log_event("error", "load_state_failed", {"error": str(e)})
            return False

    def save_state(self) -> None:
        """保存工作流状态。"""
        if self.state:
            self.state.save(self.state_path)
            self._save_todo_queue()

    def init_state(self) -> WorkflowState:
        """初始化工作流状态。"""
        self.state = WorkflowState(
            instance_id=self.instance_id,
            current_phase="init",
            phase_status=PhaseStatus.PENDING
        )
        self.save_state()
        return self.state

    def get_or_create_state(self) -> WorkflowState:
        """获取或创建状态。"""
        if not self.load_state():
            return self.init_state()
        return self.state

    # === 调度核心 ===

    def tick(self) -> Tuple[List[str], List[str]]:
        """执行一次调度 tick。

        Returns:
            (ready_workers, ready_todos): 可以执行的 worker 和 todo
        """
        if not self.state:
            self.get_or_create_state()

        # 1. 计算 ready workers
        ready_workers = self.registry.get_ready_workers(
            self._completed_workers,
            self._running_workers
        )

        # 2. 计算 ready todos
        ready_todos = self._get_ready_todos()

        # 3. 记录事件
        self._log_event("tick", "schedule", {
            "ready_workers": ready_workers,
            "ready_todos": [t.todo_id for t in ready_todos],
            "running": list(self._running_workers),
            "completed": list(self._completed_workers)
        })

        return ready_workers, ready_todos

    def _get_ready_todos(self) -> List[TodoItem]:
        """获取可以执行的 todo。"""
        ready = []
        for todo in self.state.todos:
            if todo.status != TodoStatus.PENDING:
                continue

            # 检查依赖
            deps_satisfied = all(
                self._is_todo_completed(dep_id)
                for dep_id in todo.depends_on
            )

            if deps_satisfied and todo.can_retry():
                ready.append(todo)

        # 按优先级排序
        priority_order = {
            TodoPriority.P0: 0,
            TodoPriority.P1: 1,
            TodoPriority.P2: 2,
            TodoPriority.P3: 3
        }
        ready.sort(key=lambda t: priority_order.get(t.priority, 4))

        return ready

    def _is_todo_completed(self, todo_id: str) -> bool:
        """检查 todo 是否完成。"""
        todo = self.state.get_todo_by_id(todo_id)
        return todo and todo.status == TodoStatus.DONE

    # === Worker 执行 ===

    async def execute_worker(
        self,
        worker_id: str,
        inputs: Optional[Dict[str, Any]] = None
    ) -> TaskSpec:
        """执行 worker。

        Args:
            worker_id: Worker ID
            inputs: 输入数据

        Returns:
            TaskSpec: 任务规格（包含执行结果）
        """
        worker_spec = self.registry.get(worker_id)
        if not worker_spec:
            raise ValueError(f"Unknown worker: {worker_id}")

        # 创建任务
        task = TaskSpec(
            task_id=f"task_{worker_id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            worker_id=worker_id,
            status=TaskStatus.RUNNING,
            inputs=inputs or {},
            started_at=datetime.utcnow().isoformat()
        )

        # 更新状态
        self._running_workers.add(worker_id)
        self.state.worker_status[worker_id] = TaskStatus.RUNNING
        self.save_state()

        self._log_event("worker", "started", {
            "worker_id": worker_id,
            "task_id": task.task_id
        })

        try:
            # 获取执行器
            executor = self.registry.get_executor(worker_id)

            # 准备执行输入
            exec_inputs = inputs or {}
            exec_inputs["workspace_dir"] = str(self.instance_dir)
            exec_inputs["instance_id"] = self.instance_id

            if executor:
                # 执行
                result = await self._run_executor(executor, exec_inputs)
                task.outputs = result or {}
                task.status = TaskStatus.COMPLETED
            else:
                # 尝试动态加载执行器
                result = await self._run_dynamic_executor(worker_id, exec_inputs)
                if result:
                    task.outputs = result
                    task.status = TaskStatus.COMPLETED
                else:
                    # 无执行器，标记为完成（用于模拟或占位）
                    task.outputs = {"status": "no_executor", "worker_id": worker_id}
                    task.status = TaskStatus.COMPLETED

            # 更新完成状态
            self._completed_workers.add(worker_id)
            self.state.worker_status[worker_id] = TaskStatus.COMPLETED
            self.state.worker_results[worker_id] = task.outputs

            # 更新卡片版本
            for card_type in worker_spec.produces_cards:
                current_version = self.state.card_versions.get(card_type, 0)
                self.state.card_versions[card_type] = current_version + 1

            # 生成 todo（如果有）
            if worker_spec.produces_todos:
                self._generate_todos_from_worker(worker_spec, task.outputs)

        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)

            self._failed_workers.add(worker_id)
            self.state.worker_status[worker_id] = TaskStatus.FAILED
            self.state.failure_reason = str(e)
            self.state.failure_phase = worker_spec.phase

            # 生成修复 todo
            self._generate_fix_todo(worker_id, str(e))

        finally:
            task.completed_at = datetime.utcnow().isoformat()
            if task.started_at:
                start = datetime.fromisoformat(task.started_at)
                end = datetime.fromisoformat(task.completed_at)
                task.duration_ms = int((end - start).total_seconds() * 1000)

            self._running_workers.discard(worker_id)
            self.save_state()

        self._log_event("worker", "completed", {
            "worker_id": worker_id,
            "task_id": task.task_id,
            "status": task.status.value,
            "duration_ms": task.duration_ms
        })

        return task

    async def _run_executor(
        self,
        executor: Callable,
        inputs: Optional[Dict[str, Any]]
    ) -> Any:
        """运行执行器。"""
        if asyncio.iscoroutinefunction(executor):
            return await executor(inputs)
        else:
            return executor(inputs)

    async def _run_dynamic_executor(
        self,
        worker_id: str,
        inputs: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """动态加载并运行执行器。

        根据 worker_id 调用对应的实际执行函数。
        """
        workspace = inputs.get("workspace_dir") if inputs else None
        instance_id = inputs.get("instance_id") if inputs else None

        if not workspace or not instance_id:
            return None

        try:
            # Phase 1: Artifact Parsing
            if worker_id == "artifact-parser":
                from ..artifact_parsers_llm import run_phase1_parsing
                result = await run_phase1_parsing(workspace, instance_id)
                return {"cards": list(result.get("cards", {}).keys()), "summary": result.get("summary", {})}

            # Phase 2: Evidence Extraction
            elif worker_id == "symptom-extractor":
                from ..evidence_extractors_phase2 import extract_symptom_evidence
                result = extract_symptom_evidence(workspace, instance_id)
                return {"card": "symptom", "version": result.version if result else 1}

            elif worker_id == "localization-extractor":
                from ..evidence_extractors_phase2 import extract_localization_evidence
                result = extract_localization_evidence(workspace, instance_id)
                return {"card": "localization", "version": result.version if result else 1}

            elif worker_id == "constraint-extractor":
                from ..evidence_extractors_phase2 import extract_constraint_evidence
                result = extract_constraint_evidence(workspace, instance_id)
                return {"card": "constraint", "version": result.version if result else 1}

            elif worker_id == "structural-extractor":
                from ..evidence_extractors_phase2 import extract_structural_evidence
                result = extract_structural_evidence(workspace, instance_id)
                return {"card": "structural", "version": result.version if result else 1}

            # Evidence Validator
            elif worker_id == "llm-enhancer":
                from ..evidence_extractors_phase2 import enhance_all_cards
                result = enhance_all_cards(workspace, instance_id)
                return {"enhanced_cards": result}

            # Closure Checker
            elif worker_id == "closure-checker":
                from ..closure_checker import check_evidence_closure
                result = check_evidence_closure(workspace, instance_id)
                return {"passed": result.get("passed", False), "gaps": result.get("gaps", [])}

            # Patch Planner
            elif worker_id == "patch-planner":
                from ..patch_planner import create_patch_plan
                result = create_patch_plan(workspace, instance_id)
                return {"plan": result}

            # Patch Executor
            elif worker_id == "patch-executor":
                from ..patch_executor import execute_patch
                result = execute_patch(workspace, instance_id)
                return {"result": result}

            # Validator
            elif worker_id == "validator":
                from ..validator import validate_patch
                result = validate_patch(workspace, instance_id)
                return {"valid": result.get("valid", False)}

            else:
                return None

        except ImportError as e:
            self._log_event("error", "import_failed", {"worker_id": worker_id, "error": str(e)})
            return None
        except Exception as e:
            self._log_event("error", "dynamic_executor_failed", {"worker_id": worker_id, "error": str(e)})
            raise

    # === Todo 管理 ===

    def add_todo(self, todo: TodoItem) -> None:
        """添加 todo。"""
        self.state.todos.append(todo)
        self.save_state()

        self._log_event("todo", "added", {
            "todo_id": todo.todo_id,
            "priority": todo.priority.value,
            "source": todo.source_type
        })

    def resolve_todo(self, todo_id: str, resolution: Dict[str, Any]) -> bool:
        """解决 todo。"""
        todo = self.state.get_todo_by_id(todo_id)
        if not todo:
            return False

        todo.status = TodoStatus.DONE
        todo.result = resolution
        todo.completed_at = datetime.utcnow().isoformat()
        todo.updated_at = datetime.utcnow().isoformat()

        # 解除阻塞
        for blocked_id in todo.blocks:
            blocked_todo = self.state.get_todo_by_id(blocked_id)
            if blocked_todo and blocked_todo.status == TodoStatus.BLOCKED:
                blocked_todo.status = TodoStatus.PENDING

        self.save_state()

        self._log_event("todo", "resolved", {
            "todo_id": todo_id,
            "resolution": resolution
        })

        return True

    def fail_todo(self, todo_id: str, error: str) -> bool:
        """标记 todo 失败。"""
        todo = self.state.get_todo_by_id(todo_id)
        if not todo:
            return False

        todo.retry_count += 1
        todo.error_info = {"error": error, "at": datetime.utcnow().isoformat()}
        todo.updated_at = datetime.utcnow().isoformat()

        if todo.can_retry():
            todo.status = TodoStatus.PENDING
        else:
            todo.status = TodoStatus.ABANDONED

        self.save_state()

        self._log_event("todo", "failed", {
            "todo_id": todo_id,
            "error": error,
            "retry_count": todo.retry_count
        })

        return True

    def _generate_todos_from_worker(
        self,
        worker_spec: WorkerSpec,
        outputs: Dict[str, Any]
    ) -> None:
        """从 worker 输出生成 todo。"""
        # 检查输出中是否有 gap 信息
        gaps = outputs.get("gaps", [])
        for gap in gaps:
            todo = TodoItem(
                todo_id=f"todo_gap_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{gap.get('id', '')}",
                source_type="closure",
                source_phase=worker_spec.phase,
                priority=TodoPriority.P0,
                title=f"Resolve evidence gap: {gap.get('type', 'unknown')}",
                description=gap.get("description", ""),
                action_type="verify",
                target_card=gap.get("card_type"),
                target_phase="phase2"
            )
            self.add_todo(todo)

    def _generate_fix_todo(self, worker_id: str, error: str) -> None:
        """生成修复 todo。"""
        todo = TodoItem(
            todo_id=f"todo_fix_{worker_id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            source_type="worker",
            source_phase=self.registry.get(worker_id).phase if self.registry.get(worker_id) else "unknown",
            priority=TodoPriority.P1,
            title=f"Fix worker failure: {worker_id}",
            description=f"Worker {worker_id} failed with error: {error}",
            action_type="fix",
            target_phase="phase2"
        )
        self.add_todo(todo)

    # === 分支处理 ===

    def handle_closure_allow(self) -> None:
        """处理 closure 检查通过的情况。

        分支 R1: closure_report.overall_status=allow 继续执行 patch-planner。
        """
        self._log_event("branch", "closure_allow", {})
        # 正常流程，patch-planner 将在 tick 中被调度

    def handle_closure_block(self, gaps: List[Dict[str, Any]]) -> None:
        """处理 closure 检查阻塞的情况。

        分支 R2: closure_report.overall_status=block 时挂起 patch-planner，
        生成验证 todo 并回到 Phase 2。
        """
        self._log_event("branch", "closure_block", {"gaps": gaps})

        # 标记 patch-planner 为 blocked
        self.state.worker_status["patch-planner"] = TaskStatus.PENDING  # 将由 todo 完成后解锁

        # 为每个 gap 生成 todo
        for gap in gaps:
            todo = TodoItem(
                todo_id=f"todo_gap_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{gap.get('id', '')}",
                source_type="closure",
                source_phase="phase3",
                priority=TodoPriority.P0,
                title=f"Resolve evidence gap: {gap.get('gap_type', 'unknown')}",
                description=gap.get("description", ""),
                action_type="verify",
                target_card=gap.get("card_type"),
                target_phase="phase2",
                blocks=["patch-planner"]
            )
            self.add_todo(todo)

        # 回退到 phase2
        self.state.current_phase = "phase2"
        self.state.phase_status = PhaseStatus.IN_PROGRESS

        self.save_state()

    def handle_patch_plan_invalid(self, issues: List[str]) -> None:
        """处理 patch_plan 无效的情况。

        分支 R3: patch_plan.json schema/字段校验失败，阻止 Phase5 继续执行。
        """
        self._log_event("branch", "patch_plan_invalid", {"issues": issues})

        # 生成修复 todo
        for issue in issues:
            todo = TodoItem(
                todo_id=f"todo_plan_fix_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
                source_type="validation",
                source_phase="phase4",
                priority=TodoPriority.P1,
                title=f"Fix patch plan issue: {issue[:50]}",
                description=issue,
                action_type="fix",
                target_phase="phase4",
                blocks=["patch-executor"]
            )
            self.add_todo(todo)

        self.save_state()

    # === 主调度循环 ===

    async def run(
        self,
        max_iterations: int = 100,
        fail_fast: bool = False,
        from_phase: Optional[str] = None
    ) -> ScheduleResult:
        """运行调度循环。

        Args:
            max_iterations: 最大迭代次数
            fail_fast: 是否在失败时立即停止
            from_phase: 从指定阶段开始（用于 --resume）

        Returns:
            ScheduleResult
        """
        self.get_or_create_state()

        if from_phase:
            self.state.current_phase = from_phase
            self.state.phase_status = PhaseStatus.IN_PROGRESS

        completed = []
        failed = []
        todos_generated = []
        todos_resolved = []

        iteration = 0
        while iteration < max_iterations:
            iteration += 1

            # 计算 ready set
            ready_workers, ready_todos = self.tick()

            # 检查是否完成
            if not ready_workers and not ready_todos:
                # 检查是否有正在运行的任务
                if not self._running_workers:
                    # 检查是否所有 worker 都完成或失败
                    all_workers = set(w.worker_id for w in self.registry.get_all())
                    pending = all_workers - self._completed_workers - self._failed_workers

                    if not pending:
                        break  # 完成

                    # 检查是否被阻塞
                    if self.state.todos:
                        blocked_todos = [t for t in self.state.todos if t.status == TodoStatus.BLOCKED]
                        if blocked_todos:
                            self.state.phase_status = PhaseStatus.BLOCKED
                            break

            # 执行 ready workers
            for worker_id in ready_workers:
                try:
                    task = await self.execute_worker(worker_id)
                    if task.status == TaskStatus.COMPLETED:
                        completed.append(worker_id)
                    else:
                        failed.append(worker_id)
                        if fail_fast:
                            break
                except Exception as e:
                    failed.append(worker_id)
                    self._log_event("error", "worker_exception", {
                        "worker_id": worker_id,
                        "error": str(e)
                    })
                    if fail_fast:
                        break

            # 执行 ready todos（简化处理，实际可能需要更多逻辑）
            for todo in ready_todos:
                todos_generated.append(todo.todo_id)
                # TODO: 实际执行 todo 的逻辑

        # 保存最终状态
        self.save_state()

        # 确定最终状态
        final_status = PhaseStatus.COMPLETED
        if failed:
            final_status = PhaseStatus.FAILED
        elif self.state.phase_status == PhaseStatus.BLOCKED:
            final_status = PhaseStatus.BLOCKED

        # 保存事件日志
        self._save_events_log()

        return ScheduleResult(
            success=len(failed) == 0,
            completed_workers=completed,
            failed_workers=failed,
            todos_generated=todos_generated,
            todos_resolved=todos_resolved,
            final_phase=self.state.current_phase,
            final_status=final_status,
            metrics={
                "iterations": iteration,
                "total_workers": len(self.registry.get_all()),
                "completed_count": len(completed),
                "failed_count": len(failed),
                "todos_count": len(self.state.todos)
            }
        )

    # === 日志 ===

    def _log_event(self, category: str, event_type: str, data: Dict[str, Any]) -> None:
        """记录事件。"""
        event = {
            "timestamp": datetime.utcnow().isoformat(),
            "instance_id": self.instance_id,
            "category": category,
            "event_type": event_type,
            "data": data
        }
        self._events.append(event)

    def _save_events_log(self) -> None:
        """保存事件日志。"""
        log_path = self.log_dir / "scheduler_events.jsonl"
        with open(log_path, 'a', encoding='utf-8') as f:
            for event in self._events:
                f.write(json.dumps(event, ensure_ascii=False) + '\n')

    def _save_todo_queue(self) -> None:
        """保存 todo 队列。"""
        data = {
            "instance_id": self.instance_id,
            "updated_at": datetime.utcnow().isoformat(),
            "todos": [t.model_dump() for t in self.state.todos]
        }
        self.todo_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    # === 统计 ===

    def get_statistics(self) -> Dict[str, Any]:
        """获取调度统计。"""
        if not self.state:
            return {"status": "not_initialized"}

        return {
            "instance_id": self.instance_id,
            "current_phase": self.state.current_phase,
            "phase_status": self.state.phase_status.value,
            "workers": {
                "total": len(self.registry.get_all()),
                "completed": len(self._completed_workers),
                "running": len(self._running_workers),
                "failed": len(self._failed_workers)
            },
            "todos": {
                "total": len(self.state.todos),
                "pending": len([t for t in self.state.todos if t.status == TodoStatus.PENDING]),
                "done": len([t for t in self.state.todos if t.status == TodoStatus.DONE]),
                "blocked": len([t for t in self.state.todos if t.status == TodoStatus.BLOCKED])
            },
            "card_versions": self.state.card_versions
        }
