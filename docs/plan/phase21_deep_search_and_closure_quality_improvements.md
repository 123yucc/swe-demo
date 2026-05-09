# Phase 21: Deep-Search与Closure-Checker质量改进

## 背景

Phase 20完成后，在三个真实issue的测试中暴露了证据收集阶段的系统性问题：

**测试结果**：
- issue_001 (NodeBB): EVIDENCE_INCOMPLETE - 用完30次deep-search预算，生成patch但closure-checker未批准
- issue_002 (face_recognition): EVIDENCE_INCOMPLETE - 12次deep-search后放弃，未生成patch
- issue_003 (WebFinger): EVIDENCE_INCOMPLETE - 24次deep-search，3轮rework全部失败

**核心问题**：
1. Deep-search agent对"新接口"判断错误，将待实现接口标记为AS_IS_COMPLIANT但无法提供证据位置
2. Deep-search的prescriptive fix缺少边界情况考虑，被closure-checker的boundary check拒绝
3. Orchestrator的I2 invariant检查陷入循环，浪费大量预算（issue_001的iter 15-30都在循环）
4. Deep-search的self-reflection未能有效纠正上述问题

**Phase 21目标**：
- 修复deep-search对新接口的判断逻辑
- 强化AS_IS_COMPLIANT的证据要求
- 优化orchestrator的I2 invariant处理
- 提升deep-search的boundary enumeration能力

---

## 1. Deep-Search Agent改进

### 1.1 新接口判断逻辑强化

**问题诊断**：
```python
# issue_001中的典型错误
req-015: {
  "origin": "new_interfaces",
  "verdict": "AS_IS_COMPLIANT",  # ❌ 错误：新接口不可能已存在
  "evidence_locations": []        # ❌ 无证据位置
}
```

Deep-search在判断新接口时：
- 找到了**相似但不完全匹配**的函数（如`getUserField`而非`getEmailForValidation`）
- 错误地将"部分功能存在"判断为"接口已实现"
- Self-reflection未能纠正这个逻辑错误

**改进方案**：

#### 1.1.1 System Prompt增强

在`DEEP_SEARCH_SYSTEM_PROMPT`中添加新接口判断规则：

```python
DEEP_SEARCH_SYSTEM_PROMPT = """\
...existing content...

NEW INTERFACE VERIFICATION (critical):
When the requirement origin is "new_interfaces":
1. Extract the EXACT function/method signature from the requirement text
   - Function name must match exactly (not just similar)
   - Input parameters must match exactly (types and names)
   - Output type must match exactly
   - Path must match exactly

2. Verdict rules for new interfaces:
   - AS_IS_COMPLIANT: ONLY if you find the EXACT signature at the EXACT path
     * You MUST provide evidence_locations pointing to the exact definition
     * Partial matches (similar name, similar params) are NOT compliant
   
   - TO_BE_MISSING: If the interface does not exist at all
     * No similar function exists
     * evidence_locations should be empty
   
   - TO_BE_PARTIAL: If a similar but incomplete implementation exists
     * Function name matches but signature differs
     * Function exists but at wrong path
     * Provide evidence_locations to the partial implementation

3. FORBIDDEN for new interfaces:
   - DO NOT mark AS_IS_COMPLIANT if you only found similar functionality
   - DO NOT mark AS_IS_COMPLIANT without evidence_locations
   - DO NOT infer "it must exist somewhere" without verification

Example:
Requirement: "Type: Function, Name: user.email.getEmailForValidation, 
              Path: src/user/email.js, Input: uid: number, 
              Output: Promise<string | null>"

✓ CORRECT: Found exact function at src/user/email.js:70-94 with matching signature
  → AS_IS_COMPLIANT with evidence_locations: ["src/user/email.js:70-94"]

✗ WRONG: Found user.getUserField(uid, 'email') which retrieves email
  → This is NOT the required interface, mark as TO_BE_MISSING or TO_BE_PARTIAL

✗ WRONG: Marked AS_IS_COMPLIANT but evidence_locations is empty
  → This is a logical contradiction, always provide locations for AS_IS_COMPLIANT
"""
```

#### 1.1.2 Structured Output Schema强制约束

修改`DeepSearchReport`模型，添加验证逻辑：

```python
# src/models/report.py

from pydantic import field_validator, model_validator

class DeepSearchReport(BaseModel):
    target_requirement_id: str
    requirement_verdict: Literal[
        "AS_IS_COMPLIANT",
        "AS_IS_VIOLATED", 
        "TO_BE_MISSING",
        "TO_BE_PARTIAL"
    ]
    requirement_findings: str
    requirement_evidence_locations: list[str]
    
    # ... other fields ...
    
    @model_validator(mode='after')
    def validate_evidence_locations(self) -> 'DeepSearchReport':
        """强制AS_IS_COMPLIANT必须提供证据位置"""
        if self.requirement_verdict == "AS_IS_COMPLIANT":
            if not self.requirement_evidence_locations:
                raise ValueError(
                    f"AS_IS_COMPLIANT verdict for {self.target_requirement_id} "
                    f"MUST provide evidence_locations. If you cannot find the exact "
                    f"code location, the verdict should be TO_BE_MISSING or TO_BE_PARTIAL."
                )
        return self
    
    @field_validator('requirement_evidence_locations')
    @classmethod
    def validate_location_format(cls, v: list[str]) -> list[str]:
        """验证证据位置格式"""
        for loc in v:
            if ':' not in loc:
                raise ValueError(
                    f"Evidence location '{loc}' must include line numbers "
                    f"(format: 'path/to/file.js:start-end' or 'path/to/file.js:line')"
                )
        return v
```

#### 1.1.3 Reflection Prompt增强

在`REFLECTION_SYSTEM_PROMPT`中添加新接口专项检查：

```python
REFLECTION_SYSTEM_PROMPT = """\
...existing content...

4. NEW INTERFACE VERIFICATION (if requirement origin is "new_interfaces"):
   Extract the exact interface specification from the requirement:
   - Function/method name
   - Input parameter types and names
   - Output type
   - File path
   
   Review your verdict:
   - If you marked AS_IS_COMPLIANT: verify you found the EXACT signature
     * Check: does the function name match exactly?
     * Check: do the parameters match exactly (not just similar)?
     * Check: is it at the exact path specified?
     * Check: did you provide evidence_locations?
     * If ANY check fails → change verdict to TO_BE_MISSING or TO_BE_PARTIAL
   
   - If evidence_locations is empty but verdict is AS_IS_COMPLIANT:
     * This is a logical contradiction
     * Change verdict to TO_BE_MISSING (if nothing similar exists)
     * Or TO_BE_PARTIAL (if similar but incomplete implementation exists)
   
   Common mistakes to avoid:
   - Marking AS_IS_COMPLIANT because "similar functionality exists"
   - Marking AS_IS_COMPLIANT without verifying exact signature match
   - Providing empty evidence_locations for AS_IS_COMPLIANT verdict
"""
```

### 1.2 Prescriptive Boundary Enumeration强化

**问题诊断**：
```
req-010 (issue_001):
Deep-search findings: "Store expires field instead of using db.pexpire"
Closure-checker rejection: "Removing db.pexpire without cleanup mechanism 
                            causes expired objects to accumulate indefinitely"
```

Deep-search提出的修复方案未考虑边界情况：
- 过期对象的清理机制
- TTL与expires字段的共存问题
- 边界值处理（expires恰好等于Date.now()）

**改进方案**：

#### 1.2.1 Boundary Enumeration Checklist

在`DEEP_SEARCH_SYSTEM_PROMPT`中添加边界检查清单：

```python
PRESCRIPTIVE FIX BOUNDARY CHECKLIST:
When your findings contain prescriptive language ("should use X", "correct is Y", 
"must change to Z"), you MUST enumerate boundary cases BEFORE finalizing the verdict.

Required boundary cases to check:
1. Null/undefined/missing values
   - What happens if input is null?
   - What happens if required data is missing?
   
2. Empty collections
   - What happens with empty array/object?
   - What happens with zero-length string?
   
3. Boundary values
   - What happens at exactly max value?
   - What happens at exactly min value?
   - What happens at zero?
   
4. Timing/concurrency
   - What happens if timestamp equals current time?
   - What happens if operation is interrupted?
   
5. Resource cleanup
   - If removing automatic cleanup (TTL, GC), what replaces it?
   - If adding new data, what prevents unbounded growth?

For EACH boundary case:
- State the case explicitly
- Apply your prescriptive fix to that case
- Determine: does it satisfy the requirement? (PASS/FAIL)
- If FAIL: explain why and revise your prescriptive fix

Example:
Requirement: "Store expires field instead of using db.pexpire"
Prescriptive fix: "Remove db.pexpire calls, only use expires timestamp"

Boundary case 1: Expired confirmation object (expires < Date.now())
- Apply fix: Object persists in DB, isValidationPending returns false
- Result: FAIL - expired objects accumulate indefinitely without cleanup
- Revision needed: Add periodic cleanup job or keep db.pexpire as fallback

Boundary case 2: Confirmation object with expires exactly at Date.now()
- Apply fix: isValidationPending returns false (Date.now() >= expires)
- Result: PASS - correctly treated as expired

Only proceed with prescriptive fix if ALL boundary cases PASS.
```

#### 1.2.2 Reflection阶段强制Boundary Check

修改reflection agent的prompt，要求对prescriptive findings进行边界检查：

```python
# src/agents/deep_search_agent.py

REFLECTION_PROMPT_TEMPLATE = """\
...existing content...

5. PRESCRIPTIVE FIX BOUNDARY VERIFICATION:
   Scan your findings for prescriptive language:
   - "should use/store/call/remove X"
   - "must change/add/delete Y"
   - "correct approach is Z"
   - "instead of A, use B"
   
   For EACH prescriptive statement:
   a) List ≥2 boundary cases (null, empty, max, timing, cleanup)
   b) For each boundary: explicitly state PASS or FAIL with reason
   c) If ANY boundary FAILS:
      - Revise the prescriptive fix to handle that boundary
      - OR change verdict (e.g., AS_IS_VIOLATED → TO_BE_PARTIAL if fix is complex)
      - OR add caveat to findings explaining the limitation
   
   Example revision:
   Original: "Remove db.pexpire, only use expires timestamp"
   Boundary fail: Expired objects accumulate without cleanup
   Revised: "Use expires timestamp for validation checks, keep db.pexpire as 
            fallback cleanup mechanism, or add periodic cleanup job"
"""
```

### 1.3 Evidence Location质量提升

**问题诊断**：
- 即使提供了evidence_locations，有时位置不够精确（整个文件而非具体行号）
- 有时提供的位置与findings描述不匹配

**改进方案**：

#### 1.3.1 Evidence Location格式规范

```python
# src/models/report.py

class DeepSearchReport(BaseModel):
    # ... existing fields ...
    
    @field_validator('requirement_evidence_locations')
    @classmethod
    def validate_location_precision(cls, v: list[str], info) -> list[str]:
        """验证证据位置的精确性"""
        verdict = info.data.get('requirement_verdict')
        
        if verdict == "AS_IS_COMPLIANT":
            # AS_IS_COMPLIANT必须提供精确行号
            for loc in v:
                if ':' not in loc:
                    raise ValueError(
                        f"AS_IS_COMPLIANT evidence must include line numbers: {loc}"
                    )
                # 检查是否只是文件路径（没有行号）
                parts = loc.split(':')
                if len(parts) < 2 or not parts[1].strip():
                    raise ValueError(
                        f"AS_IS_COMPLIANT evidence must include specific line numbers: {loc}"
                    )
        
        return v
```

#### 1.3.2 Deep-Search Prompt明确要求

```python
EVIDENCE LOCATION REQUIREMENTS:
- Format: "path/to/file.ext:line" or "path/to/file.ext:start-end"
- Examples:
  ✓ "src/user/email.js:70-94"
  ✓ "src/database/mongo/main.js:87"
  ✗ "src/user/email.js" (missing line numbers)
  ✗ "email validation logic" (not a file path)

- For AS_IS_COMPLIANT: MUST provide exact line numbers where the compliant code exists
- For AS_IS_VIOLATED: MUST provide exact line numbers where the violation occurs
- For TO_BE_MISSING: evidence_locations should be empty (nothing to point to)
- For TO_BE_PARTIAL: provide line numbers of the partial implementation

Quality check:
- Each location should correspond to a specific claim in your findings
- If you mention "function X does Y at line Z", include "file.js:Z" in evidence_locations
- If you cannot find the exact line number, use Read tool to verify before reporting
```

---

## 2. Orchestrator改进

### 2.1 I2 Invariant循环检测与处理

**问题诊断**：
```
issue_001 action_history:
iter 15: req-015 AS_IS_COMPLIANT → I2_reset
iter 17: req-015 AS_IS_COMPLIANT → I2_reset
iter 19: req-015 AS_IS_COMPLIANT → I2_reset
iter 21: req-015 AS_IS_COMPLIANT → I2_reset
iter 23: req-015 AS_IS_COMPLIANT → I2_reset
iter 25: req-015 AS_IS_COMPLIANT → I2_reset
iter 27: req-015 AS_IS_COMPLIANT → I2_reset
iter 29: req-015 AS_IS_COMPLIANT → I2_reset
```

Orchestrator检测到I2 invariant违反（新接口不应该是AS_IS_COMPLIANT），但：
- 只是简单重置verdict为UNCHECKED
- 没有记录重置次数
- 没有在多次重置后采取不同策略
- 浪费了一半的deep-search预算

**改进方案**：

#### 2.1.1 I2 Invariant重置计数器

```python
# src/orchestrator/engine.py

class EvidenceCollectionEngine:
    def __init__(self, ...):
        # ... existing fields ...
        self.i2_reset_counter: dict[str, int] = {}  # req_id -> reset_count
        self.max_i2_resets = 2  # 最多重置2次
    
    async def _check_i2_invariant(
        self, 
        requirement: dict,
        evidence_cards: dict
    ) -> tuple[bool, str]:
        """
        检查I2 invariant: 新接口不应该判断为AS_IS_COMPLIANT
        
        Returns:
            (is_violated, reason)
        """
        req_id = requirement['id']
        origin = requirement.get('origin', '')
        verdict = requirement.get('verdict', '')
        evidence_locs = requirement.get('evidence_locations', [])
        
        # I2: 新接口不应该是AS_IS_COMPLIANT（除非提供了证据位置）
        if origin == 'new_interfaces' and verdict == 'AS_IS_COMPLIANT':
            if not evidence_locs:
                # 违反I2：新接口标记为已实现但无证据
                return True, "New interface marked AS_IS_COMPLIANT without evidence_locations"
            else:
                # 有证据位置，需要验证是否真的存在
                # TODO: 可以添加自动验证逻辑（读取文件检查）
                return False, ""
        
        return False, ""
    
    async def _handle_i2_violation(
        self,
        requirement: dict,
        evidence_cards: dict,
        reason: str
    ) -> str:
        """
        处理I2 invariant违反
        
        Returns:
            action: "reset" | "force_missing" | "escalate"
        """
        req_id = requirement['id']
        reset_count = self.i2_reset_counter.get(req_id, 0)
        
        if reset_count == 0:
            # 第一次违反：重置为UNCHECKED，给deep-search一次机会
            self.i2_reset_counter[req_id] = 1
            logger.warning(
                f"I2 invariant violated for {req_id} (attempt 1/{self.max_i2_resets}): {reason}. "
                f"Resetting to UNCHECKED."
            )
            return "reset"
        
        elif reset_count == 1:
            # 第二次违反：强制标记为TO_BE_MISSING，添加rework指令
            self.i2_reset_counter[req_id] = 2
            logger.warning(
                f"I2 invariant violated for {req_id} AGAIN (attempt 2/{self.max_i2_resets}): {reason}. "
                f"Forcing verdict to TO_BE_MISSING with rework instruction."
            )
            return "force_missing"
        
        else:
            # 第三次及以上：上报给closure-checker处理
            logger.error(
                f"I2 invariant violated for {req_id} repeatedly (attempt {reset_count + 1}): {reason}. "
                f"Escalating to closure-checker."
            )
            return "escalate"
```

#### 2.1.2 Force Missing策略

当I2 invariant第二次违反时，orchestrator强制设置verdict并添加rework指令：

```python
async def _force_missing_with_rework(
    self,
    requirement: dict,
    evidence_cards: dict
) -> None:
    """强制设置verdict为TO_BE_MISSING，并添加rework指令"""
    req_id = requirement['id']
    
    # 更新verdict
    requirement['verdict'] = 'TO_BE_MISSING'
    requirement['evidence_locations'] = []
    
    # 添加rework_context，指导deep-search正确判断
    rework_instruction = (
        f"ORCHESTRATOR OVERRIDE: This is a NEW INTERFACE (origin=new_interfaces). "
        f"You have marked it AS_IS_COMPLIANT twice without valid evidence_locations. "
        f"\n\n"
        f"NEW INTERFACE RULES:\n"
        f"1. Extract the EXACT signature from requirement text\n"
        f"2. Search for EXACT match (not similar functionality)\n"
        f"3. If exact match found: provide evidence_locations with line numbers\n"
        f"4. If only similar functionality found: verdict is TO_BE_MISSING or TO_BE_PARTIAL\n"
        f"5. If nothing found: verdict is TO_BE_MISSING\n"
        f"\n"
        f"Your previous AS_IS_COMPLIANT verdicts were rejected because:\n"
        f"- You did not provide evidence_locations (logical contradiction)\n"
        f"- OR you found similar but not exact functionality\n"
        f"\n"
        f"Re-investigate with strict exact-match criteria. If you cannot find the EXACT "
        f"interface at the EXACT path with EXACT signature, the verdict MUST be TO_BE_MISSING."
    )
    
    requirement['rework_context'] = rework_instruction
    
    # 记录action
    self._record_action(
        phase="orchestrator",
        subagent="i2_enforcer",
        outcome=f"forced_TO_BE_MISSING:reset_count={self.i2_reset_counter[req_id]}",
        requirement_id=req_id
    )
```

#### 2.1.3 Escalate策略

如果强制TO_BE_MISSING后仍然违反，直接提交给closure-checker：

```python
async def _escalate_to_closure(
    self,
    requirement: dict,
    evidence_cards: dict
) -> None:
    """将持续违反I2的requirement上报给closure-checker"""
    req_id = requirement['id']
    
    # 标记为UNCHECKED，让closure-checker决定
    requirement['verdict'] = 'UNCHECKED'
    
    # 添加escalation note
    escalation_note = (
        f"ESCALATED BY ORCHESTRATOR: This requirement has violated I2 invariant "
        f"{self.i2_reset_counter[req_id]} times. Deep-search agent repeatedly marks "
        f"this new interface as AS_IS_COMPLIANT without valid evidence. "
        f"Closure-checker should review and make final determination."
    )
    
    requirement['rework_context'] = escalation_note
    
    logger.error(f"Escalating {req_id} to closure-checker after {self.i2_reset_counter[req_id]} I2 violations")

### 2.2 Budget分配优化

**问题诊断**：
- issue_001用完30次deep-search预算，但有15次浪费在I2循环上
- 没有根据requirement复杂度动态分配预算
- 没有在预算即将耗尽时采取保守策略

**改进方案**：

#### 2.2.1 动态预算分配

```python
# src/orchestrator/engine.py

class EvidenceCollectionEngine:
    def __init__(self, ...):
        # ... existing fields ...
        self.total_budget = 30  # 总预算
        self.reserved_budget = 5  # 保留预算（用于最后的critical requirements）
        self.budget_per_req: dict[str, int] = {}  # 每个req的预算上限
    
    def _allocate_budget(self, requirements: list[dict]) -> None:
        """根据requirement复杂度分配预算"""
        total_reqs = len(requirements)
        available_budget = self.total_budget - self.reserved_budget
        
        # 计算每个requirement的复杂度分数
        complexity_scores = {}
        for req in requirements:
            score = self._calculate_complexity(req)
            complexity_scores[req['id']] = score
        
        total_complexity = sum(complexity_scores.values())
        
        # 按复杂度比例分配预算
        for req in requirements:
            req_id = req['id']
            complexity_ratio = complexity_scores[req_id] / total_complexity
            allocated = max(1, int(available_budget * complexity_ratio))
            self.budget_per_req[req_id] = allocated
            
            logger.info(
                f"Budget allocation: {req_id} = {allocated} turns "
                f"(complexity={complexity_scores[req_id]:.2f})"
            )
    
    def _calculate_complexity(self, requirement: dict) -> float:
        """计算requirement的复杂度分数"""
        score = 1.0  # 基础分数
        
        # 因素1: origin类型
        origin = requirement.get('origin', '')
        if origin == 'new_interfaces':
            score += 0.5  # 新接口需要验证不存在
        elif origin == 'prescriptive':
            score += 1.0  # prescriptive需要边界检查
        
        # 因素2: 描述长度（越长越复杂）
        text = requirement.get('text', '')
        if len(text) > 500:
            score += 0.5
        elif len(text) > 1000:
            score += 1.0
        
        # 因素3: 是否有多个条件
        if ' and ' in text.lower() or ' or ' in text.lower():
            score += 0.3
        
        # 因素4: 是否涉及多个文件
        if 'across' in text.lower() or 'multiple' in text.lower():
            score += 0.5
        
        return score
    
    def _check_budget_exhausted(self, req_id: str) -> bool:
        """检查某个requirement的预算是否耗尽"""
        used = sum(
            1 for action in self.action_history
            if action.get('requirement_id') == req_id
            and action.get('subagent') == 'deep_search'
        )
        allocated = self.budget_per_req.get(req_id, 3)  # 默认3次
        
        if used >= allocated:
            logger.warning(
                f"Budget exhausted for {req_id}: {used}/{allocated} turns used"
            )
            return True
        
        return False
```

#### 2.2.2 预算耗尽时的保守策略

```python
async def _handle_budget_exhaustion(
    self,
    requirement: dict,
    evidence_cards: dict
) -> None:
    """预算耗尽时采取保守策略"""
    req_id = requirement['id']
    current_verdict = requirement.get('verdict', 'UNCHECKED')
    
    if current_verdict == 'UNCHECKED':
        # 如果还没有verdict，根据origin设置默认值
        origin = requirement.get('origin', '')
        
        if origin == 'new_interfaces':
            # 新接口默认为TO_BE_MISSING
            requirement['verdict'] = 'TO_BE_MISSING'
            requirement['findings'] = (
                "BUDGET_EXHAUSTED: Unable to verify interface existence within budget. "
                "Defaulting to TO_BE_MISSING (conservative assumption for new interfaces)."
            )
            logger.warning(f"Budget exhausted for {req_id}, defaulting to TO_BE_MISSING")
        
        elif origin == 'prescriptive':
            # Prescriptive默认为AS_IS_VIOLATED
            requirement['verdict'] = 'AS_IS_VIOLATED'
            requirement['findings'] = (
                "BUDGET_EXHAUSTED: Unable to complete prescriptive verification within budget. "
                "Defaulting to AS_IS_VIOLATED (conservative assumption for prescriptive requirements)."
            )
            logger.warning(f"Budget exhausted for {req_id}, defaulting to AS_IS_VIOLATED")
        
        else:
            # 其他类型默认为UNCHECKED，让closure-checker决定
            requirement['findings'] = (
                "BUDGET_EXHAUSTED: Unable to complete investigation within budget. "
                "Leaving as UNCHECKED for closure-checker review."
            )
            logger.warning(f"Budget exhausted for {req_id}, leaving as UNCHECKED")
    
    else:
        # 已有verdict，保持不变但添加budget exhaustion note
        current_findings = requirement.get('findings', '')
        requirement['findings'] = (
            f"{current_findings}\n\n"
            f"NOTE: Budget exhausted before full verification. "
            f"Verdict based on partial investigation."
        )

### 2.3 Rework轮次优化

**问题诊断**：
- issue_002和issue_003在rework阶段失败
- Rework指令不够具体，deep-search不知道如何改进
- 没有在rework之间传递学习信息

**改进方案**：

#### 2.3.1 结构化Rework指令

```python
# src/orchestrator/engine.py

async def _generate_rework_instruction(
    self,
    requirement: dict,
    closure_feedback: dict
) -> str:
    """生成结构化的rework指令"""
    req_id = requirement['id']
    
    # 提取closure-checker的具体反馈
    failed_checks = closure_feedback.get('failed_checks', [])
    
    instruction_parts = [
        f"REWORK REQUIRED FOR {req_id}",
        f"",
        f"Closure-checker rejected your previous findings. Specific issues:",
        f""
    ]
    
    for i, check in enumerate(failed_checks, 1):
        check_type = check.get('check_type', 'unknown')
        reason = check.get('reason', 'No reason provided')
        
        instruction_parts.append(f"{i}. {check_type.upper()} FAILURE:")
        instruction_parts.append(f"   Reason: {reason}")
        
        # 根据check类型提供具体指导
        if check_type == 'verdict_vs_code':
            instruction_parts.append(
                f"   Action: Re-read the code at the locations you provided. "
                f"Verify your verdict matches what the code actually does."
            )
        
        elif check_type == 'findings_anti_hallucination':
            instruction_parts.append(
                f"   Action: Remove any claims not directly supported by code. "
                f"For each claim, provide evidence_locations with line numbers."
            )
        
        elif check_type == 'prescriptive_boundary_self_check':
            instruction_parts.append(
                f"   Action: Enumerate boundary cases for your prescriptive fix. "
                f"For each boundary: state PASS or FAIL with reason. "
                f"Revise fix if any boundary fails."
            )
        
        instruction_parts.append("")
    
    # 添加通用指导
    instruction_parts.extend([
        "GENERAL GUIDANCE:",
        "- Use Read tool to verify code before making claims",
        "- Provide evidence_locations for all factual claims",
        "- For prescriptive fixes: check ≥2 boundary cases",
        "- For AS_IS_COMPLIANT: verify EXACT signature match",
        ""
    ])
    
    return "\n".join(instruction_parts)
```

#### 2.3.2 Rework学习机制

在多轮rework之间传递学习信息：

```python
class EvidenceCollectionEngine:
    def __init__(self, ...):
        # ... existing fields ...
        self.rework_history: dict[str, list[dict]] = {}  # req_id -> [rework_attempts]
    
    async def _prepare_rework_context(
        self,
        requirement: dict,
        closure_feedback: dict
    ) -> dict:
        """准备rework上下文，包含历史学习信息"""
        req_id = requirement['id']
        
        # 记录本次rework
        rework_attempt = {
            'round': len(self.rework_history.get(req_id, [])) + 1,
            'previous_verdict': requirement.get('verdict'),
            'previous_findings': requirement.get('findings'),
            'closure_feedback': closure_feedback,
            'timestamp': datetime.now().isoformat()
        }
        
        if req_id not in self.rework_history:
            self.rework_history[req_id] = []
        self.rework_history[req_id].append(rework_attempt)
        
        # 生成学习摘要
        learning_summary = self._generate_learning_summary(req_id)
        
        # 生成rework指令
        rework_instruction = await self._generate_rework_instruction(
            requirement, closure_feedback
        )
        
        return {
            'rework_round': rework_attempt['round'],
            'rework_instruction': rework_instruction,
            'learning_summary': learning_summary,
            'previous_attempts': self.rework_history[req_id]
        }
    
    def _generate_learning_summary(self, req_id: str) -> str:
        """生成历史rework的学习摘要"""
        history = self.rework_history.get(req_id, [])
        
        if not history:
            return "This is your first attempt."
        
        summary_parts = [
            f"LEARNING FROM PREVIOUS ATTEMPTS ({len(history)} rounds):",
            ""
        ]
        
        for attempt in history:
            round_num = attempt['round']
            verdict = attempt['previous_verdict']
            feedback = attempt['closure_feedback']
            
            summary_parts.append(
                f"Round {round_num}: Verdict={verdict}, "
                f"Rejected because: {feedback.get('summary', 'Unknown')}"
            )
        
        # 识别重复错误
        repeated_issues = self._identify_repeated_issues(history)
        if repeated_issues:
            summary_parts.append("")
            summary_parts.append("REPEATED ISSUES (avoid these):")
            for issue in repeated_issues:
                summary_parts.append(f"- {issue}")
        
        return "\n".join(summary_parts)
    
    def _identify_repeated_issues(self, history: list[dict]) -> list[str]:
        """识别重复出现的问题"""
        issue_counts = {}
        
        for attempt in history:
            feedback = attempt.get('closure_feedback', {})
            failed_checks = feedback.get('failed_checks', [])
            
            for check in failed_checks:
                check_type = check.get('check_type')
                if check_type:
                    issue_counts[check_type] = issue_counts.get(check_type, 0) + 1
        
        # 返回出现≥2次的问题
        repeated = [
            f"{check_type} (failed {count} times)"
            for check_type, count in issue_counts.items()
            if count >= 2
        ]
        
        return repeated
```

---

## 3. Closure-Checker改进

### 3.1 反馈质量提升

**问题诊断**：
- Closure-checker的反馈有时过于简短（"verdict_vs_code check failed"）
- 缺少具体的代码位置指引
- 没有提供修复建议

**改进方案**：

#### 3.1.1 详细反馈模板

```python
# src/agents/closure_checker_agent.py

def _format_check_failure(
    self,
    check_type: str,
    requirement: dict,
    evidence: dict,
    reason: str
) -> dict:
    """格式化检查失败信息，提供详细反馈"""
    
    base_feedback = {
        'check_type': check_type,
        'requirement_id': requirement['id'],
        'reason': reason
    }
    
    # 根据check类型添加具体指导
    if check_type == 'verdict_vs_code':
        base_feedback['guidance'] = self._verdict_vs_code_guidance(
            requirement, evidence, reason
        )
    
    elif check_type == 'findings_anti_hallucination':
        base_feedback['guidance'] = self._anti_hallucination_guidance(
            requirement, evidence, reason
        )
    
    elif check_type == 'prescriptive_boundary_self_check':
        base_feedback['guidance'] = self._boundary_check_guidance(
            requirement, evidence, reason
        )
    
    return base_feedback

def _verdict_vs_code_guidance(
    self,
    requirement: dict,
    evidence: dict,
    reason: str
) -> str:
    """生成verdict_vs_code检查失败的具体指导"""
    req_id = requirement['id']
    verdict = requirement.get('verdict')
    evidence_locs = requirement.get('evidence_locations', [])
    
    guidance_parts = [
        f"Your verdict ({verdict}) does not match the code.",
        f"",
        f"Specific issue: {reason}",
        f""
    ]
    
    if verdict == 'AS_IS_COMPLIANT' and not evidence_locs:
        guidance_parts.extend([
            "You marked this as AS_IS_COMPLIANT but provided no evidence_locations.",
            "This is a logical contradiction.",
            "",
            "Action required:",
            "1. Use Read tool to find the exact code location",
            "2. Verify it matches the requirement specification exactly",
            "3. Provide evidence_locations with line numbers",
            "4. If you cannot find exact match, change verdict to TO_BE_MISSING or TO_BE_PARTIAL"
        ])
    
    elif verdict == 'AS_IS_COMPLIANT' and evidence_locs:
        guidance_parts.extend([
            f"You provided evidence_locations: {evidence_locs}",
            "But the code at those locations does not satisfy the requirement.",
            "",
            "Action required:",
            "1. Re-read the code at those locations",
            "2. Compare against the requirement specification",
            "3. If code is incomplete: change verdict to TO_BE_PARTIAL",
            "4. If code is wrong: change verdict to AS_IS_VIOLATED",
            "5. If code doesn't exist: change verdict to TO_BE_MISSING"
        ])
    
    elif verdict == 'AS_IS_VIOLATED':
        guidance_parts.extend([
            "You marked this as AS_IS_VIOLATED but the code does not violate the requirement.",
            "",
            "Action required:",
            "1. Re-read the requirement carefully",
            "2. Check if the code actually violates it or just implements it differently",
            "3. If code is correct: change verdict to AS_IS_COMPLIANT",
            "4. If code is incomplete: change verdict to TO_BE_PARTIAL"
        ])
    
    return "\n".join(guidance_parts)

def _anti_hallucination_guidance(
    self,
    requirement: dict,
    evidence: dict,
    reason: str
) -> str:
    """生成anti-hallucination检查失败的具体指导"""
    guidance_parts = [
        "Your findings contain claims not supported by the code.",
        f"",
        f"Specific issue: {reason}",
        f"",
        "Common hallucination patterns:",
        "- Claiming function X exists without verifying via Read",
        "- Quoting code snippets from memory instead of actual files",
        "- Assuming implementation details without checking",
        "",
        "Action required:",
        "1. Review your findings for backtick-enclosed code snippets",
        "2. For EACH snippet: use Read to verify it exists in the cited file",
        "3. Remove or rephrase any claims not directly supported by Read output",
        "4. Add evidence_locations for all factual claims"
    ]
    
    return "\n".join(guidance_parts)

def _boundary_check_guidance(
    self,
    requirement: dict,
    evidence: dict,
    reason: str
) -> str:
    """生成boundary check失败的具体指导"""
    guidance_parts = [
        "Your prescriptive fix fails under boundary conditions.",
        f"",
        f"Specific issue: {reason}",
        f"",
        "Action required:",
        "1. Identify the prescriptive statement in your findings",
        "2. Enumerate ≥2 boundary cases:",
        "   - Null/undefined/missing values",
        "   - Empty collections",
        "   - Boundary values (max, min, zero)",
        "   - Timing edge cases (exactly at threshold)",
        "   - Resource cleanup (if removing automatic cleanup)",
        "3. For EACH boundary: apply your fix and determine PASS or FAIL",
        "4. If ANY boundary fails: revise your prescriptive fix",
        "5. Document the boundary analysis in your findings"
    ]
    
    return "\n".join(guidance_parts)
```

### 3.2 自动验证增强

**问题诊断**：
- Closure-checker依赖LLM判断，有时会误判
- 对于AS_IS_COMPLIANT，可以自动验证evidence_locations是否真实存在

**改进方案**：

#### 3.2.1 Evidence Location自动验证

```python
# src/agents/closure_checker_agent.py

async def _verify_evidence_locations(
    self,
    requirement: dict,
    repo_dir: Path
) -> tuple[bool, list[str]]:
    """
    自动验证evidence_locations是否真实存在
    
    Returns:
        (all_valid, invalid_locations)
    """
    evidence_locs = requirement.get('evidence_locations', [])
    invalid_locs = []
    
    for loc in evidence_locs:
        # 解析location格式: "path/to/file.js:start-end" or "path/to/file.js:line"
        if ':' not in loc:
            invalid_locs.append(f"{loc} (missing line numbers)")
            continue
        
        file_path, line_part = loc.rsplit(':', 1)
        full_path = repo_dir / file_path
        
        # 检查文件是否存在
        if not full_path.exists():
            invalid_locs.append(f"{loc} (file does not exist)")
            continue
        
        # 解析行号
        try:
            if '-' in line_part:
                start, end = map(int, line_part.split('-'))
            else:
                start = end = int(line_part)
        except ValueError:
            invalid_locs.append(f"{loc} (invalid line number format)")
            continue
        
        # 检查行号是否在文件范围内
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                total_lines = len(lines)
                
                if start < 1 or end > total_lines:
                    invalid_locs.append(
                        f"{loc} (line numbers out of range: file has {total_lines} lines)"
                    )
        except Exception as e:
            invalid_locs.append(f"{loc} (error reading file: {e})")
    
    return len(invalid_locs) == 0, invalid_locs

async def _run_closure_checker_with_validation(
    self,
    evidence: EvidenceCards,
    manifest: AuditManifest,
    repo_dir: Path | None = None
) -> ClosureVerdict:
    """运行closure-checker，包含自动验证"""
    
    # 预验证：检查evidence_locations
    pre_validation_failures = []
    
    for req in evidence.requirements:
        if req.verdict == "AS_IS_COMPLIANT":
            valid, invalid_locs = await self._verify_evidence_locations(req, repo_dir)
            
            if not valid:
                pre_validation_failures.append({
                    'requirement_id': req.id,
                    'check_type': 'evidence_location_validation',
                    'reason': f"Invalid evidence_locations: {', '.join(invalid_locs)}",
                    'guidance': (
                        "Your evidence_locations contain errors. "
                        "Use Read tool to verify file paths and line numbers before reporting."
                    )
                })
    
    # 如果预验证失败，直接返回EVIDENCE_MISSING
    if pre_validation_failures:
        return ClosureVerdict(
            verdict="EVIDENCE_MISSING",
            audited=[],
            missing=[f['requirement_id'] for f in pre_validation_failures],
            suggested_tasks=[f['requirement_id'] for f in pre_validation_failures],
            pre_validation_failures=pre_validation_failures
        )
    
    # 继续正常的closure-checker流程
    return await _run_closure_checker_async(evidence, manifest, repo_dir)
```

---

## 4. 测试与验证

### 4.1 单元测试

为新增功能添加单元测试：

```python
# tests/test_deep_search_improvements.py

import pytest
from src.agents.deep_search_agent import run_deep_search
from src.models.context import EvidenceCards
from src.models.report import DeepSearchReport

class TestNewInterfaceVerification:
    """测试新接口判断逻辑"""
    
    def test_new_interface_as_is_compliant_requires_evidence(self):
        """AS_IS_COMPLIANT必须提供evidence_locations"""
        
        # 构造一个新接口requirement
        todo_task = """
        Verify requirement req-001:
        Type: Function, Name: user.email.getEmailForValidation
        Path: src/user/email.js
        Input: uid: number
        Output: Promise<string | null>
        Origin: new_interfaces
        """
        
        evidence = EvidenceCards(requirements=[...])
        
        # 运行deep-search
        report = run_deep_search(todo_task, evidence, repo_dir=test_repo)
        
        # 如果verdict是AS_IS_COMPLIANT，必须有evidence_locations
        if report.requirement_verdict == "AS_IS_COMPLIANT":
            assert len(report.requirement_evidence_locations) > 0, \
                "AS_IS_COMPLIANT verdict must provide evidence_locations"
            
            # 验证evidence_locations格式
            for loc in report.requirement_evidence_locations:
                assert ':' in loc, f"Evidence location must include line numbers: {loc}"
    
    def test_new_interface_similar_functionality_not_compliant(self):
        """相似功能不应判断为AS_IS_COMPLIANT"""
        
        # 构造场景：要求getEmailForValidation，但只存在getUserField
        todo_task = """
        Verify requirement req-001:
        Type: Function, Name: user.email.getEmailForValidation
        Path: src/user/email.js
        Origin: new_interfaces
        
        Note: The codebase has user.getUserField(uid, 'email') but NOT getEmailForValidation
        """
        
        evidence = EvidenceCards(requirements=[...])
        report = run_deep_search(todo_task, evidence, repo_dir=test_repo)
        
        # 应该判断为TO_BE_MISSING或TO_BE_PARTIAL，而非AS_IS_COMPLIANT
        assert report.requirement_verdict in ["TO_BE_MISSING", "TO_BE_PARTIAL"], \
            "Similar functionality should not be marked AS_IS_COMPLIANT"

class TestPrescriptiveBoundaryCheck:
    """测试prescriptive fix的边界检查"""
    
    def test_prescriptive_fix_includes_boundary_analysis(self):
        """Prescriptive findings必须包含边界分析"""
        
        todo_task = """
        Verify requirement req-010:
        Store expires field instead of using db.pexpire
        Origin: prescriptive
        """
        
        evidence = EvidenceCards(requirements=[...])
        report = run_deep_search(todo_task, evidence, repo_dir=test_repo)
        
        # 如果findings包含prescriptive语言，应该有边界分析
        findings = report.requirement_findings.lower()
        
        if any(keyword in findings for keyword in ['should', 'must', 'instead of', 'correct is']):
            # 检查是否包含边界分析关键词
            boundary_keywords = ['boundary', 'edge case', 'null', 'empty', 'cleanup']
            has_boundary_analysis = any(kw in findings for kw in boundary_keywords)
            
            assert has_boundary_analysis, \
                "Prescriptive findings must include boundary case analysis"

class TestI2InvariantHandling:
    """测试I2 invariant处理"""
    
    @pytest.mark.asyncio
    async def test_i2_reset_counter(self):
        """测试I2违反计数器"""
        
        engine = EvidenceCollectionEngine(...)
        
        requirement = {
            'id': 'req-001',
            'origin': 'new_interfaces',
            'verdict': 'AS_IS_COMPLIANT',
            'evidence_locations': []  # 违反I2
        }
        
        # 第一次违反：应该reset
        action = await engine._handle_i2_violation(requirement, {}, "test reason")
        assert action == "reset"
        assert engine.i2_reset_counter['req-001'] == 1
        
        # 第二次违反：应该force_missing
        action = await engine._handle_i2_violation(requirement, {}, "test reason")
        assert action == "force_missing"
        assert engine.i2_reset_counter['req-001'] == 2
        
        # 第三次违反：应该escalate
        action = await engine._handle_i2_violation(requirement, {}, "test reason")
        assert action == "escalate"
```

### 4.2 集成测试

使用三个失败的issue作为回归测试：

```python
# tests/test_phase21_regression.py

import pytest
from src.orchestrator.engine import run_full_pipeline

class TestIssue001Regression:
    """issue_001 (NodeBB email validation) 回归测试"""
    
    @pytest.mark.asyncio
    async def test_issue_001_no_i2_loop(self):
        """验证不再陷入I2循环"""
        
        result = await run_full_pipeline(
            issue_dir="workdir/swe_issue_001",
            max_iterations=30
        )
        
        # 检查action_history
        i2_resets = [
            action for action in result['action_history']
            if 'I2_reset' in action.get('outcome', '')
        ]
        
        # I2重置次数应该≤每个requirement 2次
        req_ids = set(action['requirement_id'] for action in i2_resets)
        for req_id in req_ids:
            req_resets = [a for a in i2_resets if a['requirement_id'] == req_id]
            assert len(req_resets) <= 2, \
                f"{req_id} had {len(req_resets)} I2 resets (max 2 allowed)"
    
    @pytest.mark.asyncio
    async def test_issue_001_new_interface_verdict(self):
        """验证新接口判断正确"""
        
        result = await run_full_pipeline(
            issue_dir="workdir/swe_issue_001",
            max_iterations=30
        )
        
        evidence = result['evidence_cards']
        
        # req-015是新接口
        req_015 = next(r for r in evidence['requirements'] if r['id'] == 'req-015')
        
        if req_015['verdict'] == 'AS_IS_COMPLIANT':
            # 必须有evidence_locations
            assert len(req_015['evidence_locations']) > 0, \
                "New interface marked AS_IS_COMPLIANT must have evidence_locations"
            
            # 验证evidence_locations真实存在
            for loc in req_015['evidence_locations']:
                assert ':' in loc, "Evidence location must include line numbers"

class TestIssue002Regression:
    """issue_002 (face_recognition) 回归测试"""
    
    @pytest.mark.asyncio
    async def test_issue_002_generates_patch(self):
        """验证能够生成patch"""
        
        result = await run_full_pipeline(
            issue_dir="workdir/swe_issue_002",
            max_iterations=30
        )
        
        # 应该通过closure-checker
        assert result['closure_checker_approved'] == True, \
            "issue_002 should pass closure-checker with improvements"
        
        # 应该生成patch
        patch_path = Path("workdir/swe_issue_002/outputs/patch.diff")
        assert patch_path.exists(), "Patch file should be generated"
        assert patch_path.stat().st_size > 0, "Patch file should not be empty"

class TestIssue003Regression:
    """issue_003 (WebFinger) 回归测试"""
    
    @pytest.mark.asyncio
    async def test_issue_003_rework_effectiveness(self):
        """验证rework机制有效"""
        
        result = await run_full_pipeline(
            issue_dir="workdir/swe_issue_003",
            max_iterations=30
        )
        
        # 检查rework轮次
        rework_actions = [
            action for action in result['action_history']
            if 'rework' in action.get('outcome', '').lower()
        ]
        
        # 如果有rework，应该在3轮内解决
        if rework_actions:
            assert len(rework_actions) <= 3, \
                f"Rework should resolve within 3 rounds, got {len(rework_actions)}"
        
        # 最终应该通过
        assert result['closure_checker_approved'] == True, \
            "issue_003 should pass closure-checker after rework"
```

### 4.3 性能基准测试

```python
# tests/test_phase21_performance.py

import pytest
import time
from src.orchestrator.engine import run_full_pipeline

class TestPerformanceBenchmarks:
    """性能基准测试"""
    
    @pytest.mark.asyncio
    async def test_budget_efficiency(self):
        """测试预算使用效率"""
        
        start_time = time.time()
        
        result = await run_full_pipeline(
            issue_dir="workdir/swe_issue_001",
            max_iterations=30
        )
        
        elapsed = time.time() - start_time
        
        # 统计deep-search调用次数
        deep_search_calls = [
            action for action in result['action_history']
            if action.get('subagent') == 'deep_search'
        ]
        
        total_calls = len(deep_search_calls)
        total_requirements = len(result['evidence_cards']['requirements'])
        
        # 平均每个requirement的调用次数应该≤3
        avg_calls_per_req = total_calls / total_requirements
        assert avg_calls_per_req <= 3.0, \
            f"Average {avg_calls_per_req:.1f} calls per requirement (target ≤3.0)"
        
        # 总耗时应该在合理范围内（假设每次调用平均30秒）
        expected_max_time = total_calls * 30 * 1.5  # 1.5倍容差
        assert elapsed <= expected_max_time, \
            f"Elapsed {elapsed:.0f}s exceeds expected {expected_max_time:.0f}s"
    
    @pytest.mark.asyncio
    async def test_i2_loop_prevention(self):
        """测试I2循环预防效果"""
        
        result = await run_full_pipeline(
            issue_dir="workdir/swe_issue_001",
            max_iterations=30
        )
        
        # 统计浪费在I2循环上的迭代次数
        i2_wasted = sum(
            1 for action in result['action_history']
            if 'I2_reset' in action.get('outcome', '')
        )
        
        total_iterations = len([
            a for a in result['action_history']
            if a.get('subagent') == 'deep_search'
        ])
        
        # I2浪费应该<20%总迭代次数
        waste_ratio = i2_wasted / total_iterations if total_iterations > 0 else 0
        assert waste_ratio < 0.2, \
            f"I2 loop waste {waste_ratio:.1%} exceeds 20% threshold"
```

---

## 5. 实施计划

### 5.1 实施顺序

**Phase 21.1: Deep-Search Agent改进** (优先级: 最高)
- [ ] 1.1.1 System Prompt增强（新接口判断规则）
- [ ] 1.1.2 Structured Output Schema强制约束
- [ ] 1.1.3 Reflection Prompt增强
- [ ] 1.2.1 Boundary Enumeration Checklist
- [ ] 1.2.2 Reflection阶段强制Boundary Check
- [ ] 1.3.1 Evidence Location格式规范
- [ ] 1.3.2 Deep-Search Prompt明确要求

**Phase 21.2: Orchestrator改进** (优先级: 高)
- [ ] 2.1.1 I2 Invariant重置计数器
- [ ] 2.1.2 Force Missing策略
- [ ] 2.1.3 Escalate策略
- [ ] 2.2.1 动态预算分配
- [ ] 2.2.2 预算耗尽时的保守策略
- [ ] 2.3.1 结构化Rework指令
- [ ] 2.3.2 Rework学习机制

**Phase 21.3: Closure-Checker改进** (优先级: 中)
- [ ] 3.1.1 详细反馈模板
- [ ] 3.2.1 Evidence Location自动验证

**Phase 21.4: 测试与验证** (优先级: 高)
- [ ] 4.1 单元测试
- [ ] 4.2 集成测试（三个issue回归测试）
- [ ] 4.3 性能基准测试

### 5.2 验收标准

**必须达成**：
1. ✅ 三个失败的issue (001, 002, 003) 至少2个通过closure-checker
2. ✅ I2循环浪费的迭代次数 < 20%总迭代次数
3. ✅ 新接口判断准确率 > 90%（AS_IS_COMPLIANT必须有evidence_locations）
4. ✅ Prescriptive fix包含边界分析的比例 > 80%

**期望达成**：
1. 🎯 三个issue全部通过closure-checker并生成有效patch
2. 🎯 平均每个requirement的deep-search调用次数 ≤ 2.5次
3. 🎯 Rework成功率 > 60%（第一轮rework后通过closure-checker）

### 5.3 风险与缓解

**风险1: 过度严格导致误拒**
- 描述: 新的验证逻辑可能过于严格，拒绝合理的判断
- 缓解: 
  - 在测试阶段收集误拒案例
  - 调整验证阈值
  - 提供override机制

**风险2: Prompt过长影响性能**
- 描述: 增强的prompt可能导致token消耗增加
- 缓解:
  - 监控token使用量
  - 优化prompt措辞，去除冗余
  - 考虑将部分规则移到structured output validation

**风险3: I2强制策略过于激进**
- 描述: Force missing可能在deep-search确实找到接口时误判
- 缓解:
  - 第一次只reset，第二次才force
  - 在force之前添加详细的rework指令
  - 保留escalate机制作为最后手段

---

## 6. 后续优化方向

### 6.1 自动化Evidence验证

当前closure-checker依赖LLM判断，可以增加更多自动化验证：

```python
# 未来可以实现的自动验证
- 函数签名匹配验证（AST解析）
- 代码覆盖率验证（evidence_locations是否覆盖关键逻辑）
- 跨文件引用验证（import/require关系）
- 测试用例验证（是否有对应的测试）
```

### 6.2 Deep-Search学习机制

从成功和失败案例中学习：

```python
# 构建案例库
- 成功案例: 正确判断新接口的案例
- 失败案例: I2循环、边界检查失败的案例
- 在deep-search prompt中引用相似案例作为few-shot示例
```

### 6.3 动态Prompt调整

根据requirement类型动态调整prompt：

```python
# 针对不同类型的requirement使用不同的prompt模板
- new_interfaces: 强调exact match验证
- prescriptive: 强调boundary enumeration
- localization: 强调call chain追踪
```

---

## 附录: 关键代码位置

**需要修改的文件**：
- `src/agents/deep_search_agent.py` - Deep-search agent主逻辑
- `src/agents/closure_checker_agent.py` - Closure-checker主逻辑
- `src/orchestrator/engine.py` - Orchestrator主逻辑
- `src/models/report.py` - DeepSearchReport模型
- `src/models/verdict.py` - ClosureVerdict模型

**需要新增的文件**：
- `tests/test_deep_search_improvements.py` - Deep-search改进测试
- `tests/test_phase21_regression.py` - 回归测试
- `tests/test_phase21_performance.py` - 性能测试

**配置文件**：
- `.env` - 可能需要调整模型参数（max_tokens, temperature）
- `src/config.py` - 添加Phase 21相关配置项

---

## 总结

Phase 21针对Phase 20测试中暴露的问题，从三个层面进行改进：

1. **Deep-Search Agent**: 强化新接口判断、边界检查、证据质量
2. **Orchestrator**: 优化I2处理、预算分配、rework机制
3. **Closure-Checker**: 提升反馈质量、增加自动验证

核心改进点：
- ✅ 新接口AS_IS_COMPLIANT必须提供evidence_locations（schema强制）
- ✅ Prescriptive fix必须包含边界分析（prompt要求 + reflection验证）
- ✅ I2循环最多2次重置，之后force missing或escalate
- ✅ 动态预算分配，避免浪费在循环上
- ✅ 结构化rework指令，包含学习摘要

预期效果：
- 三个失败issue至少2个通过（目标3个全部通过）
- Deep-search效率提升30%（减少无效迭代）
- Closure-checker通过率提升50%

实施后需要在三个真实issue上进行回归测试，验证改进效果。

---

## 7. 代码结构Review与重构

### 7.1 当前架构分析

**Orchestrator架构（Phase 18之后）**：
```
纯代码状态机驱动 + 4个LLM sub-agent
├─ 状态转换：PipelineState enum（纯代码）
├─ 机械检查：guards.py（纯代码）
├─ Audit构建：audit.py（纯代码）
└─ LLM调用点（仅4处）：
   ├─ Parser（1次）
   ├─ Deep-search（N次，预算30）
   ├─ Closure-checker（1-3次）
   ├─ Patch Planner（1次）
   └─ Patch Generator（1次）
```

**LLM成本分布**：
- Parser: 1次
- Deep-search: ~30次（主要成本）
- Closure-checker: 1-3次
- Patch Planner: 1次
- Patch Generator: 1次
- **总计**: ~36次LLM调用/issue

### 7.2 发现的臃肿点

#### 7.2.1 Closure-Checker语义检查不足

**问题**：
- 当前只做3种检查：`verdict_vs_code`、`findings_anti_hallucination`、`prescriptive_boundary_self_check`
- **缺少Evidence Cards内部一致性检查**：
  - `localization.exact_code_regions` vs `requirements[].evidence_locations` 的一致性
  - `constraint.missing_elements_to_implement` vs `requirements[origin=new_interfaces]` 的双向映射
  - `findings` 中的描述 vs `verdict` 的语义矛盾
  - 多个requirements的`evidence_locations`重叠时，findings是否互相矛盾

**影响**：
- Structural invariants检查（I1/I2/I3）分散在`guards.py`和`engine.py`中
- Closure-checker无法利用这些信息做语义验证
- 导致issue_001中I2循环浪费15次迭代

#### 7.2.2 约束传递到Patch Plan失败

**问题**：
- Patch planner接收完整的`EvidenceCards` JSON（可能几万tokens）
- **上下文过长**导致模型注意力分散，忽略关键约束
- **约束定义不清晰**：
  - `constraint.similar_implementation_patterns`是字符串列表，没有结构化
  - `structural.must_co_edit_relations`是自然语言描述，难以解析

**影响**：
- issue_003中patch plan未能遵守约束（状态码、授权检查、aliases数组）
- Patch planner的prompt直接dump整个evidence JSON，没有信息提炼

#### 7.2.3 Orchestrator状态机复杂度高

**问题**：
- `engine.py`的主循环有700+行
- Rework机制分散在多处：
  - I2 reset在`engine.py:566-593`
  - Closure-checker rework在`engine.py:700+`
  - Rework feedback生成在`_build_per_req_audit_feedback()`（160行）
- **错误处理逻辑重复**：每个agent调用都有try-except + memory.record_action

**影响**：
- 维护成本高，难以理解状态转换逻辑
- 错误处理代码重复5次（parser、deep-search、closure、planner、generator）

### 7.3 重构方案（按性价比排序）

#### 7.3.1 高性价比改进（纯代码 + 解决核心问题）

**A. Closure-Checker语义一致性检查（纯代码）**

在`guards.py`或新建`semantic_checks.py`中添加5种检查：

```python
# src/orchestrator/semantic_checks.py

def check_evidence_internal_consistency(evidence: EvidenceCards) -> dict[str, list[str]]:
    """检查Evidence Cards内部一致性
    
    Returns:
        dict[check_name, list[failure_messages]]
    """
    failures = {
        "localization_vs_requirements": [],
        "missing_elements_vs_new_interfaces": [],
        "findings_vs_verdict": [],
        "overlapping_findings_contradiction": [],
        "evidence_location_coverage": [],
    }
    
    # Check 1: localization.exact_code_regions应该是requirements[].evidence_locations的超集
    all_req_locs = set()
    for req in evidence.requirements:
        all_req_locs.update(req.evidence_locations)
    
    localization_locs = set(evidence.localization.exact_code_regions)
    missing_in_localization = all_req_locs - localization_locs
    
    if missing_in_localization:
        failures["localization_vs_requirements"].append(
            f"Requirements cite {len(missing_in_localization)} locations not in localization.exact_code_regions: "
            f"{list(missing_in_localization)[:3]}"
        )
    
    # Check 2: constraint.missing_elements_to_implement ↔ requirements[origin=new_interfaces]双向映射
    new_interface_reqs = [r for r in evidence.requirements if r.origin == "new_interfaces"]
    missing_elements = evidence.constraint.missing_elements_to_implement
    
    # 提取接口名称
    from src.orchestrator.guards import _extract_interface_names_from_text
    
    ni_names = set()
    for req in new_interface_reqs:
        ni_names.update(_extract_interface_names_from_text(req.text))
    
    me_names = set()
    for line in missing_elements:
        me_names.update(_extract_interface_names_from_text(line))
    
    orphan_ni = ni_names - me_names
    orphan_me = me_names - ni_names
    
    if orphan_ni:
        failures["missing_elements_vs_new_interfaces"].append(
            f"New interface requirements mention {orphan_ni} but not in missing_elements_to_implement"
        )
    if orphan_me:
        failures["missing_elements_vs_new_interfaces"].append(
            f"missing_elements_to_implement mentions {orphan_me} but no corresponding new_interface requirement"
        )
    
    # Check 3: findings中的描述 vs verdict的语义矛盾
    contradiction_keywords = {
        "AS_IS_COMPLIANT": ["does not exist", "missing", "not found", "absent"],
        "TO_BE_MISSING": ["already exists", "implemented", "present", "found at"],
        "AS_IS_VIOLATED": ["compliant", "correct", "satisfies", "meets requirement"],
    }
    
    for req in evidence.requirements:
        if req.verdict in contradiction_keywords:
            findings_lower = req.findings.lower()
            for keyword in contradiction_keywords[req.verdict]:
                if keyword in findings_lower:
                    failures["findings_vs_verdict"].append(
                        f"{req.id}: verdict={req.verdict} but findings contains '{keyword}'"
                    )
                    break
    
    # Check 4: 重叠evidence_locations的findings是否矛盾
    from src.orchestrator.audit import _locations_overlap
    
    for i, req_a in enumerate(evidence.requirements):
        for req_b in evidence.requirements[i+1:]:
            # 检查是否有重叠的locations
            has_overlap = any(
                _locations_overlap(loc_a, loc_b)
                for loc_a in req_a.evidence_locations
                for loc_b in req_b.evidence_locations
            )
            
            if has_overlap:
                # 检查findings是否矛盾（简单关键词检测）
                findings_a = req_a.findings.lower()
                findings_b = req_b.findings.lower()
                
                # 如果一个说"不存在"，另一个说"已实现"
                if ("not exist" in findings_a or "missing" in findings_a) and \
                   ("exists" in findings_b or "implemented" in findings_b):
                    failures["overlapping_findings_contradiction"].append(
                        f"{req_a.id} and {req_b.id} have overlapping locations but contradictory findings"
                    )
    
    # Check 5: evidence_locations覆盖率（是否有requirement没有证据）
    for req in evidence.requirements:
        if req.verdict not in ("UNCHECKED", "AS_IS_COMPLIANT"):
            if not req.evidence_locations:
                failures["evidence_location_coverage"].append(
                    f"{req.id}: verdict={req.verdict} but no evidence_locations"
                )
    
    return failures
```

**集成到Orchestrator**：

```python
# src/orchestrator/engine.py

# 在EvidenceRefining状态，sufficiency和attribution检查之后
semantic_failures = check_evidence_internal_consistency(current_evidence)

# 如果有严重失败，返回deep-search
critical_failures = []
for check_name, failures in semantic_failures.items():
    if failures and check_name in ["findings_vs_verdict", "evidence_location_coverage"]:
        critical_failures.extend(failures)

if critical_failures and not budget.is_exhausted():
    print(f"[orchestrator] semantic consistency check failed: {critical_failures[:3]}", flush=True)
    # 重置相关requirements
    for failure in critical_failures:
        req_id_match = _REQ_ID_RE.search(failure)
        if req_id_match:
            req_id = req_id_match.group(0)
            reset_requirement_for_rework(
                req_id,
                audit_feedback=f"Semantic consistency check failed: {failure}"
            )
    state = PipelineState.UNDER_SPECIFIED
    continue

# 将非严重失败作为warnings传递给closure-checker
semantic_warnings = []
for check_name, failures in semantic_failures.items():
    if failures and check_name not in ["findings_vs_verdict", "evidence_location_coverage"]:
        semantic_warnings.extend(failures)

manifest: AuditManifest = build_audit_manifest(
    current_evidence,
    structural_warnings=i1_i3_warnings + semantic_warnings,  # 合并warnings
)
```

**B. 结构化Evidence字段（改schema）**

```python
# src/models/evidence.py

class SimilarImplementationPattern(BaseModel):
    """结构化的相似实现模式"""
    file_path: str = Field(description="文件路径，如 src/user/email.js")
    location: str = Field(description="具体位置，如 lines 47-68")
    function_name: str = Field(description="函数/方法名称")
    pattern_type: str = Field(
        description="模式类型：data_fetch | validation | error_handling | state_update | etc"
    )
    description: str = Field(description="模式描述，如何应用到当前问题")

class MustCoEditRelation(BaseModel):
    """结构化的co-edit关系"""
    trigger_location: str = Field(description="触发位置，如 src/api/auth.js:42-58")
    dependent_locations: list[str] = Field(description="依赖位置列表")
    reason: str = Field(description="为什么必须同时修改")

class ConstraintCard(BaseModel):
    # ... existing fields ...
    
    similar_implementation_patterns: list[SimilarImplementationPattern] = Field(
        default_factory=list,
        description="结构化的相似实现模式"
    )

class StructuralCard(BaseModel):
    must_co_edit_relations: list[MustCoEditRelation] = Field(
        default_factory=list,
        description="结构化的co-edit关系"
    )
    # ... other fields ...
```

**更新Deep-search的structured output schema**：

```python
# src/agents/deep_search_agent.py

class DeepSearchReport(BaseModel):
    # ... existing fields ...
    
    similar_patterns: list[SimilarImplementationPattern] = Field(
        default_factory=list,
        description="发现的相似实现模式（结构化）"
    )
    
    co_edit_relations: list[MustCoEditRelation] = Field(
        default_factory=list,
        description="发现的co-edit关系（结构化）"
    )
```

**C. Orchestrator错误处理统一（decorator）**

```python
# src/orchestrator/error_handling.py

from functools import wraps
from typing import Callable, Any
from src.models.memory import SharedWorkingMemory

def handle_agent_error(
    agent_name: str,
    phase: str,
    memory: SharedWorkingMemory,
    budget: Any = None,
):
    """统一的agent错误处理装饰器"""
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                result = await func(*args, **kwargs)
                return result, None  # (result, error)
            except Exception as exc:
                if budget:
                    budget.record_iteration()
                
                print(
                    f"[orchestrator] {agent_name} failed: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                
                memory.record_action(
                    phase=phase,
                    subagent=agent_name,
                    outcome=f"error:{type(exc).__name__}",
                )
                
                return None, exc  # (result, error)
        return wrapper
    return decorator
```

**使用示例**：

```python
# src/orchestrator/engine.py

@handle_agent_error("deep-search", "deep-search", memory, budget)
async def run_deep_search_with_error_handling(todo_task, evidence, repo_dir):
    return await _run_deep_search_async(todo_task, evidence, repo_dir)

# 在主循环中
result, error = await run_deep_search_with_error_handling(todo_task, current_evidence, repo_dir)
if error:
    state = PipelineState.EVIDENCE_REFINING
    continue

report = result
# ... 继续处理
```

#### 7.3.2 中等性价比改进（增加LLM调用，但解决关键问题）

**D. Closure-Checker添加Reflection（+1次LLM调用）**

**理由**：
- Closure-checker是质量门禁，误判成本极高
- 当前issue_001/002/003都因closure-checker拒绝而失败
- 增加1次LLM调用（从3次变4次），但能显著降低误判率

**实现方案**：

```python
# src/agents/closure_checker_agent.py

CLOSURE_REFLECTION_PROMPT = """\
You are reviewing your own closure-checker verdict.

Your task: verify your audit results are correct before finalizing.

SELF-REFLECTION CHECKS:

1. VERDICT CONSISTENCY
   For each AuditResult you marked as FAIL:
   - Re-read the cited evidence_locations
   - Verify: does the code actually contradict the requirement's verdict?
   - If you're unsure, mark as PASS with a note rather than FAIL

2. FALSE POSITIVE CHECK
   Common false positive patterns:
   - Marking AS_IS_COMPLIANT as FAIL because "no evidence_locations"
     → AS_IS_COMPLIANT is exempt from evidence_locations requirement
   - Marking prescriptive fix as FAIL for edge cases that are out of scope
     → Only fail if the edge case is explicitly mentioned in requirement
   - Marking findings as hallucination when the code exists but in different format
     → Verify the semantic meaning, not just exact string match

3. SEVERITY CALIBRATION
   Ask yourself: "If I approve this evidence, will patch planner have enough info?"
   - If YES: consider changing FAIL to PASS with warning
   - If NO: keep FAIL and provide specific guidance

If reflection reveals issues, revise your ClosureVerdict.
Return the final ClosureVerdict (original or revised).
"""

async def _run_closure_checker_async(
    evidence: EvidenceCards,
    manifest: AuditManifest,
    repo_dir: Path | None = None,
) -> ClosureVerdict:
    # Round 1: 初始判断
    verdict = await run_structured_query(
        system_prompt=CLOSURE_CHECKER_SYSTEM_PROMPT,
        user_prompt=...,
        response_model=ClosureVerdict,
        component="closure-checker",
        allowed_tools=["Grep", "Read", "Glob"],
        max_turns=30,
        max_budget_usd=2.5,
        cwd=str(repo_dir) if repo_dir is not None else None,
    )
    
    # Round 2: Reflection（仅当verdict=EVIDENCE_MISSING时）
    if verdict.verdict == "EVIDENCE_MISSING":
        verdict_json = verdict.model_dump_json(indent=2)
        evidence_json = evidence.model_dump_json(indent=2)
        
        reflection_prompt = (
            f"## Your Initial Verdict\n"
            f"```json\n{verdict_json}\n```\n\n"
            f"## Evidence Cards\n"
            f"```json\n{evidence_json}\n```\n\n"
            "Review your verdict using the self-reflection checks. "
            "Return a ClosureVerdict (original or revised)."
        )
        
        try:
            reflected_verdict = await run_structured_query(
                system_prompt=CLOSURE_REFLECTION_PROMPT,
                user_prompt=reflection_prompt,
                response_model=ClosureVerdict,
                component="closure-checker-reflection",
                allowed_tools=["Read", "Grep"],  # 只允许读取，不允许Glob
                max_turns=10,
                max_budget_usd=1.0,
                cwd=str(repo_dir) if repo_dir is not None else None,
            )
            
            # 如果reflection改变了verdict，记录
            if reflected_verdict.verdict != verdict.verdict:
                print(
                    f"[closure-checker] Reflection changed verdict: "
                    f"{verdict.verdict} → {reflected_verdict.verdict}",
                    flush=True,
                )
            
            return reflected_verdict
            
        except Exception as exc:
            print(
                f"[closure-checker] Reflection failed ({type(exc).__name__}), "
                f"using initial verdict",
                flush=True,
            )
            return verdict
    
    return verdict
```

**成本分析**：
- 当前：3次closure调用（每次2.5 USD预算）= 7.5 USD
- 改进后：3次closure + 3次reflection（每次1.0 USD预算）= 10.5 USD
- **增加成本**：3 USD/issue（约40%增加）
- **收益**：降低误判率，减少无效rework轮次

#### 7.3.3 低性价比改进（不建议）

**E. Evidence Card统一更新机制**

**当前机制已够用**：
- `update_localization()`一次性替换整个scope的所有字段（原子操作）
- Python字典操作本身就是原子的
- 不需要额外的"事务"或"回滚"机制

**不建议做**：
- ❌ 创建`EvidenceUpdateTransaction`类（过度设计）
- ❌ 添加更新日志（调试用，非核心功能）

**F. Patch Planner/Generator Reflection**

**成本过高**：
- 当前：1次planner + 1次generator = 2次LLM调用
- 改进后：1次planner + 1次reflection + 1次generator + 1次reflection = 4次
- **增加成本**：翻倍

**收益不明确**：
- Patch质量问题主要来自evidence不足，而非planner/generator能力
- 应该优先改进evidence质量（方向A/B/C/D）

### 7.4 实施优先级（最终版）

**Phase 21.1: 核心改进（必做）**
- [ ] A. Closure-Checker语义一致性检查（纯代码，5种检查）
- [ ] B. 结构化Evidence字段（改schema）
- [ ] C. Orchestrator错误处理统一（decorator）
- [ ] D. Closure-Checker添加Reflection（+1次LLM调用）

**Phase 21.2: 可选改进（根据测试结果决定）**
- [ ] Orchestrator主循环重构（降低维护成本）
- [ ] 创建`ConstraintSummary`模型（减少patch planner的token消耗）

**不做**：
- ❌ Evidence Card统一更新机制（当前已够用）
- ❌ Patch Planner/Generator Reflection（成本翻倍，收益不明确）

### 7.5 成本收益分析

**当前成本**（每个issue）：
- LLM调用：~36次
- 主要成本：Deep-search（30次）

**Phase 21改进后**：
- LLM调用：~39次（+3次closure reflection）
- **增加成本**：~8%
- **预期收益**：
  - 减少无效deep-search迭代（从30次降到20次）
  - 减少rework轮次（从3轮降到1轮）
  - **净收益**：减少~30% LLM成本

**ROI计算**：
- 投入：8%成本增加（reflection）
- 产出：30%成本减少（减少无效迭代）
- **净ROI**：22%成本节省

```


```



