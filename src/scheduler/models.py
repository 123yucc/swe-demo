"""Scheduler Data Models.

定义核心数据结构：
- WorkflowState: 工作流状态
- WorkerSpec/TaskSpec: Worker 和任务定义
- TodoItem: 待办事项
- WorkerRegistry: Worker 注册表
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Callable, Set
from pydantic import BaseModel, Field
from enum import Enum


class TodoStatus(str, Enum):
    """Todo 状态。"""
    PENDING = "pending"         # 等待执行
    READY = "ready"             # 可以执行
    RUNNING = "running"         # 正在执行
    DONE = "done"               # 完成
    BLOCKED = "blocked"         # 阻塞
    ABANDONED = "abandoned"     # 放弃


class TodoPriority(str, Enum):
    """Todo 优先级。"""
    P0 = "p0"  # Critical - closure gaps
    P1 = "p1"  # High - gate blockers
    P2 = "p2"  # Medium - quality improvements
    P3 = "p3"  # Low - optional


class PhaseStatus(str, Enum):
    """阶段状态。"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskStatus(str, Enum):
    """任务状态。"""
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRY = "retry"


# === TodoItem ===

class TodoItem(BaseModel):
    """待办事项。"""
    todo_id: str = Field(..., description="Todo ID")
    source_type: str = Field(..., description="来源类型 (closure/worker/runtime)")
    source_phase: str = Field(..., description="来源阶段")
    priority: TodoPriority = Field(default=TodoPriority.P2, description="优先级")
    status: TodoStatus = Field(default=TodoStatus.PENDING, description="状态")

    # 描述
    title: str = Field(..., description="标题")
    description: str = Field(default="", description="详细描述")

    # 执行信息
    action_type: str = Field(..., description="动作类型 (verify/fix/enhance/retry)")
    target_card: Optional[str] = Field(None, description="目标卡片类型")
    target_phase: Optional[str] = Field(None, description="目标阶段")

    # 依赖
    depends_on: List[str] = Field(default_factory=list, description="依赖的 todo_id")
    blocks: List[str] = Field(default_factory=list, description="阻塞的 todo_id")

    # 执行结果
    result: Optional[Dict[str, Any]] = Field(None, description="执行结果")
    error_info: Optional[Dict[str, Any]] = Field(None, description="错误信息")

    # 重试
    retry_count: int = Field(default=0, description="重试次数")
    max_retries: int = Field(default=3, description="最大重试次数")

    # 时间戳
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    started_at: Optional[str] = Field(None, description="开始时间")
    completed_at: Optional[str] = Field(None, description="完成时间")

    # 验证路径
    validation_path: Optional[str] = Field(None, description="验证路径")

    def is_ready(self) -> bool:
        """检查是否可以执行。"""
        return self.status == TodoStatus.PENDING and self.retry_count < self.max_retries

    def can_retry(self) -> bool:
        """检查是否可以重试。"""
        return self.retry_count < self.max_retries


# === WorkerSpec ===

class WorkerSpec(BaseModel):
    """Worker 规格定义。"""
    worker_id: str = Field(..., description="Worker ID")
    phase: str = Field(..., description="所属阶段")
    description: str = Field(default="", description="描述")

    # 依赖
    depends_on: List[str] = Field(default_factory=list, description="依赖的 worker_id")
    gate_conditions: List[str] = Field(default_factory=list, description="门禁条件")

    # 执行配置
    can_parallel: bool = Field(default=False, description="是否可并行")
    max_retries: int = Field(default=3, description="最大重试次数")
    timeout: int = Field(default=300, description="超时时间（秒）")

    # 输出
    produces_cards: List[str] = Field(default_factory=list, description="产出的卡片类型")
    produces_todos: List[str] = Field(default_factory=list, description="可能产生的 todo 类型")

    # 执行器
    executor: Optional[str] = Field(None, description="执行器函数名")

    def __hash__(self):
        return hash(self.worker_id)

    def __eq__(self, other):
        if isinstance(other, WorkerSpec):
            return self.worker_id == other.worker_id
        return False


# === TaskSpec ===

class TaskSpec(BaseModel):
    """任务规格。"""
    task_id: str = Field(..., description="任务 ID")
    worker_id: str = Field(..., description="对应的 worker")
    status: TaskStatus = Field(default=TaskStatus.PENDING, description="状态")

    # 输入输出
    inputs: Dict[str, Any] = Field(default_factory=dict, description="输入数据")
    outputs: Dict[str, Any] = Field(default_factory=dict, description="输出数据")

    # 执行信息
    started_at: Optional[str] = Field(None)
    completed_at: Optional[str] = Field(None)
    duration_ms: Optional[int] = Field(None)

    # 错误信息
    error: Optional[str] = Field(None)
    retry_count: int = Field(default=0)

    # 决策审计
    decisions: List[Dict[str, Any]] = Field(default_factory=list, description="决策记录")


# === WorkflowState ===

class WorkflowState(BaseModel):
    """工作流状态。"""
    instance_id: str = Field(..., description="实例 ID")
    schema_version: str = Field(default="1.0", description="Schema 版本")

    # 当前状态
    current_phase: str = Field(default="init", description="当前阶段")
    phase_status: PhaseStatus = Field(default=PhaseStatus.PENDING, description="阶段状态")

    # 阶段历史
    phase_history: List[Dict[str, Any]] = Field(default_factory=list, description="阶段历史")

    # Worker 状态
    worker_status: Dict[str, TaskStatus] = Field(default_factory=dict, description="Worker 状态")
    worker_results: Dict[str, Dict[str, Any]] = Field(default_factory=dict, description="Worker 结果")

    # Todo 队列
    todos: List[TodoItem] = Field(default_factory=list, description="Todo 列表")

    # 资源依赖
    resources: Dict[str, Any] = Field(default_factory=dict, description="资源状态")

    # 失败信息
    failure_reason: Optional[str] = Field(None, description="失败原因")
    failure_phase: Optional[str] = Field(None, description="失败阶段")

    # Cards 版本
    card_versions: Dict[str, int] = Field(default_factory=dict, description="卡片版本")

    # 时间戳
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

    def update_timestamp(self) -> None:
        """更新时间戳。"""
        self.updated_at = datetime.utcnow().isoformat()

    def add_phase_history(self, phase: str, status: str, details: Optional[Dict] = None) -> None:
        """添加阶段历史。"""
        self.phase_history.append({
            "phase": phase,
            "status": status,
            "timestamp": datetime.utcnow().isoformat(),
            "details": details or {}
        })
        self.update_timestamp()

    def get_pending_todos(self) -> List[TodoItem]:
        """获取待处理的 todo。"""
        return [t for t in self.todos if t.status in (TodoStatus.PENDING, TodoStatus.READY)]

    def get_todo_by_id(self, todo_id: str) -> Optional[TodoItem]:
        """根据 ID 获取 todo。"""
        for todo in self.todos:
            if todo.todo_id == todo_id:
                return todo
        return None

    @classmethod
    def load(cls, path: Path) -> "WorkflowState":
        """从文件加载状态。"""
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            # 处理枚举转换
            if "worker_status" in data:
                data["worker_status"] = {k: TaskStatus(v) for k, v in data["worker_status"].items()}
            if "todos" in data:
                data["todos"] = [TodoItem(**t) for t in data["todos"]]
            if "phase_status" in data:
                data["phase_status"] = PhaseStatus(data["phase_status"])
            return cls(**data)
        raise FileNotFoundError(f"Workflow state not found: {path}")

    def save(self, path: Path) -> None:
        """保存状态到文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )


# === WorkerRegistry ===

class WorkerRegistry:
    """Worker 注册表。

    管理所有已注册的 worker，提供 DAG 遍历和依赖解析。
    """

    def __init__(self):
        self._workers: Dict[str, WorkerSpec] = {}
        self._executors: Dict[str, Callable] = {}

    def register(self, spec: WorkerSpec, executor: Optional[Callable] = None) -> None:
        """注册 worker。"""
        self._workers[spec.worker_id] = spec
        if executor:
            self._executors[spec.worker_id] = executor

    def get(self, worker_id: str) -> Optional[WorkerSpec]:
        """获取 worker 规格。"""
        return self._workers.get(worker_id)

    def get_executor(self, worker_id: str) -> Optional[Callable]:
        """获取 worker 执行器。"""
        return self._executors.get(worker_id)

    def get_all(self) -> List[WorkerSpec]:
        """获取所有 worker。"""
        return list(self._workers.values())

    def get_by_phase(self, phase: str) -> List[WorkerSpec]:
        """获取指定阶段的 worker。"""
        return [w for w in self._workers.values() if w.phase == phase]

    def get_dependencies(self, worker_id: str) -> List[str]:
        """获取 worker 的依赖。"""
        worker = self._workers.get(worker_id)
        return worker.depends_on if worker else []

    def get_dependents(self, worker_id: str) -> List[str]:
        """获取依赖此 worker 的其他 worker。"""
        dependents = []
        for wid, spec in self._workers.items():
            if worker_id in spec.depends_on:
                dependents.append(wid)
        return dependents

    def topological_sort(self) -> List[str]:
        """拓扑排序。"""
        visited = set()
        result = []

        def visit(worker_id: str):
            if worker_id in visited:
                return
            visited.add(worker_id)
            worker = self._workers.get(worker_id)
            if worker:
                for dep in worker.depends_on:
                    visit(dep)
            result.append(worker_id)

        for worker_id in self._workers:
            visit(worker_id)

        return result

    def get_ready_workers(self, completed: Set[str], running: Set[str]) -> List[str]:
        """获取可以执行的 worker。

        Args:
            completed: 已完成的 worker 集合
            running: 正在运行的 worker 集合

        Returns:
            可以执行的 worker ID 列表
        """
        ready = []
        for worker_id, spec in self._workers.items():
            if worker_id in completed or worker_id in running:
                continue

            # 检查所有依赖是否完成
            all_deps_done = all(dep in completed for dep in spec.depends_on)
            if all_deps_done:
                ready.append(worker_id)

        return ready

    def build_dag(self) -> Dict[str, List[str]]:
        """构建 DAG 图。

        Returns:
            邻接表表示的 DAG（worker_id -> 依赖列表）
        """
        return {
            worker_id: spec.depends_on
            for worker_id, spec in self._workers.items()
        }


def create_default_registry() -> WorkerRegistry:
    """创建默认的 worker 注册表。

    包含 Phase 1-6 的所有 worker。
    """
    registry = WorkerRegistry()

    # Phase 1: Artifact Parsing
    registry.register(WorkerSpec(
        worker_id="artifact-parser",
        phase="phase1",
        description="Parse input artifacts and generate initial evidence cards",
        produces_cards=["symptom", "localization", "constraint", "structural"],
        can_parallel=False,
        executor="run_phase1_parsing"
    ))

    # Phase 2: Evidence Extraction
    registry.register(WorkerSpec(
        worker_id="symptom-extractor",
        phase="phase2",
        description="Extract and enhance symptom evidence",
        depends_on=["artifact-parser"],
        produces_cards=["symptom"],
        can_parallel=True
    ))

    registry.register(WorkerSpec(
        worker_id="localization-extractor",
        phase="phase2",
        description="Extract and enhance localization evidence",
        depends_on=["artifact-parser"],
        produces_cards=["localization"],
        can_parallel=True
    ))

    registry.register(WorkerSpec(
        worker_id="constraint-extractor",
        phase="phase2",
        description="Extract and enhance constraint evidence",
        depends_on=["artifact-parser"],
        produces_cards=["constraint"],
        can_parallel=True
    ))

    registry.register(WorkerSpec(
        worker_id="structural-extractor",
        phase="phase2",
        description="Extract and enhance structural evidence",
        depends_on=["artifact-parser"],
        produces_cards=["structural"],
        can_parallel=True
    ))

    # LLM Enhancement Layer
    registry.register(WorkerSpec(
        worker_id="llm-enhancer",
        phase="phase2",
        description="LLM enhancement for all evidence cards",
        depends_on=["symptom-extractor", "localization-extractor", "constraint-extractor", "structural-extractor"],
        produces_cards=["symptom", "localization", "constraint", "structural"],
        can_parallel=False
    ))

    # Phase 3: Closure Checking
    registry.register(WorkerSpec(
        worker_id="closure-checker",
        phase="phase3",
        description="Check evidence closure before patch planning",
        depends_on=["llm-enhancer"],
        gate_conditions=["evidence_sufficient"],
        produces_todos=["gap_verification"],
        can_parallel=False
    ))

    # Phase 4: Patch Planning
    registry.register(WorkerSpec(
        worker_id="patch-planner",
        phase="phase4",
        description="Create detailed patch plan",
        depends_on=["closure-checker"],
        gate_conditions=["closure_passed"],
        can_parallel=False
    ))

    # Phase 5: Patch Execution
    registry.register(WorkerSpec(
        worker_id="patch-executor",
        phase="phase5",
        description="Execute patch plan",
        depends_on=["patch-planner"],
        gate_conditions=["plan_valid"],
        can_parallel=False
    ))

    # Phase 6: Validation
    registry.register(WorkerSpec(
        worker_id="validator",
        phase="phase6",
        description="Validate patch results",
        depends_on=["patch-executor"],
        can_parallel=False
    ))

    return registry
