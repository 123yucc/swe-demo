from pathlib import Path

from src.orchestrator.consistency_checks import check_removed_symbol_test_refs

errs = check_removed_symbol_test_refs(Path("workdir/swe_issue_024/repo"))
print("removed_symbol_errors", len(errs))
for err in errs:
    print(err.message)
