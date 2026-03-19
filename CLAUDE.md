# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Evidence-Closure-Aware Software Engineering Repair Agent - A Python project that uses the Claude Agent SDK to automatically fix software issues by systematically gathering and validating four types of evidence before generating patches:
- **Symptom Evidence**: What's broken and what the expected behavior should be
- **Localization Evidence**: Where the fix should be applied
- **Constraint Evidence**: What makes a fix correct and what must not be broken
- **Structural Evidence**: Dependencies and co-edit requirements

## Running the Project

```bash
# Run Phase 1 (Artifact Parsing) only
python main.py <instance_id> --phase1-only

# Run full repair workflow (requires --problem)
python main.py <instance_id> --problem "problem description"

# Example
python main.py face_recognition_issue_001 --phase1-only
```

## Installing Dependencies

```bash
pip install -r requirements.txt
```

## Architecture

The project follows a 6-phase workflow:

1. **Phase 1 (Artifact Parsing)**: Parses input artifacts (`problem_statement.md`, `requirements.md`, `interface.md`) and generates initial evidence cards
2. **Phase 2 (Evidence Extraction)**: Deep codebase analysis to enrich evidence cards
3. **Phase 3 (Closure Checking)**: Validates evidence sufficiency before proceeding
4. **Phase 4 (Patch Planning)**: Creates detailed patch plans
5. **Phase 5 (Patch Generation)**: Implements the patches
6. **Phase 6 (Validation/Replan)**: Validates and replans if needed

## Key Files

- `main.py` - CLI entry point with argument parsing
- `src/orchestrator.py` - Main workflow controller and agent definitions
- `src/evidence_cards.py` - Pydantic models for the four evidence card types
- `src/artifact_parsers.py` - Parsers for problem statements, requirements, and interfaces
- `src/evidence_extractors.py` - Phase 2 extractors for deep codebase analysis
- `src/agents/artifact_parser_agent.py` - Phase 1 agent implementation

## Workspace Structure

Each SWE-Bench instance has an isolated workspace under `workdir/{instance_id}/`:
```
workdir/{instance_id}/
├── repo/              # Cloned target repository
├── artifacts/         # Input: problem_statement.md, requirements.md, new_interfaces.md, expected_and current_behavior.md
├── evidence/          # Output: evidence cards (JSON)
└── evidence/card_versions/  # Version history of cards
```

## Dependencies

- `claude-agent-sdk>=0.1.0` - Claude Agent SDK for agent orchestration
- `pydantic>=2.0.0` - Data validation and models
- `aiohttp>=3.9.0` - Async HTTP support
- `datasets>=2.18.0` - SWE-Bench data loading
- `pytest>=8.0.0` - Testing framework
- `orjson>=3.9.0` - Fast JSON handling

## Agent Definitions

The system defines specialized sub-agents (configured in `src/orchestrator.py`):
- `artifact-parser` - Parses input artifacts and generates initial evidence cards
- `symptom-extractor` - Analyzes symptoms from problem statements
- `localization-extractor` - Finds candidate edit locations in the repository
- `constraint-extractor` - Extracts constraints from requirements and interfaces
- `structural-extractor` - Analyzes code dependencies and structural relationships
- `closure-checker` - Evaluates evidence sufficiency before patch planning
- `patch-planner` - Creates detailed patch plans based on evidence
- `patch-executor` - Implements patches according to the plan


注意,你在bash环境运行的测试脚本，所以删除临时文件时不要用del等windows命令
用中文回答用户

