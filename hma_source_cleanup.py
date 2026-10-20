"""Preview or apply HMA source-layout cleanup. No live Agent/model invocation."""
from __future__ import annotations
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

EXPECTED = {'memory_model_3d.py': '66c46a7ffbcff1bcbaf09bb4393485197b0c4fe9499c4d978cd3f024407873fa', 'preflight_memory_3d.py': 'dbdb8c936fd81b8bde8e64a827b2e200974da7c078886f7969e5460131e0925e', 'patch_memory_note_facts.py': '68fef0cd11f0e7f33fb43df87f0968375f755ce3ab007fd4362e9c2ffd9608da', 'tests/test_memory_model_harness.py': 'e9c5b3104b6c5e6ca94145e4c2daaaa6f4c9770842067a20b2eda83bd462c94f', 'tests/test_memory_note_facts.py': '7c152d5d0fdfe4358645d7bf2512547aa841c785d5110233924be2b079ca66b5', 'run_tests_4c3.py': '2a68410ee0e7b9a0c180a782aea248c1c0b565b89a74917d1c01985b3713c345'}
RUNNER = '"""Run the 28 approved offline regression groups carried over from 4C3."""\nfrom __future__ import annotations\nimport argparse\nimport hashlib\nimport json\nimport os\nfrom pathlib import Path\nimport sys\nimport unittest\nfrom datetime import datetime, timezone\n\nGROUPS = ((\'test_action_matrix_profiles.py\', 20), (\'test_action_matrix_runner.py\', 5), (\'test_handoff_profiles.py\', 24), (\'test_handoff_runner.py\', 5), (\'test_profile_policy.py\', 10), (\'test_ownership_profiles.py\', 20), (\'test_runtime_binding.py\', 14), (\'test_runner_binding_metadata.py\', 11), (\'test_runner_profile_main.py\', 16), (\'test_stage4.py\', 20), (\'test_stage7a.py\', 16), (\'test_runtime_lock.py\', 10), (\'test_profile_selector.py\', 26), (\'test_profile_cli.py\', 11), (\'test_memory_note_facts.py\', 8), (\'test_memory_model_harness.py\', 21), (\'test_memory_episode.py\', 9), (\'test_memory_prompt.py\', 18), (\'test_prompt_rules.py\', 4), (\'test_memory_tools.py\', 34), (\'test_memory_runner.py\', 6), (\'test_memory_store.py\', 28), (\'test_memory_schema.py\', 14), (\'test_memory_integration.py\', 9), (\'test_stage0.py\', 35), (\'test_stage2b.py\', 24), (\'test_stage2a.py\', 36), (\'test_stage3_loop.py\', 16))\n\ndef source_hashes(root):\n    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()\n            for p in sorted((root / "hma").rglob("*.py"))}\n\ndef main(argv=None):\n    parser = argparse.ArgumentParser(description=__doc__)\n    parser.add_argument("--report", type=Path)\n    args = parser.parse_args(argv)\n    root = Path(__file__).resolve().parents[1]\n    os.chdir(root)\n    before = source_hashes(root)\n    details = []\n    successful = True\n    for pattern, expected in GROUPS:\n        suite = unittest.TestLoader().discover("tests", pattern=pattern)\n        count = suite.countTestCases()\n        print(f"\\n=== {pattern}: {count}; expected {expected} ===", flush=True)\n        result = unittest.TextTestRunner(verbosity=1).run(suite)\n        ok = count == expected and result.testsRun == expected and result.wasSuccessful()\n        details.append({"pattern": pattern, "expected": expected, "discovered": count,\n                        "tests_run": result.testsRun, "failures": len(result.failures),\n                        "errors": len(result.errors), "skipped": len(result.skipped), "ok": ok})\n        successful = successful and ok\n    after = source_hashes(root)\n    unchanged = before == after\n    included = {pattern for pattern, _ in GROUPS}\n    other = sorted(p.name for p in (root / "tests").glob("test_*.py") if p.name not in included)\n    doc = {"suite": "approved_4c3_regression", "groups": details,\n           "expected_tests": sum(n for _, n in GROUPS),\n           "tests_run": sum(g["tests_run"] for g in details),\n           "runtime_source_unchanged": unchanged,\n           "other_test_files_not_in_this_suite": other,\n           "all_passed": successful and unchanged,\n           "verified_at_utc": datetime.now(timezone.utc).isoformat()}\n    if args.report:\n        args.report.parent.mkdir(parents=True, exist_ok=True)\n        args.report.write_text(json.dumps(doc, ensure_ascii=False, indent=2)+"\\n", encoding="utf-8")\n    print("\\n" + json.dumps(doc, ensure_ascii=False, indent=2), flush=True)\n    return 0 if doc["all_passed"] else 1\n\nif __name__ == "__main__":\n    sys.exit(main())\n'

def digest(data):
    return hashlib.sha256(data).hexdigest()

def runtime_hashes(root):
    return {str(p.relative_to(root)): digest(p.read_bytes())
            for p in sorted((root / "hma").rglob("*.py"))}

def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError("Expected exactly one import: " + old)
    return source.replace(old, new, 1)

def helper_only(source):
    tree = ast.parse(source)
    remove = set()
    for node in tree.body:
        drop = isinstance(node, ast.FunctionDef) and node.name == "main"
        drop = drop or isinstance(node, ast.If) and (
            ast.dump(node.test) == ast.dump(ast.parse("__name__ == '__main__'", mode="eval").body)
        )
        if isinstance(node, ast.Import):
            drop = drop or any(a.name == "argparse" for a in node.names)
        if drop:
            remove.update(range(node.lineno, node.end_lineno + 1))
    result = "\n".join(line for i, line in enumerate(source.splitlines(), 1)
                       if i not in remove) + "\n"
    node = ast.parse(result).body[0]
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
        lines = result.splitlines(keepends=True)
        lines[node.lineno-1:node.end_lineno] = [
            '\"\"\"Helpers for note-facts patch/backup regression tests; no CLI entrypoint.\"\"\"\n'
        ]
        result = "".join(lines)
    return result

def check_service():
    def prop(unit, name):
        return subprocess.check_output(
            ["systemctl", "show", unit, "-p", name, "--value"], text=True
        ).strip()
    if prop("hma.timer", "ActiveState") != "inactive":
        raise RuntimeError("Stop hma.timer before applying cleanup.")
    if prop("hma.service", "MainPID") != "0" or prop("hma.service", "ActiveState") not in ("inactive", "failed"):
        raise RuntimeError("hma.service must not be running.")

def build_plan(root):
    original = {}
    for name, expected in EXPECTED.items():
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing or symlink source: " + name)
        source = path.read_text(encoding="utf-8")
        if digest(source.strip().encode()) != expected:
            raise ValueError("Source differs from the reviewed upload: " + name)
        ast.parse(source, filename=name)
        original[name] = source
    changes = {
        "tools/__init__.py": '"""Operator and regression utilities; not Agent runtime."""\n',
        "tools/memory_preflight.py": original["preflight_memory_3d.py"],
        "tools/memory_harness.py": replace_once(original["memory_model_3d.py"],
            "from preflight_memory_3d import build_plan", "from tools.memory_preflight import build_plan"),
        "tests/support/__init__.py": '"""Helpers for regression tests."""\n',
        "tests/support/note_facts_patch.py": helper_only(original["patch_memory_note_facts.py"]),
        "tests/test_memory_model_harness.py": replace_once(original["tests/test_memory_model_harness.py"],
            "import memory_model_3d as H", "from tools import memory_harness as H"),
        "tests/test_memory_note_facts.py": replace_once(original["tests/test_memory_note_facts.py"],
            "import patch_memory_note_facts as P", "from tests.support import note_facts_patch as P"),
        "tools/run_tests.py": RUNNER,
    }
    edited = {"tests/test_memory_model_harness.py", "tests/test_memory_note_facts.py"}
    for name, source in changes.items():
        path = root / name
        if path.is_symlink() or (name not in edited and path.exists()):
            raise ValueError("Refuse to overwrite destination: " + name)
        ast.parse(source, filename=name)
    historical = set(root.glob("patch_*.py")) | set(root.glob("run_tests_*.py")) | set(root.glob("*_patch.json"))
    historical.update(root / name for name in ("memory_3d_plan.json", "memory_model_3d.py", "preflight_memory_3d.py"))
    historical = sorted(p for p in historical if p.is_file())
    for path in historical:
        if path.is_symlink():
            raise ValueError("Refuse symlink history file: " + str(path))
    return changes, edited, historical

def verify_history(root):
    import importlib.util
    path = root / "run_tests_4c3.py"
    spec = importlib.util.spec_from_file_location("hma_legacy_4c3_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def load(name):
        return json.loads((root / name).read_text(encoding="utf-8"))
    module.verify(root, load("matrix_4c3_patch.json"), load("handoff_4c2_patch.json"),
                  load("ownership_4c1_patch.json"), load("profile_selector_4a_patch.json"),
                  load("memory_note_facts_patch.json"))

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    changes, edited, historical = build_plan(root)
    preview = {
        "mode": "apply" if args.apply else "preview_only",
        "root": str(root),
        "new_files": sorted(set(changes)-edited),
        "edited_tests": sorted(edited),
        "archive_before_regression": [str(p.relative_to(root)) for p in historical],
        "runtime_files_changed": [],
        "approved_regression_groups": 28,
        "approved_regression_tests": 470,
        "other_test_files_are_not_deleted": True,
        "live_agent_started": False,
    }
    print(json.dumps(preview, ensure_ascii=False, indent=2), flush=True)
    if not args.apply:
        return 0
    check_service()
    sys.path.insert(0, str(root))
    from hma.runtime_lock import RunLock
    archive = root.parent / (root.name + "-source-history") / (
        "cleanup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    before = runtime_hashes(root)
    audit = dict(preview, state="started", history_directory=str(archive), runtime_before_sha256=before,
                 before_files={}, retired_files=[])
    def save():
        temp = archive / "cleanup-manifest.json.tmp"
        temp.write_text(json.dumps(audit, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        temp.replace(archive / "cleanup-manifest.json")
    with RunLock(root / "data" / "run.lock"):
        check_service()
        build_plan(root)
        verify_history(root)
        audit["legacy_hash_chain_verified_before_cleanup"] = True
        archive.mkdir(parents=True, mode=0o700, exist_ok=False)
        save()
        try:
            for path in historical + [root / name for name in sorted(edited)]:
                relative = path.relative_to(root)
                target = archive / "before" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                sha = digest(path.read_bytes())
                if digest(target.read_bytes()) != sha:
                    raise RuntimeError("Backup mismatch: " + str(relative))
                audit["before_files"][str(relative)] = sha
            audit["state"] = "backed_up"
            save()
            for name, source in changes.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source, encoding="utf-8")
            if runtime_hashes(root) != before:
                raise RuntimeError("Runtime source changed unexpectedly.")
            for path in historical:
                relative = str(path.relative_to(root))
                if digest(path.read_bytes()) != audit["before_files"][relative]:
                    raise RuntimeError("History file changed before retirement: " + relative)
            retired = archive / "retired"
            retired.mkdir()
            for path in historical:
                shutil.move(str(path), str(retired / path.name))
                audit["retired_files"].append(path.name)
                save()
            audit["state"] = "running_regression_after_retirement"
            save()
            report = archive / "regression.json"
            console = archive / "regression-console.txt"
            with console.open("w", encoding="utf-8") as handle:
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "tools.run_tests", "--report", str(report)],
                    cwd=root, stdout=handle, stderr=subprocess.STDOUT,
                )
            summary = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
            audit["regression"] = summary
            audit["runtime_after_sha256"] = runtime_hashes(root)
            if result.returncode or not summary.get("all_passed") or audit["runtime_after_sha256"] != before:
                audit["state"] = "regression_failed_backups_preserved"
                save()
                print(json.dumps({"state": audit["state"], "history_directory": str(archive),
                                  "console": str(console), "regression": summary}, ensure_ascii=False, indent=2))
                return 1
            audit["state"] = "source_layout_cleaned"
            save()
        except BaseException:
            audit["state"] = "interrupted_or_failed_do_not_rerun_blindly"
            save()
            raise
    print(json.dumps({"state": audit["state"], "history_directory": str(archive),
                      "runtime_source_unchanged": runtime_hashes(root) == before,
                      "regression_tests_run": summary.get("tests_run"),
                      "all_approved_tests_passed": summary.get("all_passed"),
                      "other_test_files_not_in_this_suite": summary.get("other_test_files_not_in_this_suite"),
                      "retired_files": len(audit["retired_files"]),
                      "agent_database_or_mailbox_cleaned": False}, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
