"""Phase 2: Evidence Extraction - 动态分析和置信度计算版本。

去除硬编码模式，使用：
- 文本分析 (grep) 定位代码
- AST分析 理解代码结构
- 动态置信度计算 (来源权重 + 匹配质量 + LLM评分)
"""

import re
import ast
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime

from .evidence_cards import (
    SymptomCard, LocalizationCard, ConstraintCard, StructuralCard,
    CandidateLocation, Constraint, DependencyEdge, CoEditGroup,
    EvidenceSource, SufficiencyStatus
)


class CodebaseNavigator:
    """代码库导航器，使用文本和AST分析。"""

    def __init__(self, repo_dir: str):
        self.repo_dir = Path(repo_dir)

    def grep_search(self, pattern: str, file_pattern: str = "*.py") -> List[Dict[str, Any]]:
        """使用rg进行文本搜索。"""
        results = []
        try:
            # 使用rg (ripgrep) 进行搜索
            cmd = ["rg", "-n", "--type", "py", pattern, str(self.repo_dir)]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')

            for line in result.stdout.strip().split('\n'):
                if ':' in line:
                    parts = line.split(':', 2)
                    if len(parts) >= 3:
                        file_path = Path(parts[0]).relative_to(self.repo_dir)
                        line_num = int(parts[1])
                        content = parts[2]
                        results.append({
                            "file_path": str(file_path),
                            "line_number": line_num,
                            "content": content.strip()
                        })
        except (subprocess.SubprocessError, FileNotFoundError):
            # Fallback: 使用Python的glob和搜索
            for py_file in self.repo_dir.rglob(file_pattern):
                try:
                    with open(py_file, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                    for i, line in enumerate(lines, 1):
                        if re.search(pattern, line):
                            results.append({
                                "file_path": str(py_file.relative_to(self.repo_dir)),
                                "line_number": i,
                                "content": line.strip()
                            })
                except Exception:
                    continue

        return results

    def find_function_definition(self, function_name: str, file_path: Optional[str] = None) -> List[Dict[str, Any]]:
        """使用AST查找函数定义。"""
        results = []

        search_files = []
        if file_path:
            full_path = self.repo_dir / file_path
            if full_path.exists():
                search_files.append(full_path)
        else:
            search_files = list(self.repo_dir.rglob("*.py"))

        for py_file in search_files:
            try:
                with open(py_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                tree = ast.parse(content)

                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if node.name == function_name:
                            results.append({
                                "file_path": str(py_file.relative_to(self.repo_dir)),
                                "symbol_name": node.name,
                                "symbol_type": "function",
                                "region_start": node.lineno,
                                "region_end": node.end_lineno,
                                "args": [arg.arg for arg in node.args.args],
                                "decorators": [ast.unparse(d) for d in node.decorator_list]
                            })

                    elif isinstance(node, ast.ClassDef):
                        if node.name == function_name:
                            methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                            results.append({
                                "file_path": str(py_file.relative_to(self.repo_dir)),
                                "symbol_name": node.name,
                                "symbol_type": "class",
                                "region_start": node.lineno,
                                "region_end": node.end_lineno,
                                "methods": methods
                            })

                        # Also check methods
                        for item in node.body:
                            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                if item.name == function_name:
                                    results.append({
                                        "file_path": str(py_file.relative_to(self.repo_dir)),
                                        "symbol_name": f"{node.name}.{item.name}",
                                        "symbol_type": "method",
                                        "region_start": item.lineno,
                                        "region_end": item.end_lineno,
                                        "class_name": node.name
                                    })
            except (SyntaxError, UnicodeDecodeError):
                continue

        return results

    def find_decorated_functions(self, decorator_pattern: str) -> List[Dict[str, Any]]:
        """查找具有特定装饰器的函数（如 @app.route）。"""
        results = []

        for py_file in self.repo_dir.rglob("*.py"):
            try:
                with open(py_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                tree = ast.parse(content)

                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for decorator in node.decorator_list:
                            decorator_str = ast.unparse(decorator)
                            if re.search(decorator_pattern, decorator_str):
                                results.append({
                                    "file_path": str(py_file.relative_to(self.repo_dir)),
                                    "symbol_name": node.name,
                                    "symbol_type": "function",
                                    "region_start": node.lineno,
                                    "region_end": node.end_lineno,
                                    "decorator": decorator_str,
                                    "args": [arg.arg for arg in node.args.args]
                                })
            except (SyntaxError, UnicodeDecodeError):
                continue

        return results

    def get_call_graph(self, function_name: str) -> List[Dict[str, Any]]:
        """获取函数调用图（哪些函数调用了它）。"""
        results = []

        for py_file in self.repo_dir.rglob("*.py"):
            try:
                with open(py_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                tree = ast.parse(content)

                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        if isinstance(node.func, ast.Name) and node.func.id == function_name:
                            # 找到调用，向上查找包含的函数
                            for parent in ast.walk(tree):
                                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                    if parent.lineno <= node.lineno <= parent.end_lineno:
                                        results.append({
                                            "file_path": str(py_file.relative_to(self.repo_dir)),
                                            "caller": parent.name,
                                            "callee": function_name,
                                            "line": node.lineno
                                        })
                                        break
            except (SyntaxError, UnicodeDecodeError):
                continue

        return results

    def find_class_methods(self, method_name: str) -> List[Dict[str, Any]]:
        """使用AST查找类方法（在任何类内定义的方法）。"""
        results = []

        for py_file in self.repo_dir.rglob("*.py"):
            try:
                with open(py_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                tree = ast.parse(content)

                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef):
                        # 在这个类中查找方法
                        for item in node.body:
                            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                if item.name == method_name:
                                    results.append({
                                        "file_path": str(py_file.relative_to(self.repo_dir)),
                                        "class_name": node.name,
                                        "symbol_name": item.name,
                                        "symbol_type": "method",
                                        "region_start": item.lineno,
                                        "region_end": item.end_lineno,
                                        "args": [arg.arg for arg in item.args.args],
                                        "decorators": [ast.unparse(d) for d in item.decorator_list]
                                    })
            except (SyntaxError, UnicodeDecodeError):
                continue

        return results


class DynamicSymptomExtractor:
    """Phase 2: 动态Symptom Evidence提取器。"""

    def __init__(self, workspace_dir: str):
        self.workspace_dir = Path(workspace_dir)
        self.repo_dir = self.workspace_dir / "repo"
        self.navigator = CodebaseNavigator(str(self.repo_dir))

    def extract(self, symptom_card_v1: Dict[str, Any]) -> SymptomCard:
        """提取和增强symptom evidence。"""
        now = datetime.utcnow().isoformat()

        # 从v1卡加载基础数据
        observed = symptom_card_v1.get("observed_failure", {})
        expected = symptom_card_v1.get("expected_behavior", {})

        # 1. 分析错误模式 - 动态从代码/日志中提取
        error_patterns = self._extract_error_patterns(observed)

        # 2. 定位stack trace中提到的代码
        stack_locations = self._locate_stack_trace(observed.get("stack_trace_summary", ""))

        # 3. 评估充分性
        sufficiency = self._assess_sufficiency_v2(observed, error_patterns, stack_locations)

        # 构建v2 card
        from .evidence_cards import ObservedFailure, ExpectedBehavior, EntityReference

        symptom_card = SymptomCard(
            version=2,
            updated_at=now,
            updated_by="symptom_extractor",
            observed_failure=ObservedFailure(
                description=observed.get("description", ""),
                trigger_condition=observed.get("trigger_condition"),
                exception_type=observed.get("exception_type"),
                stack_trace_summary=observed.get("stack_trace_summary"),
                error_message=observed.get("error_message"),
                evidence_source=self._build_evidence_sources("symptom", error_patterns, stack_locations)
            ),
            expected_behavior=ExpectedBehavior(
                description=expected.get("description", ""),
                grounded_in=expected.get("grounded_in", "problem_statement"),
                evidence_source=[EvidenceSource(
                    source_type="artifact",
                    source_path="expected_and_current_behavior.md",
                    confidence_contribution=0.8
                )]
            ),
            mentioned_entities=self._enhance_entities(symptom_card_v1.get("mentioned_entities", [])),
            hinted_scope=symptom_card_v1.get("hinted_scope"),
            sufficiency_status=sufficiency["status"],
            sufficiency_notes=sufficiency["notes"]
        )

        return symptom_card

    def _extract_error_patterns(self, observed: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从代码库中提取错误模式。"""
        patterns = []

        error_message = observed.get("error_message", "")
        exception_type = observed.get("exception_type", "")

        if error_message:
            # 搜索代码中的错误消息
            results = self.navigator.grep_search(re.escape(error_message[:50]))
            for r in results:
                patterns.append({
                    "pattern": error_message,
                    "location": r,
                    "source": "grep"
                })

        if exception_type:
            # 搜索异常类型
            results = self.navigator.grep_search(exception_type)
            for r in results:
                if "raise " + exception_type in r["content"] or "except " + exception_type in r["content"]:
                    patterns.append({
                        "pattern": exception_type,
                        "location": r,
                        "source": "ast"
                    })

        return patterns

    def _locate_stack_trace(self, stack_summary: str) -> List[Dict[str, Any]]:
        """从stack trace摘要定位代码位置。"""
        locations = []

        if not stack_summary:
            return locations

        # 解析stack trace中的文件路径和行号
        # 格式如: "File \"path/to/file.py\", line 42, in function_name"
        pattern = r'File "([^"]+)"[^\d]*(\d+)'
        matches = re.findall(pattern, stack_summary)

        for file_path, line_num in matches:
            locations.append({
                "file_path": file_path,
                "line_number": int(line_num),
                "source": "stack_trace"
            })

        return locations

    def _enhance_entities(self, entities_v1: List[Dict[str, Any]]) -> List[Any]:
        """增强实体信息。"""
        from .evidence_cards import EntityReference

        enhanced = []
        for ent in entities_v1:
            name = ent.get("name", "")
            entity_type = ent.get("type", "unknown")

            # 尝试在代码中找到这个实体
            definitions = self.navigator.find_function_definition(name)

            if definitions:
                for d in definitions:
                    confidence = self._compute_confidence_v2(
                        source_type="code",
                        match_quality="exact",
                        has_context=True
                    )
                    enhanced.append(EntityReference(
                        name=name,
                        type=entity_type,
                        file_path=d.get("file_path"),
                        line_number=d.get("region_start"),
                        evidence_source=[EvidenceSource(
                            source_type="repo",
                            source_path=d.get("file_path", ""),
                            matching_detail=d,
                            confidence_contribution=confidence
                        )],
                        computed_confidence=confidence
                    ))
            else:
                # 保持v1的信息
                confidence = self._compute_confidence_v2(
                    source_type="artifact",
                    match_quality="fuzzy",
                    has_context=False
                )
                enhanced.append(EntityReference(
                    name=name,
                    type=entity_type,
                    file_path=ent.get("file_path"),
                    line_number=ent.get("line_number"),
                    evidence_source=[EvidenceSource(
                        source_type="problem_statement",
                        source_path="problem_statement.md",
                        confidence_contribution=confidence
                    )],
                    computed_confidence=confidence
                ))

        return enhanced

    def _compute_confidence_v2(self, source_type: str, match_quality: str, has_context: bool = True) -> float:
        """Phase 2动态置信度计算。"""
        # 来源权重
        source_weights = {
            "interface": 1.0,
            "code": 0.95,
            "test": 0.9,
            "repo": 0.85,
            "stack_trace": 0.9,
            "artifact": 0.7,
            "heuristic": 0.5
        }

        # 匹配质量
        match_weights = {
            "exact": 1.0,
            "strong": 0.9,
            "fuzzy": 0.7,
            "weak": 0.5
        }

        base = source_weights.get(source_type, 0.5)
        match = match_weights.get(match_quality, 0.5)
        context = 1.0 if has_context else 0.8

        return round(base * match * context, 2)

    def _build_evidence_sources(self, category: str, patterns: List[Dict], locations: List[Dict]) -> List[EvidenceSource]:
        """构建证据来源列表。"""
        sources = []

        for p in patterns:
            loc = p.get("location", {})
            sources.append(EvidenceSource(
                source_type="repo" if p.get("source") == "grep" else "code",
                source_path=loc.get("file_path", ""),
                matching_detail=p,
                confidence_contribution=0.85
            ))

        for loc in locations:
            sources.append(EvidenceSource(
                source_type="stack_trace",
                source_path=loc.get("file_path", ""),
                matching_detail=loc,
                confidence_contribution=0.9
            ))

        return sources

    def _assess_sufficiency_v2(self, observed: Dict, patterns: List, locations: List) -> Dict[str, Any]:
        """评估Symptom evidence充分性。"""
        notes = []

        has_reproducible_failure = bool(observed.get("description"))
        has_trigger = bool(observed.get("trigger_condition"))
        has_expected = bool(observed.get("expected_behavior"))

        if not has_reproducible_failure:
            notes.append("缺少可复现的失败描述")
        if not has_trigger:
            notes.append("缺少触发条件")
        if not has_expected:
            notes.append("缺少预期行为")
        if not locations and observed.get("stack_trace_summary"):
            notes.append("stack trace中未定位到代码")

        if len(notes) == 0:
            status = SufficiencyStatus.SUFFICIENT
        elif len(notes) <= 2:
            status = SufficiencyStatus.PARTIAL
        else:
            status = SufficiencyStatus.INSUFFICIENT

        return {
            "status": status,
            "notes": "; ".join(notes) if notes else "Complete symptom analysis"
        }


class DynamicLocalizationExtractor:
    """Phase 2: 动态Localization Evidence提取器。"""

    def __init__(self, workspace_dir: str):
        self.workspace_dir = Path(workspace_dir)
        self.repo_dir = self.workspace_dir / "repo"
        self.navigator = CodebaseNavigator(str(self.repo_dir))
        self.artifacts_dir = self.workspace_dir / "artifacts"

    def extract(self, symptom_card: Dict[str, Any]) -> LocalizationCard:
        """提取localization evidence。"""
        now = datetime.utcnow().isoformat()

        # 从symptom card获取anchors
        entities = symptom_card.get("mentioned_entities", [])
        initial_anchors = symptom_card.get("mentioned_entities", [])

        candidate_locations = []

        # 1. 文本grep搜索
        for entity in entities:
            name = entity.get("name", "")
            if name:
                grep_results = self.navigator.grep_search(name)
                for r in grep_results:
                    confidence = self._compute_location_confidence(
                        match_type="grep",
                        symbol_type=entity.get("type", "unknown"),
                        has_full_context=False
                    )
                    candidate_locations.append(CandidateLocation(
                        file_path=r["file_path"],
                        symbol_name=name,
                        symbol_type=entity.get("type"),
                        region_start=r["line_number"],
                        evidence_source=[EvidenceSource(
                            source_type="grep",
                            source_path=r["file_path"],
                            matching_detail=r,
                            confidence_contribution=confidence
                        )],
                        computed_confidence=confidence
                    ))

        # 2. AST分析
        for entity in entities:
            name = entity.get("name", "")
            if name:
                ast_results = self.navigator.find_function_definition(name)
                for r in ast_results:
                    confidence = self._compute_location_confidence(
                        match_type="ast",
                        symbol_type=r.get("symbol_type", "unknown"),
                        has_full_context=True
                    )
                    candidate_locations.append(CandidateLocation(
                        file_path=r["file_path"],
                        symbol_name=r["symbol_name"],
                        symbol_type=r["symbol_type"],
                        region_start=r["region_start"],
                        region_end=r.get("region_end"),
                        evidence_source=[EvidenceSource(
                            source_type="ast",
                            source_path=r["file_path"],
                            matching_detail=r,
                            confidence_contribution=confidence
                        )],
                        computed_confidence=confidence
                    ))

        # 3. 搜索装饰器模式（如 @app.route）
        decorated = self.navigator.find_decorated_functions(r"@.*\.route")
        for d in decorated:
            confidence = self._compute_location_confidence(
                match_type="ast_decorator",
                symbol_type="function",
                has_full_context=True
            )
            candidate_locations.append(CandidateLocation(
                file_path=d["file_path"],
                symbol_name=d["symbol_name"],
                symbol_type="route_handler",
                region_start=d["region_start"],
                region_end=d["region_end"],
                evidence_source=[EvidenceSource(
                    source_type="ast",
                    source_path=d["file_path"],
                    matching_detail=d,
                    confidence_contribution=confidence
                )],
                computed_confidence=confidence
            ))

        # 4. 生成接口到代码的映射
        interface_mappings = self._generate_interface_mappings()
        
        # 5. 生成测试到代码的映射
        test_mappings = self._generate_test_to_code_mappings()

        # 去重并按置信度排序
        seen = set()
        unique_locations = []
        for loc in candidate_locations:
            key = (loc.file_path, loc.symbol_name)
            if key not in seen:
                seen.add(key)
                unique_locations.append(loc)

        unique_locations.sort(key=lambda x: x.computed_confidence, reverse=True)

        # 评估充分性
        sufficiency = self._assess_localization_sufficiency(unique_locations)

        return LocalizationCard(
            version=2,
            updated_at=now,
            updated_by="localization_extractor",
            candidate_locations=unique_locations[:20],  # 限制数量
            test_to_code_mappings=test_mappings,  # 使用动态生成的映射
            interface_to_code_mappings=interface_mappings,
            sufficiency_status=sufficiency["status"],
            sufficiency_notes=sufficiency["notes"]
        )

    def _compute_location_confidence(self, match_type: str, symbol_type: str, has_full_context: bool) -> float:
        """计算定位置信度。"""
        # 匹配类型权重
        type_weights = {
            "ast": 0.95,
            "ast_decorator": 0.9,
            "grep": 0.7,
            "heuristic": 0.5
        }

        # 符号类型权重
        symbol_weights = {
            "function": 1.0,
            "method": 0.95,
            "class": 0.9,
            "route_handler": 0.9,
            "unknown": 0.6
        }

        base = type_weights.get(match_type, 0.5)
        symbol = symbol_weights.get(symbol_type, 0.6)
        context = 1.0 if has_full_context else 0.8

        return round(base * symbol * context, 2)

    def _generate_interface_mappings(self) -> Dict[str, str]:
        """生成接口到代码的映射。
        
        支持两种格式：
        1. REST API routes: GET /path/to/resource
        2. 方法定义: Type: Method, Name: method_name, Filepath: ...
        
        注意：处理转义的\\n（字面上的反斜线和n），将其转换为真实换行符
        """
        mappings = {}
        artifacts_dir = Path(self.workspace_dir) / "artifacts"

        # 读取interface.md或new_interfaces.md
        interface_path = artifacts_dir / "interface.md"
        if not interface_path.exists():
            interface_path = artifacts_dir / "new_interfaces.md"

        if not interface_path.exists():
            return mappings

        try:
            content = interface_path.read_text(encoding='utf-8')
            # 处理转义的换行符：字面上的 \n（两个字符）转换为实际换行符
            content = content.replace(r'\n', '\n')
        except:
            return mappings

        # 方法1: 提取REST API路由 (GET /path, POST /path等)
        route_pattern = r'([A-Z]+)\s+(/[^\s\n]+)'
        routes = re.findall(route_pattern, content)

        for method, path in routes:
            # 搜索对应的装饰器handler
            handler_pattern = path.replace("/", r"\/").replace("{", r"\{")
            decorated = self.navigator.find_decorated_functions(handler_pattern)

            if decorated:
                d = decorated[0]
                mappings[f"{method} {path}"] = f"{d['file_path']} -> {d['symbol_name']}()"

        # 方法2: 提取方法定义格式 (Type: Method, Name: xxx, Filepath: xxx)
        # 按 "Type:" 分割成多个段落
        parts = content.split('Type:')
        
        for part in parts[1:]:  # 跳过第一个分割前的内容
            lines = part.split('\n')
            
            # 提取当前段落的字段
            type_val = lines[0].strip() if lines else ""
            name_val = None
            filepath_val = None
            
            for line in lines[1:]:
                if line.startswith('Name:'):
                    name_val = line.split('Name:')[1].strip()
                elif line.startswith('Filepath:'):
                    filepath_val = line.split('Filepath:')[1].strip()
                elif line.startswith('Type:'):
                    # 遇到下一个Type：，停止处理当前段落
                    break
            
            # 动态生成映射
            if 'Method' in type_val and name_val:
                # 在代码中搜索这个方法（先搜索顶层函数，再搜索类方法）
                ast_results = self.navigator.find_function_definition(name_val)
                
                found = False
                if ast_results:
                    result = ast_results[0]
                    mappings[f"Method: {name_val}"] = (
                        f"{result['file_path']} (line {result['region_start']})"
                    )
                    found = True
                
                # 如果没找到顶层函数，尝试找类方法
                if not found:
                    class_methods = self.navigator.find_class_methods(name_val)
                    if class_methods:
                        m = class_methods[0]
                        mappings[f"Method: {name_val}"] = (
                            f"{m['file_path']} (class: {m['class_name']}, line {m['region_start']})"
                        )
                        found = True

        return mappings

    def _generate_test_to_code_mappings(self) -> Dict[str, str]:
        """生成测试到代码的映射。
        
        SWE-Bench-Pro通常不提供测试文件，但可以尝试从test目录推断。
        """
        mappings = {}
        test_dir = Path(self.workspace_dir) / "artifacts" / "tests"
        
        if test_dir.exists():
            # 查找fail2pass和pass2pass测试
            for test_type in ["fail2pass", "pass2pass"]:
                type_dir = test_dir / test_type
                if type_dir.exists():
                    for test_file in type_dir.glob("*.py"):
                        try:
                            with open(test_file, 'r', encoding='utf-8') as f:
                                content = f.read()
                            
                            # 提取test_xxx函数名
                            test_functions = re.findall(r'def (test_[_a-zA-Z0-9]+)', content)
                            
                            for test_func in test_functions:
                                # 根据test名称推断被测试的代码
                                # 例如 test_set_detection_sensitivity -> set_detection_sensitivity
                                code_name = test_func.replace('test_', '')
                                
                                # 搜索代码
                                ast_results = self.navigator.find_function_definition(code_name)
                                if ast_results:
                                    mappings[f"{test_file.name}::{test_func}"] = (
                                        f"{ast_results[0]['file_path']}::{code_name}"
                                    )
                        except:
                            continue
        
        return mappings

    def _assess_localization_sufficiency(self, locations: List[CandidateLocation]) -> Dict[str, Any]:
        """评估Localization充分性。"""
        notes = []

        if not locations:
            notes.append("未找到候选位置")
            return {"status": SufficiencyStatus.INSUFFICIENT, "notes": "; ".join(notes)}

        high_confidence = [l for l in locations if l.computed_confidence >= 0.8]

        if not high_confidence:
            notes.append("缺少高置信度的候选位置")

        has_precise_location = any(
            l.region_start and l.symbol_name for l in locations
        )

        if not has_precise_location:
            notes.append("候选位置不够精确（缺少行号或符号名）")

        if len(notes) == 0:
            status = SufficiencyStatus.SUFFICIENT
        elif len(notes) == 1:
            status = SufficiencyStatus.PARTIAL
        else:
            status = SufficiencyStatus.INSUFFICIENT

        return {
            "status": status,
            "notes": "; ".join(notes) if notes else f"Found {len(locations)} candidate locations, {len(high_confidence)} high confidence"
        }


class DynamicConstraintExtractor:
    """Phase 2: 动态Constraint Evidence提取器。"""

    def __init__(self, workspace_dir: str):
        self.workspace_dir = Path(workspace_dir)
        self.repo_dir = self.workspace_dir / "repo"
        self.navigator = CodebaseNavigator(str(self.repo_dir))
        self.artifacts_dir = self.workspace_dir / "artifacts"

    def extract(self, constraint_card_v1: Dict[str, Any]) -> ConstraintCard:
        """提取和增强constraint evidence。"""
        now = datetime.utcnow().isoformat()

        # 从artifacts中提取代码级约束
        code_constraints = self._extract_code_constraints()
        type_constraints = self._extract_type_constraints()

        # 合并v1和v2的约束
        all_constraints = []

        # 添加v1的约束
        v1_constraints = constraint_card_v1.get("constraints", [])
        for c in v1_constraints:
            all_constraints.append(Constraint(
                type=c.get("type", "requirement"),
                description=c.get("description", ""),
                source=c.get("source", "requirements"),
                severity=c.get("severity", "must"),
                evidence_source=[EvidenceSource(
                    source_type="artifact",
                    source_path="requirements.md",
                    confidence_contribution=0.85
                )]
            ))

        # 添加代码级约束
        for cc in code_constraints:
            all_constraints.append(cc)

        # 评估充分性
        sufficiency = self._assess_constraint_sufficiency(all_constraints, type_constraints)

        # 构建v2 card
        return ConstraintCard(
            version=2,
            updated_at=now,
            updated_by="constraint_extractor",
            must_do=constraint_card_v1.get("must_do", []),
            must_not_break=constraint_card_v1.get("must_not_break", []),
            allowed_behavior=constraint_card_v1.get("allowed_behavior", []),
            forbidden_behavior=constraint_card_v1.get("forbidden_behavior", []),
            compatibility_expectations=constraint_card_v1.get("compatibility_expectations", []),
            edge_case_obligations=constraint_card_v1.get("edge_case_obligations", []),
            constraints=all_constraints,
            api_signatures=constraint_card_v1.get("api_signatures", {}),
            type_constraints=type_constraints,
            backward_compatibility=True,  # 从requirements推断
            compatibility_notes="Backward compatibility required per requirements",
            sufficiency_status=sufficiency["status"],
            sufficiency_notes=sufficiency["notes"]
        )

    def _extract_code_constraints(self) -> List[Constraint]:
        """从代码中提取约束。"""
        constraints = []

        # 1. 从装饰器提取（如 @validate, @require_auth）
        validated = self.navigator.find_decorated_functions(r"@.*\.validate|@.*\.require")
        for v in validated:
            constraints.append(Constraint(
                type="validation",
                description=f"Function {v['symbol_name']} has validation decorator: {v.get('decorator', '')}",
                source="code",
                severity="must",
                evidence_source=[EvidenceSource(
                    source_type="ast",
                    source_path=v["file_path"],
                    matching_detail=v,
                    confidence_contribution=0.9
                )]
            ))

        # 2. 从assert语句提取
        for py_file in self.repo_dir.rglob("*.py"):
            try:
                with open(py_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                tree = ast.parse(content)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assert):
                        assert_str = ast.unparse(node.test)
                        constraints.append(Constraint(
                            type="assertion",
                            description=f"Assertion: {assert_str}",
                            source="code",
                            severity="must",
                            evidence_source=[EvidenceSource(
                                source_type="ast",
                                source_path=str(py_file.relative_to(self.repo_dir)),
                                matching_detail={"line": node.lineno, "assert": assert_str},
                                confidence_contribution=0.95
                            )]
                        ))
            except (SyntaxError, UnicodeDecodeError):
                continue

        # 3. 从docstring提取约束
        for py_file in self.repo_dir.rglob("*.py"):
            try:
                with open(py_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                tree = ast.parse(content)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        docstring = ast.get_docstring(node)
                        if docstring and ("must" in docstring.lower() or "should" in docstring.lower()):
                            constraints.append(Constraint(
                                type="docstring",
                                description=f"{node.name}: {docstring[:100]}...",
                                source="code",
                                severity="should",
                                evidence_source=[EvidenceSource(
                                    source_type="ast",
                                    source_path=str(py_file.relative_to(self.repo_dir)),
                                    matching_detail={"function": node.name},
                                    confidence_contribution=0.7
                                )]
                            ))
            except (SyntaxError, UnicodeDecodeError):
                continue

        return constraints

    def _extract_type_constraints(self) -> Dict[str, str]:
        """从类型注解提取约束。"""
        type_constraints = {}

        for py_file in self.repo_dir.rglob("*.py"):
            try:
                with open(py_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                tree = ast.parse(content)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        # 参数类型
                        for arg in node.args.args:
                            if arg.annotation:
                                type_str = ast.unparse(arg.annotation)
                                type_constraints[f"{node.name}.{arg.arg}"] = type_str

                        # 返回类型
                        if node.returns:
                            return_type = ast.unparse(node.returns)
                            type_constraints[f"{node.name}.return"] = return_type

                    # Pydantic/dataclass模型
                    elif isinstance(node, ast.ClassDef):
                        for item in node.body:
                            if isinstance(item, ast.AnnAssign):
                                if hasattr(item.target, 'id'):
                                    type_str = ast.unparse(item.annotation) if item.annotation else "Any"
                                    type_constraints[f"{node.name}.{item.target.id}"] = type_str

            except (SyntaxError, UnicodeDecodeError):
                continue

        return type_constraints

    def _assess_constraint_sufficiency(self, constraints: List[Constraint], type_constraints: Dict) -> Dict[str, Any]:
        """评估Constraint充分性。"""
        notes = []

        has_api_constraints = any(c.type in ["api", "interface"] for c in constraints)
        has_type_constraints = len(type_constraints) > 0
        has_must_constraints = any(c.severity == "must" for c in constraints)

        if not has_api_constraints:
            notes.append("缺少API级约束")
        if not has_type_constraints:
            notes.append("缺少类型约束")
        if not has_must_constraints:
            notes.append("缺少must级约束")

        if len(notes) == 0:
            status = SufficiencyStatus.SUFFICIENT
        elif len(notes) == 1:
            status = SufficiencyStatus.PARTIAL
        else:
            status = SufficiencyStatus.INSUFFICIENT

        return {
            "status": status,
            "notes": "; ".join(notes) if notes else f"Found {len(constraints)} constraints, {len(type_constraints)} type annotations"
        }


class DynamicStructuralExtractor:
    """Phase 2: 动态Structural Evidence提取器。"""

    def __init__(self, workspace_dir: str):
        self.workspace_dir = Path(workspace_dir)
        self.repo_dir = self.workspace_dir / "repo"
        self.navigator = CodebaseNavigator(str(self.repo_dir))

    def extract(self, localization_card_v2: Dict[str, Any]) -> StructuralCard:
        """提取structural evidence。"""
        now = datetime.utcnow().isoformat()

        # 从候选位置获取符号
        candidate_locations = localization_card_v2.get("candidate_locations", [])

        # 分析依赖关系
        dependency_edges = self._analyze_dependencies(candidate_locations)

        # 识别协同编辑组
        co_edit_groups = self._identify_co_edit_groups(candidate_locations, dependency_edges)

        # 评估传播风险
        propagation_risks = self._assess_propagation_risks(dependency_edges)

        # 评估充分性
        sufficiency = self._assess_structural_sufficiency(dependency_edges, co_edit_groups)

        return StructuralCard(
            version=2,
            updated_at=now,
            updated_by="structural_extractor",
            dependency_edges=dependency_edges,
            co_edit_groups=co_edit_groups,
            propagation_risks=propagation_risks,
            sufficiency_status=sufficiency["status"],
            sufficiency_notes=sufficiency["notes"]
        )

    def _analyze_dependencies(self, locations: List[Dict[str, Any]]) -> List[DependencyEdge]:
        """分析候选位置的依赖关系。"""
        edges = []

        for loc in locations:
            symbol_name = loc.get("symbol_name", "")
            file_path = loc.get("file_path", "")

            if not symbol_name or not file_path:
                continue

            # 提取纯函数名（去除类名前缀）
            func_name = symbol_name.split(".")[-1]

            # 查找调用者
            callers = self.navigator.get_call_graph(func_name)
            for caller in callers:
                edges.append(DependencyEdge(
                    from_entity=caller["caller"],
                    to_entity=func_name,
                    edge_type="caller-callee",
                    strength="strong" if caller["file_path"] == file_path else "medium",
                    evidence_source=[EvidenceSource(
                        source_type="ast",
                        source_path=caller["file_path"],
                        matching_detail=caller,
                        confidence_contribution=0.85
                    )]
                ))

            # 查找导入关系
            self._find_import_relationships(symbol_name, file_path, edges)

        return edges

    def _find_import_relationships(self, symbol_name: str, file_path: str, edges: List[DependencyEdge]):
        """查找导入关系。"""
        # 搜索导入该符号的文件
        import_pattern = rf"from\s+\S+\s+import\s+.*{symbol_name}|import\s+.*{symbol_name}"
        results = self.navigator.grep_search(import_pattern)

        for r in results:
            if r["file_path"] != file_path:
                edges.append(DependencyEdge(
                    from_entity=r["file_path"],
                    to_entity=symbol_name,
                    edge_type="import",
                    strength="medium",
                    evidence_source=[EvidenceSource(
                        source_type="grep",
                        source_path=r["file_path"],
                        matching_detail=r,
                        confidence_contribution=0.7
                    )]
                ))

    def _identify_co_edit_groups(self, locations: List[Dict], edges: List[DependencyEdge]) -> List[CoEditGroup]:
        """识别需要协同编辑的组。"""
        groups = []
        group_id = 0

        # 1. 基于强依赖关系分组
        strong_deps = [e for e in edges if e.strength == "strong"]
        grouped_entities = set()

        for edge in strong_deps:
            if edge.from_entity not in grouped_entities and edge.to_entity not in grouped_entities:
                group_id += 1
                groups.append(CoEditGroup(
                    group_id=f"co_edit_{group_id}",
                    entities=[edge.from_entity, edge.to_entity],
                    reason=f"Strong dependency: {edge.edge_type}",
                    evidence_source=[EvidenceSource(
                        source_type="ast",
                        source_path="multiple",
                        confidence_contribution=0.8
                    )]
                ))
                grouped_entities.add(edge.from_entity)
                grouped_entities.add(edge.to_entity)

        # 2. 基于文件位置分组
        file_groups = {}
        for loc in locations:
            file_path = loc.get("file_path", "")
            symbol = loc.get("symbol_name", "")
            if file_path and symbol:
                if file_path not in file_groups:
                    file_groups[file_path] = []
                file_groups[file_path].append(symbol)

        for file_path, symbols in file_groups.items():
            if len(symbols) > 1:
                group_id += 1
                groups.append(CoEditGroup(
                    group_id=f"co_edit_file_{group_id}",
                    entities=symbols,
                    reason=f"Co-located in {file_path}",
                    evidence_source=[EvidenceSource(
                        source_type="ast",
                        source_path=file_path,
                        confidence_contribution=0.75
                    )]
                ))

        return groups

    def _assess_propagation_risks(self, edges: List[DependencyEdge]) -> List[str]:
        """评估修改的传播风险。"""
        risks = []

        for edge in edges:
            if edge.edge_type == "caller-callee" and edge.strength == "strong":
                risks.append(f"Changes to {edge.to_entity} may affect {edge.from_entity}")
            elif edge.edge_type == "import":
                risks.append(f"Changes to {edge.to_entity} may affect importing module {edge.from_entity}")

        # 添加通用风险
        if edges:
            risks.append("API changes may break external consumers")

        return list(set(risks))  # 去重

    def _assess_structural_sufficiency(self, edges: List[DependencyEdge], groups: List[CoEditGroup]) -> Dict[str, Any]:
        """评估Structural充分性。"""
        notes = []

        if not edges:
            notes.append("未分析依赖关系")
        if not groups:
            notes.append("未识别协同编辑组")

        # 检查是否有高置信度的依赖边
        high_confidence_edges = [e for e in edges if any(s.confidence_contribution >= 0.8 for s in e.evidence_source)]
        if not high_confidence_edges and edges:
            notes.append("缺少高置信度的依赖分析")

        if len(notes) == 0:
            status = SufficiencyStatus.SUFFICIENT
        elif len(notes) <= 2:
            status = SufficiencyStatus.PARTIAL
        else:
            status = SufficiencyStatus.INSUFFICIENT

        return {
            "status": status,
            "notes": "; ".join(notes) if notes else f"Found {len(edges)} dependency edges, {len(groups)} co-edit groups"
        }


# Convenience function for running Phase 2
def run_phase2_extraction_dynamic(workspace_dir: str, instance_id: str) -> Dict[str, Any]:
    """运行Phase 2 evidence extraction（动态版本）。"""
    import json

    # 修复：检查workspace_dir是否已包含instance_id
    workspace_path = Path(workspace_dir)
    if workspace_path.name == instance_id:
        # workspace_dir已经是实例目录，直接使用
        workspace = workspace_path
    else:
        # workspace_dir是基目录，需要添加instance_id
        workspace = workspace_path / instance_id

    evidence_dir = workspace / "evidence"
    versions_dir = evidence_dir / "card_versions"

    # 加载Phase 1的cards
    with open(evidence_dir / "symptom_card.json", encoding='utf-8') as f:
        symptom_card_v1 = json.load(f)

    with open(evidence_dir / "localization_card.json", encoding='utf-8') as f:
        localization_card_v1 = json.load(f)

    with open(evidence_dir / "constraint_card.json", encoding='utf-8') as f:
        constraint_card_v1 = json.load(f)

    with open(evidence_dir / "structural_card.json", encoding='utf-8') as f:
        structural_card_v1 = json.load(f)

    results = {}

    # 1. Symptom Extractor
    symptom_extractor = DynamicSymptomExtractor(str(workspace))
    results["symptom"] = symptom_extractor.extract(symptom_card_v1)

    # 2. Localization Extractor
    localization_extractor = DynamicLocalizationExtractor(str(workspace))
    results["localization"] = localization_extractor.extract(symptom_card_v1)

    # 3. Constraint Extractor
    constraint_extractor = DynamicConstraintExtractor(str(workspace))
    results["constraint"] = constraint_extractor.extract(constraint_card_v1)

    # 4. Structural Extractor
    structural_extractor = DynamicStructuralExtractor(str(workspace))
    results["structural"] = structural_extractor.extract(results["localization"].model_dump())

    # 保存更新后的cards和版本历史
    v2_dir = versions_dir / "v2"
    v2_dir.mkdir(parents=True, exist_ok=True)
    
    for card_type, card_data in results.items():
        # 保存当前版本到evidence目录
        card_path = evidence_dir / f"{card_type}_card.json"
        with open(card_path, 'w', encoding='utf-8') as f:
            f.write(card_data.model_dump_json(indent=2))

        # 保存版本历史到v2目录
        version_path = v2_dir / f"{card_type}_card_v2.json"
        with open(version_path, 'w', encoding='utf-8') as f:
            f.write(card_data.model_dump_json(indent=2))

    return results
