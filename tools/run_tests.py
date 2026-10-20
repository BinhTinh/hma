"""Run the 28 approved offline regression groups carried over from 4C3."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from datetime import datetime, timezone

GROUPS = (('test_action_matrix_profiles.py', 20), ('test_action_matrix_runner.py', 5), ('test_handoff_profiles.py', 24), ('test_handoff_runner.py', 5), ('test_profile_policy.py', 10), ('test_ownership_profiles.py', 20), ('test_runtime_binding.py', 14), ('test_runner_binding_metadata.py', 11), ('test_runner_profile_main.py', 16), ('test_stage4.py', 20), ('test_stage7a.py', 16), ('test_runtime_lock.py', 10), ('test_profile_selector.py', 26), ('test_profile_cli.py', 11), ('test_memory_note_facts.py', 8), ('test_memory_model_harness.py', 21), ('test_memory_episode.py', 9), ('test_memory_prompt.py', 18), ('test_prompt_rules.py', 4), ('test_memory_tools.py', 34), ('test_memory_runner.py', 6), ('test_memory_store.py', 28), ('test_memory_schema.py', 14), ('test_memory_integration.py', 9), ('test_stage0.py', 35), ('test_stage2b.py', 24), ('test_stage2a.py', 36), ('test_stage3_loop.py', 16))

def source_hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / "hma").rglob("*.py"))}

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    before = source_hashes(root)
    details = []
    successful = True
    for pattern, expected in GROUPS:
        suite = unittest.TestLoader().discover("tests", pattern=pattern)
        count = suite.countTestCases()
        print(f"\n=== {pattern}: {count}; expected {expected} ===", flush=True)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        ok = count == expected and result.testsRun == expected and result.wasSuccessful()
        details.append({"pattern": pattern, "expected": expected, "discovered": count,
                        "tests_run": result.testsRun, "failures": len(result.failures),
                        "errors": len(result.errors), "skipped": len(result.skipped), "ok": ok})
        successful = successful and ok
    after = source_hashes(root)
    unchanged = before == after
    included = {pattern for pattern, _ in GROUPS}
    other = sorted(p.name for p in (root / "tests").glob("test_*.py") if p.name not in included)
    doc = {"suite": "approved_4c3_regression", "groups": details,
           "expected_tests": sum(n for _, n in GROUPS),
           "tests_run": sum(g["tests_run"] for g in details),
           "runtime_source_unchanged": unchanged,
           "other_test_files_not_in_this_suite": other,
           "all_passed": successful and unchanged,
           "verified_at_utc": datetime.now(timezone.utc).isoformat()}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(doc, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print("\n" + json.dumps(doc, ensure_ascii=False, indent=2), flush=True)
    return 0 if doc["all_passed"] else 1

if __name__ == "__main__":
    sys.exit(main())
