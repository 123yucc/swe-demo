# Phase 20: Evidence Quality & Evaluation Infrastructure Hardening

## Executive Summary

**Problem**: Issue002 analysis revealed that patch generation failures stem from two interconnected root causes:
1. **Evidence quality issues**: Parser and Deep Search agents misunderstood task intent, leading to incorrect verdicts
2. **Evaluation infrastructure fragility**: Lack of schema contracts and error classification causes silent failures

**Solution**: Multi-layered defense system combining evidence generation improvements, patch quality gates, and evaluation infrastructure hardening.

**Impact**: Prevents over-modification patches (like issue002) and distinguishes infrastructure errors from genuine test failures.

---

## Issue 002 Root Cause Analysis

### The Problem

**Gold Standard (test_patch):**
- Move `TestHideQtWarning` class from `test_log.py` to `test_qtlog.py`
- Change `log.hide_qt_warning` ¡ú `qtlog.hide_qt_warning` in tests
- **Pure test migration task** ¡ª no implementation changes needed

**Generated Patch:**
- Re-implemented `hide_qt_warning` and `QtWarningFilter` (moved from log.py to qtlog.py)
- Deleted original implementation in log.py
- Added re-export aliases
- Created brand new test cases (instead of migrating existing ones)
- Modified unrelated files (vulture whitelist, copyright years)

**Result**: Massive over-modification that changes API signatures and rewrites tests unnecessarily.

### Evidence Card Failures

#### 1. Parser Agent Misunderstanding

**Symptom Card Error:**
```json
"observable_failures": [
  "The hide_qt_warning function and its associated tests have been moved from log.py to qtlog.py, 
   but the tests have not been updated..."
]
```

**Problem**: This description **incorrectly implies** the code has already been moved, when in reality:
- Code is **not yet** moved from log.py to qtlog.py
- The task is to **move tests**, not move implementation
- "New interfaces introduced" should mean "verify these exist" not "create these"

#### 2. Deep Search Agent Over-Specification

**Verdict Error:**
- `req-007` (hide_qt_warning function) marked as `TO_BE_PARTIAL` because signature mismatch
- Existing implementation uses `*loggers` (variadic)
- Requirement specifies `logger: str = 'qt'` (single optional)
- **Reality**: `*loggers` is a compatible superset ¡ª more flexible but backward compatible

**Problem**: Deep Search didn't recognize:
- Existing implementation is **correct** and **more powerful**
- Signature in problem_statement is **documentation**, not strict contract
- Real task is **test migration**, not implementation modification

#### 3. Closure Checker Approval of Wrong Evidence

**Problem**: Closure checker approved evidence with:
- Incorrect task type identification (feature implementation vs test migration)
- Wrong verdict on req-007 (TO_BE_PARTIAL should be AS_IS_COMPLIANT)
- No detection of scope creep risk

### Evaluation Infrastructure Issues

**Discovered by peer review:**

1. **Exit 0 ¡Ù Valid Results**: System doesn't distinguish "infrastructure error" from "test failure"
2. **Implicit Contracts**: Evaluation chain relies on file name conventions, not explicit schemas
3. **Late Quality Control**: Patch quality issues only caught at evaluation time (expensive)

---

## Phase 20 Improvement Plan

### Priority Tier 1: Evidence Generation Quality (Root Cause)

**Timeline**: 2 weeks  
**Goal**: Prevent evidence misinterpretation like issue002

#### 1.1 Parser Agent: Task Type Recognition

**New Data Model:**
```python
class TaskType(Enum):
    TEST_MIGRATION = "test_migration"      # Only move tests
    FEATURE_IMPLEMENTATION = "feature"     # Implement new functionality
    BUG_FIX = "bug_fix"                   # Fix existing bug
    REFACTORING = "refactoring"           # Restructure without behavior change

class EvidenceCards:
    # ... existing fields ...
    task_type: TaskType           # NEW: Explicit task classification
    task_intent: str              # NEW: One-sentence "what we're really doing"
    scope_constraints: List[str]  # NEW: What should NOT be modified
```

**Implementation:**

```python
# In src/agents/parser_agent.py

TASK_TYPE_PATTERNS = {
    TaskType.TEST_MIGRATION: [
        r"tests? (have been |need to be )?(moved|relocated|migrated)",
        r"move tests? (from|to)",
        r"tests? should be (in|under|relocated to)"
    ],
    TaskType.FEATURE_IMPLEMENTATION: [
        r"implement|add|create (new )?(feature|functionality|interface)",
        r"new (class|function|module|component)"
    ],
    TaskType.BUG_FIX: [
        r"fix|resolve|correct (bug|issue|error|failure)",
        r"(not working|broken|failing|incorrect)"
    ],
    TaskType.REFACTORING: [
        r"refactor|reorganize|restructure",
        r"move (code|function|class) (from|to)",
        r"better organize"
    ]
}

def identify_task_type(problem_statement: str) -> TaskType:
    """Classify task based on problem statement keywords."""
    for task_type, patterns in TASK_TYPE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, problem_statement, re.IGNORECASE):
                return task_type
    return TaskType.BUG_FIX  # Default fallback
```

**Parser Prompt Enhancement:**

```markdown
## Task Type Identification

First, classify the task type:
- TEST_MIGRATION: Only moving/updating tests, no implementation changes
- FEATURE_IMPLEMENTATION: Adding new functionality
- BUG_FIX: Fixing broken behavior
- REFACTORING: Restructuring without behavior change

For "New interfaces introduced" section:
1. Check if these interfaces ALREADY EXIST in the codebase
2. If they exist: This is a VERIFICATION task, not CREATION task
3. Mark task_intent accordingly: "Verify X exists and works" vs "Implement X"

For TEST_MIGRATION tasks:
- Symptom should describe: "Tests are in wrong location"
- Repair target: "Tests should be in correct module"
- DO NOT suggest implementation changes
- scope_constraints: ["No changes to implementation files outside tests/"]
```

**Validation:**
```python
def _enforce_parser_task_type_consistency(cards: EvidenceCards) -> EvidenceCards:
    """Ensure evidence is consistent with identified task type."""
    if cards.task_type == TaskType.TEST_MIGRATION:
        # Test migration should not have TO_BE_MISSING/TO_BE_PARTIAL for implementation
        for req in cards.requirements:
            if req.origin == "new_interfaces" and "test" not in req.text.lower():
                # This is likely a misunderstanding
                logger.warning(f"TEST_MIGRATION task has non-test new_interface: {req.id}")
    
    return cards
```

---

#### 1.2 Deep Search Agent: API Compatibility Judgment

**Problem**: Current deep search treats signature differences as violations, even when new signature is a compatible superset.

**Solution**: Add semantic compatibility checking.

**New Module: `src/orchestrator/api_compatibility.py`**

```python
from typing import Tuple, Optional
import ast
import inspect

class SignatureCompatibility:
    """Check if actual API signature is compatible with required signature."""
    
    @staticmethod
    def parse_signature(sig_str: str) -> dict:
        """
        Parse signature string into structured format.
        Example: "pattern: str, logger: str = 'qt'" 
        ¡ú {"pattern": {"type": "str", "default": None},
           "logger": {"type": "str", "default": "'qt'"}}
        """
        # Implementation using ast.parse or regex
        pass
    
    @staticmethod
    def is_compatible(required_sig: str, actual_sig: str) -> Tuple[bool, Optional[str]]:
        """
        Check if actual signature satisfies required signature.
        
        Returns:
            (is_compatible, reason)
        
        Examples:
            required: "pattern: str, logger: str = 'qt'"
            actual:   "pattern: str, *loggers: str"
            ¡ú (True, "Variadic *loggers is superset of single optional logger")
            
            required: "x: int, y: int"
            actual:   "x: int"
            ¡ú (False, "Missing required parameter: y")
        """
        req_params = SignatureCompatibility.parse_signature(required_sig)
        act_params = SignatureCompatibility.parse_signature(actual_sig)
        
        # Check 1: All required params present
        for param_name, param_info in req_params.items():
            if param_info["default"] is None:  # Required param
                if param_name not in act_params:
                    return False, f"Missing required parameter: {param_name}"
        
        # Check 2: Variadic params can replace optional params
        if "*" in actual_sig:
            # Extract variadic param name
            variadic_match = re.search(r'\*(\w+)', actual_sig)
            if variadic_match:
                variadic_name = variadic_match.group(1)
                # Check if this replaces optional params
                optional_params = [k for k, v in req_params.items() if v["default"] is not None]
                if optional_params:
                    return True, f"Variadic *{variadic_name} is compatible superset of optional {optional_params}"
        
        # Check 3: Default values compatible
        for param_name in req_params:
            if param_name in act_params:
                req_default = req_params[param_name]["default"]
                act_default = act_params[param_name]["default"]
                if req_default != act_default and act_default is not None:
                    return False, f"Incompatible default for {param_name}: {req_default} vs {act_default}"
        
        return True, "Signatures are compatible"
```

**Deep Search Prompt Enhancement:**

```markdown
## Verdict Assignment for new_interfaces Requirements

When checking if a "new interface" requirement is satisfied:

1. **First check if interface already exists** in the codebase
2. **If it exists**, compare signatures using SEMANTIC compatibility:
   - `*args` can satisfy multiple optional positional params
   - `**kwargs` can satisfy multiple optional keyword params
   - More flexible signature is ACCEPTABLE if backward compatible
   
3. **Verdict rules**:
   - AS_IS_COMPLIANT: Interface exists with compatible or better signature
   - TO_BE_PARTIAL: Interface exists but signature is incompatible (breaking change)
   - TO_BE_MISSING: Interface does not exist at all

Example:
- Required: `def foo(x: int, y: int = 0)`
- Actual: `def foo(x: int, *args: int)`
- Verdict: AS_IS_COMPLIANT (variadic *args can accept y=0 and more)

4. **For TEST_MIGRATION tasks**: If implementation already exists and works,
   DO NOT mark as TO_BE_PARTIAL just because of signature style differences.
```

**Integration in Deep Search:**

```python
# In src/agents/deep_search_agent.py

from src.orchestrator.api_compatibility import SignatureCompatibility

async def _check_new_interface_requirement(
    req: RequirementItem,
    task_type: TaskType
) -> Tuple[Verdict, str]:
    """Check if new interface requirement is satisfied."""
    
    # Extract expected signature from requirement text
    expected_sig = extract_signature_from_requirement(req.text)
    
    # Search for actual implementation
    actual_impl = await search_for_interface(req)
    
    if not actual_impl:
        return Verdict.TO_BE_MISSING, "Interface not found in codebase"
    
    # Check signature compatibility
    is_compat, reason = SignatureCompatibility.is_compatible(
        expected_sig, 
        actual_impl.signature
    )
    
    if is_compat:
        return Verdict.AS_IS_COMPLIANT, f"Interface exists with compatible signature: {reason}"
    else:
        # For TEST_MIGRATION, be lenient on signature differences
        if task_type == TaskType.TEST_MIGRATION:
            return Verdict.AS_IS_COMPLIANT, f"Interface exists (signature differs but task is test migration): {reason}"
        else:
            return Verdict.TO_BE_PARTIAL, f"Interface exists but signature incompatible: {reason}"
```

---

#### 1.3 New Guard: Structural Invariant I4 (Task-Scope Consistency)

**Add to `src/orchestrator/guards.py`:**

```python
def check_task_scope_consistency(evidence: EvidenceCards) -> GuardResult:
    """
    I4: Evidence must be consistent with task type.
    
    - TEST_MIGRATION: Should not have TO_BE_* verdicts for implementation code
    - FEATURE_IMPLEMENTATION: Should have TO_BE_MISSING for new interfaces
    - BUG_FIX: Should have AS_IS_VIOLATED for buggy code
    """
    issues = []
    
    if evidence.task_type == TaskType.TEST_MIGRATION:
        # Check: No implementation changes should be required
        impl_changes = [
            req for req in evidence.requirements
            if req.verdict in [Verdict.TO_BE_MISSING, Verdict.TO_BE_PARTIAL]
            and not any(loc.startswith("tests/") for loc in req.evidence_locations)
        ]
        if impl_changes:
            issues.append(
                f"TEST_MIGRATION task should not require implementation changes, "
                f"but found {len(impl_changes)} requirements: {[r.id for r in impl_changes]}"
            )
    
    elif evidence.task_type == TaskType.FEATURE_IMPLEMENTATION:
        # Check: Should have at least one TO_BE_MISSING requirement
        new_interfaces = [
            req for req in evidence.requirements
            if req.origin == "new_interfaces" and req.verdict == Verdict.TO_BE_MISSING
        ]
        if not new_interfaces:
            issues.append(
                "FEATURE_IMPLEMENTATION task should have at least one TO_BE_MISSING interface, "
                "but all interfaces are marked AS_IS_COMPLIANT"
            )
    
    if issues:
        return GuardResult(
            passed=False,
            reason=f"Task-scope consistency check failed: {'; '.join(issues)}",
            failed_invariant="I4"
        )
    
    return GuardResult(passed=True)
```

**Integration in Orchestrator:**

```python
# In src/orchestrator/engine.py, before closure checker

# After I1, I2, I3 checks
i4_result = check_task_scope_consistency(self.memory.evidence_cards)
if not i4_result.passed:
    logger.warning(f"I4 failed: {i4_result.reason}")
    # Option A: Force rework
    return self._trigger_rework("structural_invariant_i4", i4_result.reason)
    
    # Option B: Just warn and continue (less strict)
    # Continue to closure checker
```

---

### Priority Tier 2: Evaluation Infrastructure Hardening

**Timeline**: 1 week  
**Goal**: Distinguish infrastructure errors from test failures, prevent silent failures

#### 2.1 Schema Guard Layer (Direction A)

**Problem**: No validation of evaluation inputs/outputs, leading to silent failures when files are missing or malformed.

**Solution**: Add explicit schema validation at evaluation boundaries.

**New Module: `src/eval/schema_validator.py`**

```python
from pathlib import Path
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, ValidationError
import json

class EvalInputSchema(BaseModel):
    """Required structure for evaluation inputs."""
    instance_id: str
    model_patch: str
    base_commit: str
    repo_path: Path
    fail_to_pass_tests: List[str]
    pass_to_pass_tests: List[str]

class EvalOutputSchema(BaseModel):
    """Required structure for evaluation outputs."""
    instance_id: str
    tests: List[Dict[str, Any]]  # Must be non-empty
    resolved: bool
    
class ValidationResult(BaseModel):
    passed: bool
    errors: List[str]
    warnings: List[str]

def validate_eval_inputs(workdir: Path) -> ValidationResult:
    """
    Validate evaluation inputs before running tests.
    
    Checks:
    1. Required files exist (prediction.json, instance_metadata.json, repo/.git)
    2. JSON files are well-formed
    3. Required fields are present with correct types
    4. Repo is in clean state
    """
    errors = []
    warnings = []
    
    # Check 1: Required files
    required_files = {
        "prediction.json": workdir / "outputs" / "prediction.json",
        "instance_metadata.json": workdir / "artifacts" / "instance_metadata.json",
        "repo": workdir / "repo" / ".git"
    }
    
    for name, path in required_files.items():
        if not path.exists():
            errors.append(f"Missing required file: {name} at {path}")
    
    if errors:
        return ValidationResult(passed=False, errors=errors, warnings=warnings)
    
    # Check 2: JSON schema validation
    try:
        with open(required_files["prediction.json"]) as f:
            prediction = json.load(f)
        
        if "instance_id" not in prediction:
            errors.append("prediction.json missing 'instance_id' field")
        if "model_patch" not in prediction:
            errors.append("prediction.json missing 'model_patch' field")
        elif not isinstance(prediction["model_patch"], str):
            errors.append("prediction.json 'model_patch' must be string")
        elif len(prediction["model_patch"].strip()) == 0:
            errors.append("prediction.json 'model_patch' is empty")
            
    except json.JSONDecodeError as e:
        errors.append(f"prediction.json is not valid JSON: {e}")
    except Exception as e:
        errors.append(f"Error reading prediction.json: {e}")
    
    try:
        with open(required_files["instance_metadata.json"]) as f:
            metadata = json.load(f)
        
        if "FAIL_TO_PASS" not in metadata:
            errors.append("instance_metadata.json missing 'FAIL_TO_PASS' field")
        elif not isinstance(metadata["FAIL_TO_PASS"], list):
            errors.append("instance_metadata.json 'FAIL_TO_PASS' must be list")
        
        if "PASS_TO_PASS" not in metadata:
            warnings.append("instance_metadata.json missing 'PASS_TO_PASS' field")
            
    except json.JSONDecodeError as e:
        errors.append(f"instance_metadata.json is not valid JSON: {e}")
    except Exception as e:
        errors.append(f"Error reading instance_metadata.json: {e}")
    
    # Check 3: Repo state
    repo_path = workdir / "repo"
    if repo_path.exists():
        # Check if repo is clean (no uncommitted changes that would interfere)
        import subprocess
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            errors.append(f"Git status check failed: {result.stderr}")
    
    return ValidationResult(
        passed=len(errors) == 0,
        errors=errors,
        warnings=warnings
    )

def validate_eval_outputs(workdir: Path) -> ValidationResult:
    """
    Validate evaluation outputs after running tests.
    
    Checks:
    1. At least one structured output file exists (pytest-cases.jsonl or pytest-junit.xml)
    2. If tests list is empty, check if this is infrastructure error
    3. Log files are present
    """
    errors = []
    warnings = []
    
    # Check for structured output files
    structured_files = [
        workdir / "workspace" / "pytest-cases.jsonl",
        workdir / "workspace" / "pytest-junit.xml",
        workdir / "workspace" / "output.json"
    ]
    
    has_structured_output = any(f.exists() and f.stat().st_size > 0 for f in structured_files)
    
    if not has_structured_output:
        errors.append(
            "No structured test output found. This indicates infrastructure failure, "
            "not test failure. Check pytest installation and configuration."
        )
    
    # Check output.json if it exists
    output_json = workdir / "workspace" / "output.json"
    if output_json.exists():
        try:
            with open(output_json) as f:
                output = json.load(f)
            
            if "tests" in output:
                if not isinstance(output["tests"], list):
                    errors.append("output.json 'tests' field must be list")
                elif len(output["tests"]) == 0:
                    # Empty tests list - check if this is infrastructure error
                    stdout_log = workdir / "workspace" / "stdout.log"
                    if stdout_log.exists():
                        with open(stdout_log) as f:
                            stdout = f.read()
                        
                        # Check for pytest infrastructure errors
                        infra_error_patterns = [
                            "ERROR: file or directory not found",
                            "ERROR: usage:",
                            "ImportError:",
                            "ModuleNotFoundError:",
                            "INTERNALERROR",
                            "collection errors"
                        ]
                        
                        for pattern in infra_error_patterns:
                            if pattern in stdout:
                                errors.append(
                                    f"Empty tests list due to infrastructure error: {pattern} found in stdout"
                                )
                                break
                        else:
                            warnings.append(
                                "Empty tests list but no obvious infrastructure error pattern found. "
                                "May be legitimate (all tests skipped) or subtle infrastructure issue."
                            )
        except json.JSONDecodeError as e:
            errors.append(f"output.json is not valid JSON: {e}")
        except Exception as e:
            errors.append(f"Error reading output.json: {e}")
    
    # Check log files exist
    log_files = ["stdout.log", "stderr.log"]
    for log_file in log_files:
        log_path = workdir / "workspace" / log_file
        if not log_path.exists():
            warnings.append(f"Log file missing: {log_file}")
    
    return ValidationResult(
        passed=len(errors) == 0,
        errors=errors,
        warnings=warnings
    )
```

**Integration in Evaluation Pipeline:**

```python
# In src/main.py or evaluation orchestrator

from src.eval.schema_validator import validate_eval_inputs, validate_eval_outputs

async def run_evaluation(workdir: Path) -> EvalResult:
    """Run evaluation with schema validation gates."""
    
    # Gate 1: Validate inputs
    input_validation = validate_eval_inputs(workdir)
    if not input_validation.passed:
        return EvalResult(
            outcome=EvalOutcome.INFRA_ERROR,
            error_type="invalid_inputs",
            errors=input_validation.errors,
            tests=[]
        )
    
    # Log warnings
    for warning in input_validation.warnings:
        logger.warning(f"Input validation warning: {warning}")
    
    # Run actual evaluation
    try:
        eval_result = await _run_swe_bench_eval(workdir)
    except Exception as e:
        return EvalResult(
            outcome=EvalOutcome.INFRA_ERROR,
            error_type="evaluation_exception",
            errors=[str(e)],
            tests=[]
        )
    
    # Gate 2: Validate outputs
    output_validation = validate_eval_outputs(workdir)
    if not output_validation.passed:
        # Upgrade to INFRA_ERROR if output validation fails
        eval_result.outcome = EvalOutcome.INFRA_ERROR
        eval_result.error_type = "invalid_outputs"
        eval_result.errors = output_validation.errors
    
    return eval_result
```

---

#### 2.2 Structured-First Evaluation Output (Direction B)

**Problem**: Current evaluation relies on stdout text parsing, which is fragile and error-prone.

**Solution**: Prioritize structured output formats (JSON, JUnit XML) over text parsing.

**Modify `eval/run_script.sh`:**

```bash
#!/bin/bash
set -euo pipefail

# Install pytest plugins for structured output
pip install pytest-json-report pytest-html

# Run tests with multiple output formats
pytest \
    --json-report \
    --json-report-file=pytest-cases.jsonl \
    --json-report-indent=2 \
    --junit-xml=pytest-junit.xml \
    --tb=short \
    ${FAIL_TO_PASS_TESTS} \
    2>&1 | tee pytest-stdout.log

# Exit with pytest's exit code
exit ${PIPESTATUS[0]}
```

**New Parser Priority: `src/eval/parser.py`**

```python
from pathlib import Path
from typing import List, Dict, Any, Optional
import json
import xml.etree.ElementTree as ET

class TestResult(BaseModel):
    test_id: str
    outcome: str  # passed, failed, skipped, error
    duration: float
    message: Optional[str] = None

class ParseResult(BaseModel):
    tests: List[TestResult]
    parse_method: str  # jsonl, junit, stdout_fallback
    parse_errors: List[str]

def parse_pytest_results(workdir: Path) -> ParseResult:
    """
    Parse pytest results with fallback priority:
    1. pytest-cases.jsonl (most reliable)
    2. pytest-junit.xml (standard format)
    3. stdout text parsing (last resort)
    """
    
    # Priority 1: JSON Lines format
    jsonl_path = workdir / "pytest-cases.jsonl"
    if jsonl_path.exists():
        try:
            return parse_jsonl(jsonl_path)
        except Exception as e:
            logger.warning(f"Failed to parse JSONL: {e}, trying JUnit XML")
    
    # Priority 2: JUnit XML format
    junit_path = workdir / "pytest-junit.xml"
    if junit_path.exists():
        try:
            return parse_junit_xml(junit_path)
        except Exception as e:
            logger.warning(f"Failed to parse JUnit XML: {e}, falling back to stdout")
    
    # Priority 3: Stdout text parsing (fallback)
    stdout_path = workdir / "pytest-stdout.log"
    if stdout_path.exists():
        try:
            return parse_stdout_fallback(stdout_path)
        except Exception as e:
            return ParseResult(
                tests=[],
                parse_method="none",
                parse_errors=[f"All parsing methods failed: {e}"]
            )
    
    return ParseResult(
        tests=[],
        parse_method="none",
        parse_errors=["No pytest output files found"]
    )

def parse_jsonl(path: Path) -> ParseResult:
    """Parse pytest-json-report output."""
    with open(path) as f:
        data = json.load(f)
    
    tests = []
    for test_data in data.get("tests", []):
        tests.append(TestResult(
            test_id=test_data["nodeid"],
            outcome=test_data["outcome"],  # passed, failed, skipped
            duration=test_data.get("duration", 0.0),
            message=test_data.get("call", {}).get("longrepr")
        ))
    
    return ParseResult(
        tests=tests,
        parse_method="jsonl",
        parse_errors=[]
    )

def parse_junit_xml(path: Path) -> ParseResult:
    """Parse JUnit XML output."""
    tree = ET.parse(path)
    root = tree.getroot()
    
    tests = []
    for testcase in root.findall(".//testcase"):
        test_id = f"{testcase.get('classname')}::{testcase.get('name')}"
        duration = float(testcase.get("time", 0.0))
        
        # Determine outcome
        if testcase.find("failure") is not None:
            outcome = "failed"
            message = testcase.find("failure").get("message")
        elif testcase.find("error") is not None:
            outcome = "error"
            message = testcase.find("error").get("message")
        elif testcase.find("skipped") is not None:
            outcome = "skipped"
            message = testcase.find("skipped").get("message")
        else:
            outcome = "passed"
            message = None
        
        tests.append(TestResult(
            test_id=test_id,
            outcome=outcome,
            duration=duration,
            message=message
        ))
    
    return ParseResult(
        tests=tests,
        parse_method="junit",
        parse_errors=[]
    )

def parse_stdout_fallback(path: Path) -> ParseResult:
    """Fallback: parse stdout text (existing logic)."""
    # Keep existing regex-based parsing as last resort
    # ... existing implementation ...
    pass
```

---

#### 2.3 Evaluation Outcome Classification

**Problem**: Current system only has binary success/failure, doesn't distinguish infrastructure errors.

**Solution**: Add explicit outcome classification.

**New Enum: `src/models/verdict.py`**

```python
class EvalOutcome(str, Enum):
    """Evaluation outcome classification."""
    RESOLVED = "resolved"              # All FAIL_TO_PASS tests passed
    PARTIAL = "partial"                # Some FAIL_TO_PASS tests passed
    UNRESOLVED = "unresolved"          # No FAIL_TO_PASS tests passed
    INFRA_ERROR = "infra_error"        # Infrastructure/setup error
    PARSE_ERROR = "parse_error"        # Could not parse test results
    PATCH_APPLY_ERROR = "patch_error"  # Patch failed to apply
    REGRESSION = "regression"          # PASS_TO_PASS tests failed

class EvalResult(BaseModel):
    """Complete evaluation result."""
    instance_id: str
    outcome: EvalOutcome
    error_type: Optional[str] = None   # Specific error category
    errors: List[str] = []             # Error messages
    tests: List[TestResult] = []       # Individual test results
    fail_to_pass_passed: int = 0
    fail_to_pass_total: int = 0
    pass_to_pass_passed: int = 0
    pass_to_pass_total: int = 0
    parse_method: Optional[str] = None # How results were parsed
```

**Update `patch_outcome.json` Schema:**

```json
{
  "issue_id": "instance_...",
  "closure_checker_approved": true,
  "patch_outcome": "PATCH_SUCCESS",
  "patch_validation_verdict": "PATCH_VALIDATED",
  "patch_validation_failures": [],
  
  // NEW FIELDS
  "eval_outcome": "INFRA_ERROR",
  "eval_error_type": "empty_test_results",
  "eval_errors": [
    "No structured test output found",
    "pytest-cases.jsonl missing"
  ],
  "parse_method": "none",
  
  "tests": [],
  "fail_to_pass_passed": 0,
  "fail_to_pass_total": 4
}
```

**Outcome Decision Logic:**

```python
def determine_eval_outcome(
    tests: List[TestResult],
    fail_to_pass_tests: List[str],
    pass_to_pass_tests: List[str],
    parse_errors: List[str]
) -> EvalOutcome:
    """Determine evaluation outcome from test results."""
    
    # Infrastructure/parse errors take precedence
    if parse_errors:
        return EvalOutcome.PARSE_ERROR
    
    if not tests:
        return EvalOutcome.INFRA_ERROR
    
    # Count outcomes
    f2p_passed = sum(1 for t in tests if t.test_id in fail_to_pass_tests and t.outcome == "passed")
    f2p_total = len(fail_to_pass_tests)
    
    p2p_failed = sum(1 for t in tests if t.test_id in pass_to_pass_tests and t.outcome != "passed")
    
    # Check for regressions
    if p2p_failed > 0:
        return EvalOutcome.REGRESSION
    
    # Check FAIL_TO_PASS resolution
    if f2p_passed == f2p_total:
        return EvalOutcome.RESOLVED
    elif f2p_passed > 0:
        return EvalOutcome.PARTIAL
    else:
        return EvalOutcome.UNRESOLVED
```

---

### Priority Tier 3: Patch Quality Pre-Flight Checks

**Timeline**: 1 week  
**Goal**: Catch over-modification issues before patch application

#### 3.1 Patch Scope Validator

**Problem**: Patches can modify files outside the task scope (like issue002 modifying implementation when task is test migration).

**Solution**: Add pre-generation scope validation.

**New Module: `src/orchestrator/patch_scope_validator.py`**

```python
from typing import List, Set
from pathlib import Path
from src.models.evidence import EvidenceCards, TaskType
from src.models.patch import PatchPlan, FileEditPlan

class ScopeViolation(BaseModel):
    severity: str  # BLOCKING, WARNING
    message: str
    file_path: str
    violation_type: str

class ScopeValidationResult(BaseModel):
    approved: bool
    violations: List[ScopeViolation]

def validate_patch_scope(
    evidence: EvidenceCards,
    patch_plan: PatchPlan
) -> ScopeValidationResult:
    """
    Validate that patch plan respects task scope constraints.
    
    Checks:
    1. TEST_MIGRATION tasks should only modify test files
    2. No noise modifications (copyright, whitelist, type comments)
    3. No unnecessary file creations
    4. Modifications align with evidence locations
    """
    violations = []
    
    # Extract all files being modified
    modified_files = {fp.path for fp in patch_plan.files}
    
    # Check 1: Task type constraints
    if evidence.task_type == TaskType.TEST_MIGRATION:
        non_test_files = [
            f for f in modified_files 
            if not f.startswith("tests/") and not f.startswith("test_")
        ]
        
        if non_test_files:
            violations.append(ScopeViolation(
                severity="BLOCKING",
                message=f"TEST_MIGRATION task should not modify implementation files",
                file_path=", ".join(non_test_files),
                violation_type="task_scope_violation"
            ))
    
    # Check 2: Evidence location alignment
    evidence_files = set()
    for req in evidence.requirements:
        for loc in req.evidence_locations:
            # Extract file path from "path/to/file.py:LINE" format
            file_path = loc.split(":")[0]
            evidence_files.add(file_path)
    
    # Files being modified that are not in evidence
    unexpected_files = modified_files - evidence_files
    
    # Allow some exceptions (e.g., test files for new tests)
    allowed_patterns = [
        r"tests/.*",  # Test files
        r".*/__init__\.py",  # Package init files
    ]
    
    import re
    unexpected_files = [
        f for f in unexpected_files
        if not any(re.match(pattern, f) for pattern in allowed_patterns)
    ]
    
    if unexpected_files:
        violations.append(ScopeViolation(
            severity="WARNING",
            message=f"Modifying files not mentioned in evidence locations",
            file_path=", ".join(unexpected_files),
            violation_type="unexpected_file_modification"
        ))
    
    # Check 3: Noise modifications
    noise_patterns = {
        r"Copyright \d{4}-\d{4}": "copyright_year_update",
        r"# type: ignore": "type_ignore_addition",
        r"# noqa:": "noqa_addition",
        r"whitelist": "whitelist_modification"
    }
    
    for file_plan in patch_plan.files:
        for edit in file_plan.edits:
            for pattern, violation_type in noise_patterns.items():
                if re.search(pattern, edit.new_string):
                    violations.append(ScopeViolation(
                        severity="WARNING",
                        message=f"Potential noise modification: {violation_type}",
                        file_path=file_plan.path,
                        violation_type=violation_type
                    ))
    
    # Determine if approved
    blocking_violations = [v for v in violations if v.severity == "BLOCKING"]
    approved = len(blocking_violations) == 0
    
    return ScopeValidationResult(
        approved=approved,
        violations=violations
    )
```

---

#### 3.2 Patch Reviewer (Post-Generation)

**Problem**: Generated patches can have over-modifications that weren't caught in planning phase.

**Solution**: Hybrid review combining rule-based checks (fast, deterministic) with LLM review (semantic, context-aware).

**Design Philosophy:**
- **Rule-based checks** (Phase 20.3): Fast, deterministic, catches obvious issues
- **LLM review** (Phase 20.3+): Semantic understanding, catches subtle issues
- **Two-stage approach**: Rules first (gate), LLM second (advisory)

**New Module: `src/orchestrator/patch_reviewer.py`**

```python
import re
from typing import List, Dict, Set
from src.models.evidence import EvidenceCards, TaskType

class PatchIssue(BaseModel):
    severity: str  # CRITICAL, HIGH, MEDIUM, LOW
    category: str
    message: str
    file_path: Optional[str] = None
    line_range: Optional[str] = None

class PatchReviewResult(BaseModel):
    approved: bool
    issues: List[PatchIssue]
    summary: str

class PatchReviewer:
    """Review generated patch for quality issues."""
    
    def review_patch(
        self,
        patch_diff: str,
        evidence: EvidenceCards
    ) -> PatchReviewResult:
        """
        Comprehensive patch review.
        
        Checks:
        1. Scope alignment with task type
        2. API signature changes
        3. Test file rewrites vs migrations
        4. Noise modifications
        5. Excessive changes
        """
        issues = []
        
        # Parse patch to extract file changes
        file_changes = self._parse_patch_diff(patch_diff)
        
        # Check 1: Task scope alignment
        issues.extend(self._check_task_scope(file_changes, evidence))
        
        # Check 2: API signature changes
        issues.extend(self._check_api_changes(file_changes, evidence))
        
        # Check 3: Test file handling
        issues.extend(self._check_test_modifications(file_changes, evidence))
        
        # Check 4: Noise modifications
        issues.extend(self._check_noise_modifications(file_changes))
        
        # Check 5: Change magnitude
        issues.extend(self._check_change_magnitude(file_changes, evidence))
        
        # Determine approval
        critical_issues = [i for i in issues if i.severity == "CRITICAL"]
        approved = len(critical_issues) == 0
        
        summary = self._generate_summary(issues, file_changes)
        
        return PatchReviewResult(
            approved=approved,
            issues=issues,
            summary=summary
        )
    
    def _parse_patch_diff(self, patch_diff: str) -> Dict[str, Dict]:
        """Parse unified diff into structured format."""
        file_changes = {}
        current_file = None
        
        for line in patch_diff.split("\n"):
            if line.startswith("diff --git"):
                # Extract file path
                match = re.search(r"b/(.*?)$", line)
                if match:
                    current_file = match.group(1)
                    file_changes[current_file] = {
                        "additions": 0,
                        "deletions": 0,
                        "hunks": [],
                        "content": []
                    }
            elif current_file:
                file_changes[current_file]["content"].append(line)
                
                if line.startswith("+") and not line.startswith("+++"):
                    file_changes[current_file]["additions"] += 1
                elif line.startswith("-") and not line.startswith("---"):
                    file_changes[current_file]["deletions"] += 1
        
        return file_changes
    
    def _check_task_scope(
        self,
        file_changes: Dict,
        evidence: EvidenceCards
    ) -> List[PatchIssue]:
        """Check if modifications align with task type."""
        issues = []
        
        if evidence.task_type == TaskType.TEST_MIGRATION:
            # Should only modify test files
            non_test_files = [
                f for f in file_changes.keys()
                if not f.startswith("tests/") and not f.startswith("test_")
            ]
            
            if non_test_files:
                issues.append(PatchIssue(
                    severity="CRITICAL",
                    category="task_scope_violation",
                    message=f"TEST_MIGRATION task modifying implementation files: {non_test_files}",
                    file_path=", ".join(non_test_files)
                ))
        
        return issues
    
    def _check_api_changes(
        self,
        file_changes: Dict,
        evidence: EvidenceCards
    ) -> List[PatchIssue]:
        """Detect unexpected API signature changes."""
        issues = []
        
        # Look for function/class signature changes
        signature_pattern = r"^[-+]\s*(def|class)\s+(\w+)\s*\("
        
        for file_path, changes in file_changes.items():
            if file_path.startswith("tests/"):
                continue  # Skip test files
            
            content = "\n".join(changes["content"])
            
            # Find signature changes (lines with both - and + for same function)
            removed_sigs = set()
            added_sigs = set()
            
            for line in changes["content"]:
                match = re.match(signature_pattern, line)
                if match:
                    func_name = match.group(2)
                    if line.startswith("-"):
                        removed_sigs.add(func_name)
                    elif line.startswith("+"):
                        added_sigs.add(func_name)
            
            # Functions with signature changes
            changed_sigs = removed_sigs & added_sigs
            
            if changed_sigs:
                # Check if these changes are justified by evidence
                justified = False
                for req in evidence.requirements:
                    if any(sig in req.text for sig in changed_sigs):
                        justified = True
                        break
                
                if not justified:
                    issues.append(PatchIssue(
                        severity="HIGH",
                        category="unexpected_api_change",
                        message=f"API signature changed without evidence requirement: {changed_sigs}",
                        file_path=file_path
                    ))
        
        return issues
    
    def _check_test_modifications(
        self,
        file_changes: Dict,
        evidence: EvidenceCards
    ) -> List[PatchIssue]:
        """Check if test files are being rewritten vs migrated."""
        issues = []
        
        for file_path, changes in file_changes.items():
            if not file_path.startswith("tests/"):
                continue
            
            total_lines = changes["additions"] + changes["deletions"]
            
            # If more than 80% of lines changed, likely a rewrite
            if changes["deletions"] > 0:
                change_ratio = total_lines / (changes["deletions"] * 2)
                
                if change_ratio > 0.8 and total_lines > 50:
                    issues.append(PatchIssue(
                        severity="MEDIUM",
                        category="test_file_rewrite",
                        message=f"Test file appears to be rewritten ({total_lines} lines changed) instead of migrated",
                        file_path=file_path
                    ))
        
        return issues
    
    def _check_noise_modifications(
        self,
        file_changes: Dict
    ) -> List[PatchIssue]:
        """Detect noise modifications (copyright, whitelist, etc)."""
        issues = []
        
        noise_patterns = {
            r"Copyright.*\d{4}-\d{4}": "copyright_year_change",
            r"whitelist": "whitelist_modification",
            r"# type:\s*ignore": "type_ignore_addition",
            r"# noqa:": "noqa_comment_addition"
        }
        
        for file_path, changes in file_changes.items():
            content = "\n".join(changes["content"])
            
            for pattern, noise_type in noise_patterns.items():
                if re.search(pattern, content):
                    issues.append(PatchIssue(
                        severity="LOW",
                        category="noise_modification",
                        message=f"Noise modification detected: {noise_type}",
                        file_path=file_path
                    ))
        
        return issues
    
    def _check_change_magnitude(
        self,
        file_changes: Dict,
        evidence: EvidenceCards
    ) -> List[PatchIssue]:
        """Check if changes are proportional to evidence."""
        issues = []
        
        # Count total lines changed
        total_additions = sum(c["additions"] for c in file_changes.values())
        total_deletions = sum(c["deletions"] for c in file_changes.values())
        total_changes = total_additions + total_deletions
        
        # Count evidence locations
        evidence_locations = sum(len(req.evidence_locations) for req in evidence.requirements)
        
        # Heuristic: if changes are 10x more than evidence locations, likely over-modification
        if evidence_locations > 0 and total_changes > evidence_locations * 50:
            issues.append(PatchIssue(
                severity="MEDIUM",
                category="excessive_changes",
                message=f"Patch has {total_changes} line changes but only {evidence_locations} evidence locations. "
                        f"Possible over-modification.",
                file_path=None
            ))
        
        return issues
    
    def _generate_summary(
        self,
        issues: List[PatchIssue],
        file_changes: Dict
    ) -> str:
        """Generate human-readable summary."""
        total_files = len(file_changes)
        total_additions = sum(c["additions"] for c in file_changes.values())
        total_deletions = sum(c["deletions"] for c in file_changes.values())
        
        critical = len([i for i in issues if i.severity == "CRITICAL"])
        high = len([i for i in issues if i.severity == "HIGH"])
        medium = len([i for i in issues if i.severity == "MEDIUM"])
        low = len([i for i in issues if i.severity == "LOW"])
        
        summary = f"Patch modifies {total_files} files (+{total_additions}/-{total_deletions} lines). "
        
        if issues:
            summary += f"Found {len(issues)} issues: "
            issue_counts = []
            if critical > 0:
                issue_counts.append(f"{critical} CRITICAL")
            if high > 0:
                issue_counts.append(f"{high} HIGH")
            if medium > 0:
                issue_counts.append(f"{medium} MEDIUM")
            if low > 0:
                issue_counts.append(f"{low} LOW")
            summary += ", ".join(issue_counts)
        else:
            summary += "No issues found."
        
        return summary
```

---

#### 3.2.1 Rule-Based Review (Phase 20.3 - Week 4)

**Advantages:**
- ? Fast (< 1 second)
- ? Deterministic (no API cost, no variability)
- ? Catches 80% of obvious issues
- ? No false positives on well-defined rules

**Limitations:**
- ? Cannot understand semantic intent
- ? Misses subtle over-modifications
- ? Cannot judge "is this change necessary?"
- ? Rigid pattern matching

**What Rule-Based Review Catches:**

1. **Scope Violations** (BLOCKING)
   - TEST_MIGRATION modifying implementation files
   - Files not mentioned in evidence locations
   - Pattern: `if task_type == TEST_MIGRATION and file not in tests/`

2. **Noise Modifications** (WARNING)
   - Copyright year changes: `Copyright 2023-2024 ¡ú 2023-2025`
   - Whitelist updates: `scripts/dev/run_vulture.py`
   - Type comments: `# type: ignore`, `# noqa:`
   - Pattern: Regex matching known noise patterns

3. **API Signature Changes** (HIGH)
   - Function/class signatures modified
   - Not justified by evidence requirements
   - Pattern: Diff shows `- def foo(x)` and `+ def foo(x, y)`

4. **Test File Rewrites** (MEDIUM)
   - >80% of test file lines changed
   - Indicates rewrite instead of migration
   - Pattern: `(additions + deletions) / (deletions * 2) > 0.8`

5. **Excessive Changes** (MEDIUM)
   - Total changes >> evidence locations
   - Heuristic: `total_changes > evidence_locations * 50`

**Implementation (already shown above):**
```python
class PatchReviewer:
    def review_patch(self, patch_diff: str, evidence: EvidenceCards) -> PatchReviewResult:
        issues = []
        file_changes = self._parse_patch_diff(patch_diff)
        
        issues.extend(self._check_task_scope(file_changes, evidence))
        issues.extend(self._check_api_changes(file_changes, evidence))
        issues.extend(self._check_test_modifications(file_changes, evidence))
        issues.extend(self._check_noise_modifications(file_changes))
        issues.extend(self._check_change_magnitude(file_changes, evidence))
        
        critical_issues = [i for i in issues if i.severity == "CRITICAL"]
        approved = len(critical_issues) == 0
        
        return PatchReviewResult(approved=approved, issues=issues, ...)
```

---

#### 3.2.2 LLM-Based Review (Phase 20.3+ - Week 4+)

**Advantages:**
- ? Semantic understanding of "is this change necessary?"
- ? Can compare patch intent vs evidence intent
- ? Catches subtle over-modifications
- ? Provides natural language explanations

**Limitations:**
- ? Slower (~10-30 seconds per patch)
- ? API cost (~$0.01-0.05 per review)
- ? Non-deterministic (may vary between runs)
- ? Requires careful prompt engineering

**What LLM Review Catches:**

1. **Semantic Over-Modification**
   - "This patch adds error handling, but evidence doesn't mention error handling"
   - "This refactors variable names, but task is just to move tests"
   - "This changes implementation logic, but evidence only requires test migration"

2. **Unnecessary Abstractions**
   - "Patch introduces helper function not mentioned in evidence"
   - "Patch adds configuration parameter not required by constraints"

3. **Scope Creep**
   - "Evidence says 'move tests', patch also refactors test structure"
   - "Evidence says 'fix bug X', patch also optimizes performance"

4. **Intent Mismatch**
   - "Evidence describes simple rename, patch rewrites entire module"
   - "Evidence requires minimal fix, patch does major refactoring"

**LLM Review Architecture:**

```python
class LLMPatchReviewer:
    """LLM-powered semantic patch review."""
    
    def __init__(self):
        self.client = ClaudeSDKClient(
            model="claude-sonnet-4-6",  # Fast model for review
            max_tokens=4000
        )
    
    async def review_patch_semantic(
        self,
        patch_diff: str,
        evidence: EvidenceCards,
        rule_based_issues: List[PatchIssue]
    ) -> LLMReviewResult:
        """
        Semantic review of patch using LLM.
        
        Args:
            patch_diff: The actual patch diff
            evidence: Evidence cards (task intent, requirements, locations)
            rule_based_issues: Issues already found by rule-based review
        
        Returns:
            LLM review result with semantic issues
        """
        
        prompt = self._build_review_prompt(patch_diff, evidence, rule_based_issues)
        
        response = await self.client.query(
            prompt=prompt,
            output_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "patch_review",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "overall_verdict": {
                                "type": "string",
                                "enum": ["APPROVED", "NEEDS_REVISION", "REJECTED"]
                            },
                            "semantic_issues": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "severity": {"type": "string", "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"]},
                                        "category": {"type": "string"},
                                        "description": {"type": "string"},
                                        "file_path": {"type": "string"},
                                        "evidence_support": {"type": "string"}
                                    }
                                }
                            },
                            "unnecessary_changes": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "file_path": {"type": "string"},
                                        "change_description": {"type": "string"},
                                        "why_unnecessary": {"type": "string"}
                                    }
                                }
                            },
                            "missing_changes": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "requirement_id": {"type": "string"},
                                        "what_is_missing": {"type": "string"}
                                    }
                                }
                            },
                            "explanation": {"type": "string"}
                        },
                        "required": ["overall_verdict", "semantic_issues", "explanation"]
                    }
                }
            }
        )
        
        return LLMReviewResult.parse_obj(response)
    
    def _build_review_prompt(
        self,
        patch_diff: str,
        evidence: EvidenceCards,
        rule_based_issues: List[PatchIssue]
    ) -> str:
        """Build comprehensive review prompt."""
        
        return f"""You are a code review expert. Review this patch for semantic correctness and scope alignment.

# Task Context

**Task Type**: {evidence.task_type}
**Task Intent**: {evidence.task_intent}

**Symptom (What's broken)**:
{json.dumps(evidence.symptom.dict(), indent=2)}

**Requirements** (What needs to be satisfied):
{self._format_requirements(evidence.requirements)}

**Evidence Locations** (Where to make changes):
{self._format_evidence_locations(evidence)}

# Patch to Review

```diff
{patch_diff}
```

# Rule-Based Issues Already Found

{self._format_rule_issues(rule_based_issues)}

# Your Task

Review this patch and answer:

1. **Scope Alignment**: Does the patch ONLY address what's described in the task intent and requirements?
   - Are there changes that go beyond the stated requirements?
   - Are there "nice to have" improvements that weren't asked for?

2. **Necessity Check**: Is every change in the patch necessary to satisfy a requirement?
   - Identify any changes that don't map to a specific requirement
   - Flag refactorings, optimizations, or cleanups not mentioned in evidence

3. **Completeness**: Does the patch address ALL requirements?
   - Are there requirements that are not addressed by the patch?

4. **Semantic Correctness**: Does the patch make sense given the task type?
   - For TEST_MIGRATION: Should only move/update tests, not change implementation
   - For BUG_FIX: Should fix the bug without adding new features
   - For FEATURE_IMPLEMENTATION: Should implement the feature without breaking existing code

# Review Guidelines

**APPROVED**: Patch is minimal, necessary, and complete
- All changes map to requirements
- No scope creep
- No unnecessary modifications

**NEEDS_REVISION**: Patch has issues but is salvageable
- Some unnecessary changes that should be removed
- Minor scope creep
- Missing some requirements

**REJECTED**: Patch has major problems
- Significant scope creep (doing much more than asked)
- Changes implementation when task is test-only
- Rewrites code instead of minimal fix
- Adds features not in requirements

# Output Format

Provide structured JSON with:
- overall_verdict: APPROVED | NEEDS_REVISION | REJECTED
- semantic_issues: List of issues with severity, category, description
- unnecessary_changes: Changes that should be removed
- missing_changes: Requirements not addressed
- explanation: 2-3 sentence summary of your verdict

Be strict: If in doubt, flag it. Better to catch over-modification than let it through.
"""
    
    def _format_requirements(self, requirements: List[RequirementItem]) -> str:
        """Format requirements for prompt."""
        lines = []
        for req in requirements:
            lines.append(f"- [{req.id}] {req.text}")
            lines.append(f"  Verdict: {req.verdict}")
            if req.evidence_locations:
                lines.append(f"  Locations: {', '.join(req.evidence_locations[:3])}")
        return "\n".join(lines)
    
    def _format_evidence_locations(self, evidence: EvidenceCards) -> str:
        """Format all evidence locations."""
        all_locations = set()
        for req in evidence.requirements:
            all_locations.update(req.evidence_locations)
        
        # Group by file
        by_file = {}
        for loc in all_locations:
            file_path = loc.split(":")[0]
            if file_path not in by_file:
                by_file[file_path] = []
            by_file[file_path].append(loc)
        
        lines = []
        for file_path, locs in sorted(by_file.items()):
            lines.append(f"- {file_path}: {', '.join(locs)}")
        
        return "\n".join(lines)
    
    def _format_rule_issues(self, issues: List[PatchIssue]) -> str:
        """Format rule-based issues for context."""
        if not issues:
            return "No rule-based issues found."
        
        lines = []
        for issue in issues:
            lines.append(f"- [{issue.severity}] {issue.category}: {issue.message}")
        return "\n".join(lines)
```

---

#### 3.2.3 Hybrid Review Strategy

**Two-Stage Review Process:**

```python
class HybridPatchReviewer:
    """Combines rule-based and LLM review."""
    
    def __init__(self):
        self.rule_reviewer = PatchReviewer()
        self.llm_reviewer = LLMPatchReviewer()
    
    async def review_patch(
        self,
        patch_diff: str,
        evidence: EvidenceCards,
        use_llm: bool = True
    ) -> HybridReviewResult:
        """
        Two-stage hybrid review.
        
        Stage 1: Rule-based (always runs, fast)
        Stage 2: LLM review (optional, slower)
        
        Decision logic:
        - If rule-based finds CRITICAL issues ¡ú REJECT immediately (no LLM call)
        - If rule-based finds no issues ¡ú LLM review for semantic check
        - If rule-based finds warnings ¡ú LLM review to confirm
        """
        
        # Stage 1: Rule-based review (fast gate)
        rule_result = self.rule_reviewer.review_patch(patch_diff, evidence)
        
        critical_issues = [i for i in rule_result.issues if i.severity == "CRITICAL"]
        
        if critical_issues:
            # Fast rejection: Don't waste LLM call on obviously bad patches
            logger.info(f"Patch rejected by rule-based review: {len(critical_issues)} critical issues")
            return HybridReviewResult(
                approved=False,
                rule_based_result=rule_result,
                llm_result=None,
                final_verdict="REJECTED",
                rejection_reason="rule_based_critical_issues"
            )
        
        # Stage 2: LLM review (semantic check)
        if use_llm:
            logger.info("Rule-based review passed, running LLM semantic review...")
            llm_result = await self.llm_reviewer.review_patch_semantic(
                patch_diff,
                evidence,
                rule_result.issues  # Pass rule issues as context
            )
            
            # Combine verdicts
            if llm_result.overall_verdict == "REJECTED":
                approved = False
                final_verdict = "REJECTED"
                rejection_reason = "llm_semantic_issues"
            elif llm_result.overall_verdict == "NEEDS_REVISION":
                approved = False
                final_verdict = "NEEDS_REVISION"
                rejection_reason = "llm_unnecessary_changes"
            else:
                # LLM says APPROVED
                # But check if rule-based had warnings
                if rule_result.issues:
                    approved = True  # Allow with warnings
                    final_verdict = "APPROVED_WITH_WARNINGS"
                    rejection_reason = None
                else:
                    approved = True
                    final_verdict = "APPROVED"
                    rejection_reason = None
            
            return HybridReviewResult(
                approved=approved,
                rule_based_result=rule_result,
                llm_result=llm_result,
                final_verdict=final_verdict,
                rejection_reason=rejection_reason
            )
        else:
            # LLM review disabled, use rule-based only
            return HybridReviewResult(
                approved=rule_result.approved,
                rule_based_result=rule_result,
                llm_result=None,
                final_verdict="APPROVED" if rule_result.approved else "REJECTED",
                rejection_reason="rule_based_issues" if not rule_result.approved else None
            )
```

**Integration in Orchestrator:**

```python
# In src/orchestrator/engine.py

async def _execute_patch_phase(self) -> PipelineState:
    """Execute patch generation with hybrid review."""
    
    # ... (patch planning and generation as before) ...
    
    # Hybrid Review
    logger.info("=== PATCH PHASE: Hybrid Review ===")
    
    hybrid_reviewer = HybridPatchReviewer()
    
    # Configuration: Enable LLM review for non-trivial patches
    use_llm_review = self._should_use_llm_review(patch_diff, evidence)
    
    review_result = await hybrid_reviewer.review_patch(
        patch_diff,
        self.memory.evidence_cards,
        use_llm=use_llm_review
    )
    
    logger.info(f"Hybrid review verdict: {review_result.final_verdict}")
    
    if not review_result.approved:
        # Log detailed issues
        if review_result.rule_based_result:
            logger.error(f"Rule-based issues: {review_result.rule_based_result.issues}")
        if review_result.llm_result:
            logger.error(f"LLM semantic issues: {review_result.llm_result.semantic_issues}")
            logger.error(f"Unnecessary changes: {review_result.llm_result.unnecessary_changes}")
        
        # Write detailed failure report
        self._write_patch_outcome(
            outcome="PATCH_FAILED",
            reason=review_result.rejection_reason,
            details={
                "final_verdict": review_result.final_verdict,
                "rule_issues": [i.dict() for i in review_result.rule_based_result.issues],
                "llm_issues": review_result.llm_result.dict() if review_result.llm_result else None
            }
        )
        
        return self._transition_to(
            PipelineState.PATCH_FAILED,
            reason=f"Patch review failed: {review_result.final_verdict}"
        )
    
    # Approved (possibly with warnings)
    if review_result.final_verdict == "APPROVED_WITH_WARNINGS":
        logger.warning(f"Patch approved with warnings: {review_result.rule_based_result.issues}")
    
    # ... (continue to patch application) ...

def _should_use_llm_review(self, patch_diff: str, evidence: EvidenceCards) -> bool:
    """Decide whether to use LLM review based on patch characteristics."""
    
    # Always use LLM for:
    # 1. Large patches (>200 lines)
    # 2. Multi-file patches (>5 files)
    # 3. Implementation file changes (not just tests)
    # 4. When task type is ambiguous
    
    file_count = len(re.findall(r'^diff --git', patch_diff, re.MULTILINE))
    line_count = len(patch_diff.split('\n'))
    has_impl_changes = any(
        not line.startswith('tests/')
        for line in re.findall(r'^\+\+\+ b/(.+)$', patch_diff, re.MULTILINE)
    )
    
    if line_count > 200:
        logger.info("Using LLM review: large patch (>200 lines)")
        return True
    
    if file_count > 5:
        logger.info("Using LLM review: multi-file patch (>5 files)")
        return True
    
    if has_impl_changes and evidence.task_type == TaskType.TEST_MIGRATION:
        logger.info("Using LLM review: implementation changes in TEST_MIGRATION task")
        return True
    
    # Skip LLM for simple test-only patches
    if file_count <= 2 and not has_impl_changes:
        logger.info("Skipping LLM review: simple test-only patch")
        return False
    
    # Default: use LLM
    return True
```

---

#### 3.2.4 Cost-Benefit Analysis

**Rule-Based Review:**
- Cost: ~0 (pure code)
- Time: <1 second
- Accuracy: ~80% (catches obvious issues)
- False positive rate: <5%

**LLM Review:**
- Cost: ~$0.01-0.05 per patch (Sonnet 4.6, ~2K input + 1K output)
- Time: ~10-30 seconds
- Accuracy: ~95% (catches subtle issues)
- False positive rate: ~10-15% (may be overly cautious)

**Hybrid Strategy:**
- Cost: $0.01-0.05 per patch (only when needed)
- Time: 1-30 seconds (fast path for bad patches)
- Accuracy: ~95% (best of both)
- False positive rate: ~5-10% (LLM moderated by rules)

**When to Skip LLM Review:**
- Simple test-only patches (<100 lines, 1-2 files)
- Patches that fail rule-based review (already rejected)
- Budget constraints (can disable via config)

**Cost Projection:**
- Average patch: ~$0.02 LLM review
- 100 issues/day: $2/day = $60/month
- Acceptable for production use

---

#### 3.2.5 Example: Issue002 with Hybrid Review

**Rule-Based Review Output:**
```json
{
  "approved": false,
  "issues": [
    {
      "severity": "CRITICAL",
      "category": "task_scope_violation",
      "message": "TEST_MIGRATION task modifying implementation files",
      "file_path": "qutebrowser/utils/log.py, qutebrowser/utils/qtlog.py"
    },
    {
      "severity": "WARNING",
      "category": "noise_modification",
      "message": "Copyright year change detected",
      "file_path": "tests/unit/utils/test_qtlog.py"
    },
    {
      "severity": "MEDIUM",
      "category": "test_file_rewrite",
      "message": "Test file rewritten (165 lines changed) instead of migrated",
      "file_path": "tests/unit/utils/test_qtlog.py"
    }
  ]
}
```

**Result**: Rejected immediately by rule-based review (CRITICAL issue), no LLM call needed.

**If rule-based only found warnings**, LLM would review:

```json
{
  "overall_verdict": "REJECTED",
  "semantic_issues": [
    {
      "severity": "CRITICAL",
      "category": "scope_creep",
      "description": "Patch re-implements hide_qt_warning and QtWarningFilter in qtlog.py, but evidence shows these already exist. Task is to move TESTS, not re-implement functionality.",
      "file_path": "qutebrowser/utils/qtlog.py",
      "evidence_support": "req-007 and req-008 are marked AS_IS_COMPLIANT, meaning implementation already exists"
    }
  ],
  "unnecessary_changes": [
    {
      "file_path": "qutebrowser/utils/log.py",
      "change_description": "Deletes hide_qt_warning and QtWarningFilter implementation, adds re-export",
      "why_unnecessary": "Task is TEST_MIGRATION - should not touch implementation files at all"
    },
    {
      "file_path": "qutebrowser/utils/qtlog.py",
      "change_description": "Adds hide_qt_warning and QtWarningFilter implementation",
      "why_unnecessary": "These already exist (per evidence), task is to move tests not code"
    },
    {
      "file_path": "scripts/dev/run_vulture.py",
      "change_description": "Updates whitelist path",
      "why_unnecessary": "Noise modification not related to test migration"
    }
  ],
  "explanation": "This patch significantly exceeds the task scope. The task is TEST_MIGRATION (move tests from test_log.py to test_qtlog.py), but the patch re-implements the entire hide_qt_warning functionality and modifies multiple implementation files. The correct patch should only move the TestHideQtWarning class and update import statements."
}
```

**Final Verdict**: REJECTED (would have been caught by either stage)

---

#### 3.2.6 Configuration & Tuning

**Config File: `.claude/patch_review_config.json`**

```json
{
  "rule_based_review": {
    "enabled": true,
    "blocking_severities": ["CRITICAL"],
    "noise_patterns": [
      "Copyright \\d{4}-\\d{4}",
      "# type:\\s*ignore",
      "# noqa:",
      "whitelist"
    ]
  },
  "llm_review": {
    "enabled": true,
    "model": "claude-sonnet-4-6",
    "skip_conditions": {
      "max_files_for_skip": 2,
      "max_lines_for_skip": 100,
      "test_only_skip": true
    },
    "strictness": "balanced",  // "lenient" | "balanced" | "strict"
    "max_retries": 1
  },
  "hybrid_strategy": {
    "fast_reject_on_critical": true,
    "llm_confirms_warnings": true,
    "approval_requires_both": false  // false = either can approve, true = both must approve
  }
}
```

**Strictness Levels:**
- **Lenient**: Only flag obvious over-modifications, allow minor scope creep
- **Balanced**: Flag unnecessary changes, moderate on "nice to have" improvements
- **Strict**: Flag any change not explicitly mentioned in evidence

---

## Summary: Why Hybrid Approach?

**Rule-Based Alone:**
- ? Fast and cheap
- ? Misses semantic issues (like issue002's "re-implementation when migration needed")
- ? Cannot judge "is this necessary?"

**LLM Alone:**
- ? Catches everything
- ? Slow and expensive for every patch
- ? Overkill for simple patches

**Hybrid (Best of Both):**
- ? Fast rejection of obviously bad patches (rules)
- ? Semantic review of ambiguous cases (LLM)
- ? Cost-effective (LLM only when needed)
- ? High accuracy with low false positive rate

**Phase 20.3 Implementation Priority:**
1. Week 4 Day 1-2: Rule-based reviewer (core checks)
2. Week 4 Day 3-4: LLM reviewer (prompt + integration)
3. Week 4 Day 5: Hybrid strategy + configuration
4. Week 4 Weekend: Testing on issue001/002/003

This gives us a production-ready patch review system that catches issue002-style problems while remaining fast and cost-effective.

---

#### 3.3 Integration into Orchestrator

**Modify `src/orchestrator/engine.py`:**

```python
from src.orchestrator.patch_scope_validator import validate_patch_scope
from src.orchestrator.patch_reviewer import PatchReviewer

async def _execute_patch_phase(self) -> PipelineState:
    """
    Execute patch generation with quality gates.
    
    Flow:
    1. Patch Planning
    2. Scope Validation (pre-generation gate)
    3. Patch Generation
    4. Patch Review (post-generation gate)
    5. Patch Application
    """
    
    logger.info("=== PATCH PHASE: Planning ===")
    
    # Step 1: Patch Planning
    patch_plan = await self._run_patch_planner()
    if not patch_plan:
        return self._transition_to(
            PipelineState.PATCH_FAILED,
            reason="Patch planner failed to generate plan"
        )
    
    # Step 2: Scope Validation (NEW)
    logger.info("=== PATCH PHASE: Scope Validation ===")
    scope_validation = validate_patch_scope(
        self.memory.evidence_cards,
        patch_plan
    )
    
    if not scope_validation.approved:
        blocking_violations = [
            v for v in scope_validation.violations 
            if v.severity == "BLOCKING"
        ]
        
        logger.error(f"Patch scope validation failed: {blocking_violations}")
        
        # Write to patch_outcome.json
        self._write_patch_outcome(
            outcome="PATCH_FAILED",
            reason="scope_validation_failed",
            details={
                "violations": [v.dict() for v in scope_validation.violations]
            }
        )
        
        return self._transition_to(
            PipelineState.PATCH_FAILED,
            reason=f"Patch scope validation failed: {len(blocking_violations)} blocking violations"
        )
    
    # Log warnings
    warnings = [v for v in scope_validation.violations if v.severity == "WARNING"]
    if warnings:
        logger.warning(f"Patch scope validation warnings: {warnings}")
    
    # Step 3: Patch Generation
    logger.info("=== PATCH PHASE: Generation ===")
    patch_diff = await self._run_patch_generator(patch_plan)
    if not patch_diff:
        return self._transition_to(
            PipelineState.PATCH_FAILED,
            reason="Patch generator failed to produce diff"
        )
    
    # Step 4: Patch Review (NEW)
    logger.info("=== PATCH PHASE: Review ===")
    reviewer = PatchReviewer()
    review_result = reviewer.review_patch(
        patch_diff,
        self.memory.evidence_cards
    )
    
    logger.info(f"Patch review: {review_result.summary}")
    
    if not review_result.approved:
        critical_issues = [i for i in review_result.issues if i.severity == "CRITICAL"]
        
        logger.error(f"Patch review failed: {critical_issues}")
        
        # Option A: Reject immediately
        self._write_patch_outcome(
            outcome="PATCH_FAILED",
            reason="patch_review_failed",
            details={
                "issues": [i.dict() for i in review_result.issues],
                "summary": review_result.summary
            }
        )
        
        return self._transition_to(
            PipelineState.PATCH_FAILED,
            reason=f"Patch review failed: {len(critical_issues)} critical issues"
        )
        
        # Option B: Give patch generator one retry with feedback (future enhancement)
        # rework_feedback = [i.message for i in critical_issues]
        # patch_diff = await self._run_patch_generator(patch_plan, rework_feedback)
    
    # Log non-critical issues
    non_critical = [i for i in review_result.issues if i.severity != "CRITICAL"]
    if non_critical:
        logger.warning(f"Patch review non-critical issues: {non_critical}")
    
    # Step 5: Patch Application
    logger.info("=== PATCH PHASE: Application ===")
    success = self._apply_patch(patch_diff)
    
    if success:
        self._write_patch_outcome(
            outcome="PATCH_SUCCESS",
            patch_diff=patch_diff,
            review_summary=review_result.summary
        )
        return self._transition_to(PipelineState.PATCH_SUCCESS)
    else:
        self._write_patch_outcome(
            outcome="PATCH_FAILED",
            reason="patch_apply_failed"
        )
        return self._transition_to(PipelineState.PATCH_FAILED)
```

---

### Priority Tier 4: Evaluation Artifact Standardization (Optional)

**Timeline**: 1 week (optional, can defer to Phase 21)  
**Goal**: Improve debuggability and human review experience

#### 4.1 Standardized Artifact Collection

**Problem**: Evaluation artifacts scattered across directories, hard to review failures.

**Solution**: Centralized artifact collection with human-readable summary.

**New Module: `src/eval/artifact_collector.py`**

```python
from pathlib import Path
from typing import Dict, Any, Optional
import json
import shutil
from datetime import datetime

class EvalArtifacts(BaseModel):
    """Complete set of evaluation artifacts."""
    instance_id: str
    timestamp: str
    logs: Dict[str, Optional[str]]
    structured_outputs: Dict[str, Optional[Dict]]
    patch_info: Dict[str, Any]
    summary: str

def collect_eval_artifacts(workdir: Path) -> EvalArtifacts:
    """
    Collect all evaluation artifacts regardless of success/failure.
    
    Artifacts collected:
    - Logs: stdout, stderr, pytest output
    - Structured outputs: JSON, JUnit XML
    - Patch info: diff, application status
    - Summary: human-readable overview
    """
    
    # Read logs
    logs = {
        "stdout": _read_if_exists(workdir / "workspace" / "stdout.log"),
        "stderr": _read_if_exists(workdir / "workspace" / "stderr.log"),
        "pytest_raw": _read_if_exists(workdir / "workspace" / "pytest-stdout.log"),
        "entryscript": _read_if_exists(workdir / "workspace" / "entryscript.sh")
    }
    
    # Read structured outputs
    structured_outputs = {
        "pytest_cases": _read_json_if_exists(workdir / "workspace" / "pytest-cases.jsonl"),
        "pytest_junit": _read_xml_if_exists(workdir / "workspace" / "pytest-junit.xml"),
        "output": _read_json_if_exists(workdir / "workspace" / "output.json"),
        "parser_metadata": _read_json_if_exists(workdir / "workspace" / "parser_metadata.json")
    }
    
    # Read patch info
    patch_info = {
        "prediction": _read_json_if_exists(workdir / "outputs" / "prediction.json"),
        "patch_outcome": _read_json_if_exists(workdir / "outputs" / "patch_outcome.json"),
        "patch_applied": (workdir / "repo" / ".git" / "ORIG_HEAD").exists()
    }
    
    # Generate summary
    summary = _generate_artifact_summary(logs, structured_outputs, patch_info)
    
    return EvalArtifacts(
        instance_id=patch_info.get("prediction", {}).get("instance_id", "unknown"),
        timestamp=datetime.now().isoformat(),
        logs=logs,
        structured_outputs=structured_outputs,
        patch_info=patch_info,
        summary=summary
    )

def _read_if_exists(path: Path) -> Optional[str]:
    """Read text file if exists."""
    if path.exists():
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"<Error reading file: {e}>"
    return None

def _read_json_if_exists(path: Path) -> Optional[Dict]:
    """Read JSON file if exists."""
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            return {"_error": f"Failed to parse JSON: {e}"}
    return None

def _read_xml_if_exists(path: Path) -> Optional[Dict]:
    """Read XML file and convert to dict if exists."""
    if path.exists():
        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(path)
            root = tree.getroot()
            return {"_xml_root": root.tag, "_xml_attrib": root.attrib}
        except Exception as e:
            return {"_error": f"Failed to parse XML: {e}"}
    return None

def _generate_artifact_summary(
    logs: Dict,
    structured_outputs: Dict,
    patch_info: Dict
) -> str:
    """Generate human-readable summary for quick review."""
    
    lines = []
    lines.append("=" * 80)
    lines.append("EVALUATION ARTIFACT SUMMARY")
    lines.append("=" * 80)
    lines.append("")
    
    # Patch info
    lines.append("## Patch Information")
    if patch_info.get("prediction"):
        lines.append(f"Instance ID: {patch_info['prediction'].get('instance_id')}")
        patch_len = len(patch_info['prediction'].get('model_patch', ''))
        lines.append(f"Patch size: {patch_len} characters")
    
    if patch_info.get("patch_outcome"):
        outcome = patch_info['patch_outcome']
        lines.append(f"Patch outcome: {outcome.get('patch_outcome')}")
        lines.append(f"Eval outcome: {outcome.get('eval_outcome', 'N/A')}")
        
        if outcome.get('eval_errors'):
            lines.append(f"Eval errors: {len(outcome['eval_errors'])}")
            for err in outcome['eval_errors'][:3]:  # Show first 3
                lines.append(f"  - {err}")
    
    lines.append("")
    
    # Test results
    lines.append("## Test Results")
    if structured_outputs.get("output"):
        output = structured_outputs["output"]
        tests = output.get("tests", [])
        lines.append(f"Total tests: {len(tests)}")
        
        if tests:
            passed = sum(1 for t in tests if t.get("status") == "PASSED")
            failed = sum(1 for t in tests if t.get("status") == "FAILED")
            lines.append(f"  Passed: {passed}")
            lines.append(f"  Failed: {failed}")
        else:
            lines.append("  WARNING: Empty test list (possible infrastructure error)")
    else:
        lines.append("  No structured test output found")
    
    lines.append("")
    
    # Logs availability
    lines.append("## Available Artifacts")
    lines.append(f"stdout.log: {'?' if logs.get('stdout') else '?'}")
    lines.append(f"stderr.log: {'?' if logs.get('stderr') else '?'}")
    lines.append(f"pytest-cases.jsonl: {'?' if structured_outputs.get('pytest_cases') else '?'}")
    lines.append(f"pytest-junit.xml: {'?' if structured_outputs.get('pytest_junit') else '?'}")
    
    lines.append("")
    
    # Error indicators
    lines.append("## Error Indicators")
    error_found = False
    
    if logs.get("stderr"):
        stderr = logs["stderr"]
        if "Error" in stderr or "Exception" in stderr:
            lines.append("? stderr contains errors")
            error_found = True
    
    if logs.get("stdout"):
        stdout = logs["stdout"]
        infra_errors = [
            "INTERNALERROR",
            "ImportError",
            "ModuleNotFoundError",
            "collection errors"
        ]
        for err_pattern in infra_errors:
            if err_pattern in stdout:
                lines.append(f"? stdout contains: {err_pattern}")
                error_found = True
    
    if not error_found:
        lines.append("No obvious error patterns detected")
    
    lines.append("")
    lines.append("=" * 80)
    
    return "\n".join(lines)

def save_artifacts_bundle(workdir: Path, artifacts: EvalArtifacts):
    """Save all artifacts in a structured bundle for easy review."""
    
    bundle_dir = workdir / "eval_artifacts_bundle"
    bundle_dir.mkdir(exist_ok=True)
    
    # Save summary as README
    (bundle_dir / "README.txt").write_text(artifacts.summary)
    
    # Save logs
    logs_dir = bundle_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    for name, content in artifacts.logs.items():
        if content:
            (logs_dir / f"{name}.log").write_text(content)
    
    # Save structured outputs
    structured_dir = bundle_dir / "structured"
    structured_dir.mkdir(exist_ok=True)
    for name, content in artifacts.structured_outputs.items():
        if content:
            (structured_dir / f"{name}.json").write_text(
                json.dumps(content, indent=2)
            )
    
    # Save patch info
    patch_dir = bundle_dir / "patch"
    patch_dir.mkdir(exist_ok=True)
    for name, content in artifacts.patch_info.items():
        if isinstance(content, dict):
            (patch_dir / f"{name}.json").write_text(
                json.dumps(content, indent=2)
            )
        elif isinstance(content, bool):
            (patch_dir / f"{name}.txt").write_text(str(content))
    
    logger.info(f"Artifacts bundle saved to: {bundle_dir}")
```

---

#### 4.2 Two-Stage Evaluation Gate

**Problem**: Patch application failures and test infrastructure failures are mixed together.

**Solution**: Split evaluation into two gates.

**New Module: `src/eval/two_stage_eval.py`**

```python
from pathlib import Path
from typing import Tuple

class Gate1Result(BaseModel):
    """First gate: basic applicability check."""
    passed: bool
    patch_applied: bool
    tests_discoverable: bool
    pytest_runnable: bool
    errors: List[str]

class Gate2Result(BaseModel):
    """Second gate: full evaluation."""
    outcome: EvalOutcome
    tests: List[TestResult]
    fail_to_pass_passed: int
    fail_to_pass_total: int

async def run_two_stage_evaluation(workdir: Path) -> Tuple[Gate1Result, Optional[Gate2Result]]:
    """
    Two-stage evaluation with early failure detection.
    
    Gate 1: Basic applicability (fast, ~30s)
    - Can patch be applied?
    - Are target tests discoverable?
    - Can pytest run at all?
    
    Gate 2: Full evaluation (slow, ~5min)
    - Run all FAIL_TO_PASS tests
    - Run all PASS_TO_PASS tests
    - Generate final verdict
    """
    
    # Gate 1: Basic applicability
    gate1 = await _run_gate1(workdir)
    
    if not gate1.passed:
        logger.warning(f"Gate 1 failed: {gate1.errors}")
        return gate1, None
    
    logger.info("Gate 1 passed, proceeding to full evaluation")
    
    # Gate 2: Full evaluation
    gate2 = await _run_gate2(workdir)
    
    return gate1, gate2

async def _run_gate1(workdir: Path) -> Gate1Result:
    """
    Gate 1: Quick applicability check.
    
    Steps:
    1. Apply patch
    2. Discover target tests (pytest --collect-only)
    3. Run one smoke test
    """
    errors = []
    
    # Step 1: Apply patch
    patch_applied = False
    try:
        prediction_path = workdir / "outputs" / "prediction.json"
        with open(prediction_path) as f:
            prediction = json.load(f)
        
        patch_diff = prediction["model_patch"]
        
        # Apply patch to repo
        repo_path = workdir / "repo"
        result = subprocess.run(
            ["git", "apply", "--check"],
            input=patch_diff,
            text=True,
            capture_output=True,
            cwd=repo_path
        )
        
        if result.returncode == 0:
            # Actually apply
            subprocess.run(
                ["git", "apply"],
                input=patch_diff,
                text=True,
                cwd=repo_path,
                check=True
            )
            patch_applied = True
        else:
            errors.append(f"Patch failed to apply: {result.stderr}")
    
    except Exception as e:
        errors.append(f"Patch application error: {e}")
    
    if not patch_applied:
        return Gate1Result(
            passed=False,
            patch_applied=False,
            tests_discoverable=False,
            pytest_runnable=False,
            errors=errors
        )
    
    # Step 2: Discover tests
    tests_discoverable = False
    try:
        metadata_path = workdir / "artifacts" / "instance_metadata.json"
        with open(metadata_path) as f:
            metadata = json.load(f)
        
        fail_to_pass = metadata["FAIL_TO_PASS"]
        
        # Try to collect tests
        result = subprocess.run(
            ["pytest", "--collect-only", "-q"] + fail_to_pass,
            capture_output=True,
            text=True,
            cwd=repo_path,
            timeout=30
        )
        
        if result.returncode == 0 or "collected" in result.stdout:
            tests_discoverable = True
        else:
            errors.append(f"Test discovery failed: {result.stderr}")
    
    except Exception as e:
        errors.append(f"Test discovery error: {e}")
    
    if not tests_discoverable:
        return Gate1Result(
            passed=False,
            patch_applied=True,
            tests_discoverable=False,
            pytest_runnable=False,
            errors=errors
        )
    
    # Step 3: Run smoke test (first FAIL_TO_PASS test only)
    pytest_runnable = False
    try:
        first_test = fail_to_pass[0] if fail_to_pass else None
        
        if first_test:
            result = subprocess.run(
                ["pytest", "-xvs", first_test],
                capture_output=True,
                text=True,
                cwd=repo_path,
                timeout=60
            )
            
            # Even if test fails, as long as pytest runs, we're good
            if "PASSED" in result.stdout or "FAILED" in result.stdout:
                pytest_runnable = True
            else:
                errors.append(f"Pytest smoke test failed: {result.stderr}")
    
    except Exception as e:
        errors.append(f"Pytest smoke test error: {e}")
    
    passed = patch_applied and tests_discoverable and pytest_runnable
    
    return Gate1Result(
        passed=passed,
        patch_applied=patch_applied,
        tests_discoverable=tests_discoverable,
        pytest_runnable=pytest_runnable,
        errors=errors
    )

async def _run_gate2(workdir: Path) -> Gate2Result:
    """
    Gate 2: Full evaluation.
    
    Run complete test suite and generate verdict.
    """
    # Use existing evaluation logic
    from src.eval.swe_bench_pro_eval import run_evaluation
    
    return await run_evaluation(workdir)
```

---

## Implementation Roadmap

### Phase 20.1: Evidence Quality (Weeks 1-2)

**Week 1: Parser & Deep Search Enhancements**
- [ ] Implement `TaskType` enum and task classification logic
- [ ] Add task type patterns to Parser agent prompt
- [ ] Implement `SignatureCompatibility` checker
- [ ] Update Deep Search agent prompt for API compatibility
- [ ] Add `task_type` and `task_intent` fields to `EvidenceCards`
- [ ] Test on issue002 to verify correct classification

**Week 2: Guards & Validation**
- [ ] Implement I4 structural invariant (task-scope consistency)
- [ ] Integrate I4 into orchestrator guard sequence
- [ ] Add parser field whitelist enforcement for task types
- [ ] Regression test on issue001 and issue002
- [ ] Document new evidence quality requirements

**Success Criteria:**
- Issue002 correctly classified as `TEST_MIGRATION`
- `req-007` marked as `AS_IS_COMPLIANT` (not `TO_BE_PARTIAL`)
- I4 guard catches task-scope violations

---

### Phase 20.2: Evaluation Infrastructure (Week 3)

**Tasks:**
- [ ] Implement `schema_validator.py` with input/output validation
- [ ] Add `EvalOutcome` enum with infrastructure error types
- [ ] Modify `run_script.sh` to generate structured outputs
- [ ] Implement structured-first parser (JSONL ¡ú JUnit ¡ú stdout fallback)
- [ ] Update `patch_outcome.json` schema with eval outcome fields
- [ ] Add validation gates to evaluation pipeline

**Success Criteria:**
- Empty test results classified as `INFRA_ERROR` (not `UNRESOLVED`)
- Evaluation uses structured output (not stdout parsing)
- Clear distinction between test failures and infrastructure failures

---

### Phase 20.3: Patch Quality Gates (Week 4)

**Tasks:**
- [ ] Implement `patch_scope_validator.py`
- [ ] Implement `patch_reviewer.py` with all checks
- [ ] Integrate scope validator before patch generation
- [ ] Integrate patch reviewer after patch generation
- [ ] Add rework feedback mechanism for failed reviews
- [ ] Test on issue002 to verify over-modification detection

**Success Criteria:**
- Issue002-style patches rejected at scope validation stage
- Noise modifications (copyright, whitelist) flagged
- Test file rewrites detected and warned

---

### Phase 20.4: Artifact Standardization (Week 5, Optional)

**Tasks:**
- [ ] Implement `artifact_collector.py`
- [ ] Implement two-stage evaluation gates
- [ ] Add artifact bundle generation
- [ ] Create human-readable summary format
- [ ] Integrate into evaluation pipeline

**Success Criteria:**
- All evaluations produce standardized artifact bundles
- Gate 1 failures detected in <1 minute
- Human-readable summaries aid debugging

---

## Validation Plan

### Regression Testing

**Test on existing issues:**
1. **Issue001**: Should still pass (no regression)
2. **Issue002**: Should now generate correct patch (test migration only)
3. **Issue003**: Should still pass (no regression)

### New Test Cases

**Create synthetic test cases:**
1. **Test-migration-001**: Pure test file move (like issue002)
2. **Feature-impl-001**: New interface implementation
3. **Bug-fix-001**: Fix existing broken code
4. **Over-modification-001**: Intentionally over-modified patch (should be rejected)

### Metrics

**Track improvements:**
- Evidence quality: % of issues with correct task type classification
- Patch quality: % of patches with scope violations
- Evaluation reliability: % of evaluations with valid structured output
- False positive rate: % of good patches incorrectly rejected

---

## Risk Mitigation

### Risk 1: Over-Strict Validation

**Risk**: New guards reject too many valid patches.

**Mitigation**:
- Start with WARNING severity, upgrade to BLOCKING after validation
- Collect metrics on rejection rate
- Manual review of first 10 rejections
- Escape hatch: `--skip-patch-review` flag for emergency

### Risk 2: Performance Impact

**Risk**: Additional validation slows down pipeline.

**Mitigation**:
- Scope validation is O(n) where n = number of files (fast)
- Patch review is O(m) where m = patch size (acceptable)
- Two-stage eval only adds ~30s for Gate 1
- Total overhead: <1 minute per issue

### Risk 3: False Negatives

**Risk**: Subtle over-modifications not caught by reviewer.

**Mitigation**:
- Continuous improvement of detection patterns
- Collect feedback from manual reviews
- Add new checks based on discovered issues
- Phase 21: ML-based patch quality scoring

---

## Success Metrics

### Phase 20.1 (Evidence Quality)
- ? Issue002 task type correctly identified as `TEST_MIGRATION`
- ? API compatibility checker prevents false `TO_BE_PARTIAL` verdicts
- ? I4 guard catches ¡Ý90% of task-scope violations

### Phase 20.2 (Eval Infrastructure)
- ? ¡Ý95% of evaluations produce structured output
- ? Infrastructure errors correctly classified (not mixed with test failures)
- ? Zero silent failures (empty test lists always flagged)

### Phase 20.3 (Patch Quality)
- ? Issue002-style over-modifications caught before application
- ? <5% false positive rate (good patches rejected)
- ? Noise modifications flagged in ¡Ý80% of cases

### Phase 20.4 (Artifacts)
- ? 100% of evaluations produce artifact bundles
- ? Human review time reduced by 50% (due to summaries)
- ? Gate 1 failures detected in <1 minute

---

## Future Enhancements (Phase 21+)

1. **ML-based Patch Quality Scoring**: Train model on good/bad patches
2. **Automated Patch Repair**: When review fails, suggest fixes
3. **Evidence Quality Scoring**: Quantify evidence completeness
4. **Interactive Patch Review**: Allow user to approve/reject with feedback
5. **Patch Minimization**: Automatically remove unnecessary changes
6. **Cross-Issue Learning**: Learn from past mistakes across issues

---

## Appendix: Issue002 Detailed Analysis

### What Went Wrong

**Evidence Generation:**
- Parser misunderstood "New interfaces introduced" as "create these" instead of "verify these exist"
- Deep Search marked existing implementation as `TO_BE_PARTIAL` due to signature style difference
- Closure Checker approved incorrect evidence

**Patch Generation:**
- Patch generator faithfully followed incorrect evidence
- Re-implemented existing functionality
- Created new tests instead of migrating existing ones
- Added unnecessary re-exports and noise modifications

### What Should Have Happened

**Correct Evidence:**
```json
{
  "task_type": "TEST_MIGRATION",
  "task_intent": "Move TestHideQtWarning class from test_log.py to test_qtlog.py",
  "requirements": [
    {
      "id": "req-001",
      "text": "TestHideQtWarning class should be in test_qtlog.py",
      "verdict": "TO_BE_MISSING",
      "evidence_locations": ["tests/unit/utils/test_qtlog.py:50-100"]
    },
    {
      "id": "req-002",
      "text": "TestHideQtWarning class should be removed from test_log.py",
      "verdict": "AS_IS_VIOLATED",
      "evidence_locations": ["tests/unit/utils/test_log.py:343-369"]
    }
  ]
}
```

**Correct Patch:**
```diff
diff --git a/tests/unit/utils/test_log.py b/tests/unit/utils/test_log.py
--- a/tests/unit/utils/test_log.py
+++ b/tests/unit/utils/test_log.py
@@ -340,29 +340,0 @@
-class TestHideQtWarning:
-    # ... (delete entire class)

diff --git a/tests/unit/utils/test_qtlog.py b/tests/unit/utils/test_qtlog.py
--- a/tests/unit/utils/test_qtlog.py
+++ b/tests/unit/utils/test_qtlog.py
@@ -50,0 +51,29 @@
+class TestHideQtWarning:
+    # ... (add entire class, change log.hide_qt_warning ¡ú qtlog.hide_qt_warning)
```

**Key Differences:**
- Only test files modified (no implementation changes)
- Simple move operation (not rewrite)
- No API signature changes
- No noise modifications

This is exactly what Phase 20 improvements will prevent and correct.

---

## Conclusion

Phase 20 addresses the root causes of issue002 through a multi-layered defense system:

1. **Evidence Quality** (Tier 1): Prevent misunderstanding at the source
2. **Evaluation Infrastructure** (Tier 2): Distinguish real failures from infrastructure issues
3. **Patch Quality Gates** (Tier 3): Catch over-modifications before they cause harm
4. **Artifact Standardization** (Tier 4): Improve debuggability and learning

By implementing these improvements, we expect to:
- Reduce patch over-modification rate by 80%
- Eliminate silent evaluation failures
- Improve evidence accuracy by 50%
- Reduce manual review time by 50%

The phased approach allows us to validate each layer independently and adjust based on real-world results.

