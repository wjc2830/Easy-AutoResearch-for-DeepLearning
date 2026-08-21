import sys

# Keep direct, discovery, and CI test invocations from modifying the release tree.
sys.dont_write_bytecode = True

import copy
import fcntl
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import re
import signal
import shutil
import stat
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _is_current_test_loader_cache(path):
    relative = path.relative_to(ROOT)
    if relative.parts[:2] != ("tests", "__pycache__"):
        return False
    if path.is_file():
        return path.suffix == ".pyc" and path.name.startswith("test_release.")
    children = list(path.iterdir())
    return bool(children) and all(_is_current_test_loader_cache(child) for child in children)


PREEXISTING_BYTECODE_ARTIFACTS = tuple(
    str(path.relative_to(ROOT))
    for path in ROOT.rglob("*")
    if (path.name == "__pycache__" or path.suffix == ".pyc")
    and not _is_current_test_loader_cache(path)
)


def _remove_bytecode_caches():
    for name in ("__pycache__", ".pytest_cache", ".ruff_cache"):
        for path in sorted(ROOT.rglob(name), key=lambda p: len(p.parts), reverse=True):
            shutil.rmtree(path)
    for path in ROOT.rglob("*.pyc"):
        path.unlink()


_TEST_PROJECT_ROOT = None
_ORIGINAL_HARNESS_DIR = None
_ORIGINAL_INIT_DIR = None


def setUpModule():
    global _TEST_PROJECT_ROOT, _ORIGINAL_HARNESS_DIR, _ORIGINAL_INIT_DIR
    _remove_bytecode_caches()
    _TEST_PROJECT_ROOT = tempfile.TemporaryDirectory()
    _ORIGINAL_HARNESS_DIR = harness.ORCHESTRATOR_DIR
    _ORIGINAL_INIT_DIR = init.SCRIPT_DIR
    harness.ORCHESTRATOR_DIR = _TEST_PROJECT_ROOT.name
    init.SCRIPT_DIR = _TEST_PROJECT_ROOT.name


def tearDownModule():
    global _TEST_PROJECT_ROOT
    harness.ORCHESTRATOR_DIR = _ORIGINAL_HARNESS_DIR
    init.SCRIPT_DIR = _ORIGINAL_INIT_DIR
    if _TEST_PROJECT_ROOT is not None:
        _TEST_PROJECT_ROOT.cleanup()
        _TEST_PROJECT_ROOT = None
    _remove_bytecode_caches()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = load("harness", ROOT / "easy-auto-research/scripts/harness.py")
init = load("init_script", ROOT / "easy-auto-research/scripts/init.py")
watch = load("watch_session", ROOT / "easy-auto-research/scripts/watch_session.py")
checkpoint = load(
    "inspect_checkpoint",
    ROOT / "easy-auto-research/scripts/Skills/inspect-checkpoint/scripts/inspect_checkpoint.py",
)


def _child_claim_result(connection, path, marker=None):
    claim = harness._claim_message_file(path, marker)
    connection.send(None if claim is None else (claim.text, claim.token))
    connection.close()


def _child_finish_inherited_claim(connection, claim):
    connection.send((harness._restore_message_claim(claim), harness._ack_message_claim(claim)))
    connection.close()


def _child_claim_then_crash(connection, path, marker=None):
    claim = harness._claim_message_file(path, marker)
    connection.send(None if claim is None else (claim.text, claim.token))
    connection.close()
    os._exit(0)


def _child_ack_then_crash(connection, path):
    claim = harness._claim_message_file(path)
    connection.send((claim.text, harness._ack_message_claim(claim)))
    connection.close()
    os._exit(0)


def _child_unlocked_append(connection, path, payload):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        connection.send("opened")
        if connection.recv() != "append":
            os._exit(2)
        os.write(fd, payload)
        os.fsync(fd)
        connection.send("appended")
    finally:
        os.close(fd)
        connection.close()


def _child_locked_split_append(connection, path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, b"first-half")
        connection.send("half-written")
        if connection.recv() != "finish":
            os._exit(2)
        os.write(fd, b"-second-half\n")
        os.fsync(fd)
        fcntl.flock(fd, fcntl.LOCK_UN)
        connection.send("complete")
    finally:
        os.close(fd)
        connection.close()


class VersionTests(unittest.TestCase):
    def test_version_parser_rejects_unsafe_names(self):
        good = "## Version\n- New: V2_trial\n- From: V1_baseline"
        self.assertEqual(harness.parse_version_directive(good), ("V2_trial", "V1_baseline"))
        for value in ("../V2_bad", "/tmp/V2_bad", "V0_bad", "V2_bad/child", "V2_bad.txt"):
            text = f"## Version\n- New: {value}\n- From: V1_baseline"
            self.assertIsNone(harness.parse_version_directive(text))

    def test_version_creation_guards_and_nested_symlinks(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
            workspace = Path(td)
            source = workspace / "V1_baseline"
            source.mkdir()
            (source / "code.py").write_text("pass\n")
            prompt = "## Version\n- New: V2_trial\n- From: V1_baseline"
            created, error = harness.maybe_create_new_version(prompt, str(source), str(workspace))
            self.assertTrue(Path(created).is_dir(), error)
            self.assertIsNone(harness.maybe_create_new_version(prompt, str(source), str(workspace))[0])
            missing = "## Version\n- New: V3_trial\n- From: V9_missing"
            self.assertIsNone(harness.maybe_create_new_version(missing, str(source), str(workspace))[0])

            root_link = workspace / "V4_link"
            root_link.symlink_to(source, target_is_directory=True)
            linked = "## Version\n- New: V5_trial\n- From: V4_link"
            self.assertIsNone(harness.maybe_create_new_version(linked, str(source), str(workspace))[0])

            outside = Path(outside_td)
            (outside / "secret.txt").write_text("private")
            (outside / "tree").mkdir()
            (outside / "tree/data.txt").write_text("private")
            (source / "file_link").symlink_to(outside / "secret.txt")
            file_plan = "## Version\n- New: V6_trial\n- From: V1_baseline"
            made, reason = harness.maybe_create_new_version(file_plan, str(source), str(workspace))
            self.assertIsNone(made)
            self.assertIn("symlink", reason)
            (source / "file_link").unlink()
            (source / "dir_link").symlink_to(outside / "tree", target_is_directory=True)
            dir_plan = "## Version\n- New: V7_trial\n- From: V1_baseline"
            made, reason = harness.maybe_create_new_version(dir_plan, str(source), str(workspace))
            self.assertIsNone(made)
            self.assertIn("symlink", reason)

    def test_setup_v1_and_scan_reject_symlinks_and_malformed_names(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
            root = Path(td)
            code = root / "code"
            code.mkdir()
            (code / "escape").symlink_to(Path(outside_td), target_is_directory=True)
            workspace = root / "WorkSpace"
            self.assertIsNone(harness.setup_v1(str(workspace), str(code)))

            workspace.mkdir(exist_ok=True)
            valid = workspace / "V2_valid"
            valid.mkdir()
            (workspace / "V999junk").mkdir()
            (workspace / "V999_escape").symlink_to(Path(outside_td), target_is_directory=True)
            self.assertEqual([v["name"] for v in harness.scan_versions(str(workspace))], ["V2_valid"])

            for name in (harness.FIRST_VERSION_NAME, "v1"):
                candidate = workspace / name
                if candidate.exists():
                    shutil.rmtree(candidate)
                candidate.mkdir()
                (candidate / "escape").symlink_to(Path(outside_td), target_is_directory=True)
                self.assertIsNone(harness.setup_v1(str(workspace), str(code)))
                shutil.rmtree(candidate)

    def test_version_ownership_and_uppercase_contracts(self):
        skill = ROOT / "easy-auto-research"
        planner = (skill / "templates/planner.md").read_text()
        executer = (skill / "templates/executer.md").read_text()
        orchestrator = (skill / "templates/orchestrator.md").read_text()
        interviewer = (skill / "templates/interviewer.md").read_text()
        secretary = (skill / "templates/secretary.md").read_text()
        skill_text = (skill / "SKILL.md").read_text()

        self.assertIn("The harness creates the Planner-named version", executer)
        self.assertIn("Do not create, copy, rename, or delete version directories", executer)
        self.assertIn("harness has already created and selected the new version", orchestrator)
        self.assertIn("creates and manages these", planner)
        self.assertNotRegex(executer, r"`v(?:<N>|\d)")
        self.assertIn("`V<N>_xxx/`", executer)
        for text in (executer, orchestrator, interviewer):
            self.assertNotIn("train.log", text)
            self.assertIn("training.log", text)
        user_facing = [
            (ROOT / "README.md").read_text(),
            skill_text,
            *(path.read_text() for path in (skill / "templates").glob("*.md")),
        ]
        for text in user_facing:
            self.assertNotIn("watch_session.py", text)
            self.assertNotRegex(text, r"claude\s+--resume")
            self.assertNotRegex(text, r"(?i)\bcoding-agent\b|\bagent skill\b|\bunattended\b|\bhost\b")
        self.assertIn("ask Claude Code in natural language", secretary)
        self.assertNotIn("<cli>", secretary)
        self.assertNotIn("same CLI + model", interviewer)

    def test_generated_runtime_agent_prompt_contracts(self):
        templates = ROOT / "easy-auto-research/templates"

        def rendered(role):
            return re.sub(r"{{[^{}]+}}", "project value", (templates / f"{role}.md").read_text())

        for role, _ in init.AGENT_ROLES:
            with self.subTest(role=role):
                prompt = rendered(role)
                self.assertEqual(init.validate_runtime_agent_prompt(role, prompt), [])
                first_heading = re.search(r"^## .+$", prompt, re.MULTILINE).group(0)
                self.assertTrue(init.validate_runtime_agent_prompt(
                    role, prompt.replace(first_heading, "## Renamed Contract", 1),
                ))

        executer = rendered("executer")
        self.assertIn(
            "use training.log consistently",
            "; ".join(init.validate_runtime_agent_prompt("executer", executer.replace("training.log", "train.log"))),
        )
        self.assertIn(
            "must not manage version directories",
            "; ".join(init.validate_runtime_agent_prompt(
                "executer",
                executer.replace("Do not create, copy, rename, or delete version directories", "Manage versions"),
            )),
        )
        self.assertIn(
            "lowercase version reference",
            "; ".join(init.validate_runtime_agent_prompt("planner", rendered("planner") + "\nUse v2_trial.")),
        )

    def test_verifier_prompt_contract_mutations_fail_closed(self):
        template = (ROOT / "easy-auto-research/templates/verifier.md").read_text()
        verifier = re.sub(r"{{[^{}]+}}", "project value", template)
        mutations = {
            "removed terminal heading": verifier.replace("## Terminal Output Contract\n", "", 1),
            "renamed terminal heading": verifier.replace(
                "## Terminal Output Contract", "## Final Output Contract", 1,
            ),
            "missing ERROR status": verifier.replace(
                "STATUS: ERROR            # otherwise broken (log unreadable, PID lookup failed)\n", "", 1,
            ),
            "renamed ERROR status": verifier.replace("STATUS: ERROR", "STATUS: FAILED", 1),
            "extra unsupported status": verifier + "\nSTATUS: UNKNOWN\n",
            "renamed phenomena start": verifier.replace("PHENOMENA:\n<1-3", "OBSERVATIONS:\n<1-3", 1),
            "renamed phenomena end": verifier.replace("PHENOMENA_END:\n```", "OBSERVATIONS_END:\n```", 1),
            "removed observational requirement": verifier.replace("OBSERVATIONAL ONLY", "concise", 1),
            "removed exact terminal shape": verifier.replace(
                "terminal reply is EXACTLY one `STATUS:` line followed by a `PHENOMENA:` … `PHENOMENA_END:` block — nothing else.",
                "terminal reply should summarize the run.",
                1,
            ),
        }
        for name, mutated in mutations.items():
            with self.subTest(mutation=name):
                self.assertTrue(init.validate_runtime_agent_prompt("verifier", mutated))
        for status in init._VERIFIER_TERMINAL_STATUSES:
            with self.subTest(mutation=f"removed STATUS {status}"):
                mutated = re.sub(
                    rf"^STATUS: {status}.*\n", "", verifier, count=1, flags=re.MULTILINE,
                )
                self.assertTrue(init.validate_runtime_agent_prompt("verifier", mutated))

    def test_agent_writer_rejects_generated_prompt_contract_drift(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            agents = project / "agents"
            agents.mkdir()
            (project / "goal.md").write_text("goal\n")
            (project / "PROJECT_BRIEF.md").write_text("brief\n")
            output = agents / "executer.md"

            def write_invalid(*args, **kwargs):
                output.write_text("Use train.log in v2_trial.\n")
                return subprocess.CompletedProcess(["claude"], 0, "done", "")

            with mock.patch.object(init, "TEMPLATES_DIR", str(ROOT / "easy-auto-research/templates")), \
                    mock.patch.object(init, "_run_process_group", side_effect=write_invalid):
                role, ok, detail = init._setup_one_agent("executer", str(project), 10)

        self.assertEqual(role, "executer")
        self.assertFalse(ok)
        self.assertIn("contract validation failed", detail)
        self.assertIn("training.log", detail)
        self.assertIn("lowercase version reference", detail)

    def test_inherited_results_do_not_count_after_refreshed_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            source = workspace / "V1_baseline"
            result = source / "train/results.jsonl"
            result.parent.mkdir(parents=True)
            result.write_text("old")
            prompt = "## Version\n- New: V2_trial\n- From: V1_baseline"
            created, error = harness.maybe_create_new_version(prompt, str(source), str(workspace))
            self.assertTrue(created, error)
            refreshed = harness.result_signatures(str(workspace))
            self.assertEqual(refreshed, harness.result_signatures(str(workspace)))
            Path(created, "train/results.jsonl").write_text("new training output")
            self.assertNotEqual(refreshed, harness.result_signatures(str(workspace)))

    def test_result_signature_detects_equal_size_same_mtime_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            result = workspace / "V1_baseline/train/results.jsonl"
            result.parent.mkdir(parents=True)
            result.write_bytes(b"alpha")
            timestamp = result.stat().st_mtime_ns
            before = harness.result_signatures(str(workspace))
            result.write_bytes(b"bravo")
            os.utime(result, ns=(timestamp, timestamp))
            after = harness.result_signatures(str(workspace))
            self.assertNotEqual(before, after)


class ContractTests(unittest.TestCase):
    def _goal_met(self, paths, metric="1.0", justification="supported"):
        evidence = "\n".join(f"  - {path} (read)" for path in paths)
        return (
            "EVAL_VERDICT: GOAL_MET\n"
            f"PRIMARY_METRIC: {metric}\n"
            f"EVIDENCE:\n{evidence}\n"
            f"JUSTIFICATION: {justification}"
        )

    def test_strict_evaluator_contract_requires_all_evidence(self):
        not_met = (
            "analysis may precede the contract\n"
            "EVAL_VERDICT: GOAL_NOT_MET\n"
            "PRIMARY_METRIC: N/A\n"
            "EVIDENCE:\n  - /tmp/missing (inspected)\n"
            "JUSTIFICATION: criterion failed"
        )
        self.assertEqual(harness.parse_evaluator_verdict(not_met), "GOAL_NOT_MET")
        self.assertIsNone(harness.parse_evaluator_verdict(not_met + "\ntrailing prose"))
        self.assertIsNone(harness.parse_evaluator_verdict(not_met + "\nEVAL_VERDICT: INCONCLUSIVE"))
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
            root = Path(td)
            first = root / "metrics.json"
            second = root / "report.txt"
            first.write_text("{}")
            second.write_text("ok")
            response = self._goal_met([first, second])
            self.assertTrue(harness._validate_eval_verdict_goal_met(response, str(root))[0])

            second.write_text("")
            self.assertFalse(harness._validate_eval_verdict_goal_met(response, str(root))[0])
            second.write_text("ok")
            outside = Path(outside_td) / "outside.txt"
            outside.write_text("x")
            self.assertFalse(
                harness._validate_eval_verdict_goal_met(self._goal_met([first, outside]), str(root))[0]
            )
            self.assertFalse(
                harness._validate_eval_verdict_goal_met(response.replace("JUSTIFICATION: supported", "JUSTIFICATION:"), str(root))[0]
            )
            self.assertFalse(
                harness._validate_eval_verdict_goal_met(response.replace("PRIMARY_METRIC: 1.0\n", ""), str(root))[0]
            )
            directory = root / "artifact_dir"
            directory.mkdir()
            self.assertFalse(
                harness._validate_eval_verdict_goal_met(self._goal_met([directory]), str(root))[0]
            )

    def test_primary_metric_is_unique_and_fully_anchored(self):
        self.assertEqual(harness.parse_primary_metric("PRIMARY_METRIC: -1.25e-2"), -0.0125)
        self.assertIsNone(harness.parse_primary_metric("PRIMARY_METRIC: 1garbage"))
        self.assertIsNone(harness.parse_primary_metric("PRIMARY_METRIC: 1\nPRIMARY_METRIC: 2"))
        self.assertIsNone(harness.parse_primary_metric("PRIMARY_METRIC: N/A"))
        self.assertIsNone(harness.parse_primary_metric("PRIMARY_METRIC: nan"))

    def test_verifier_terminal_requires_phenomena_for_normal_completion(self):
        status, phenomena, error = harness.validate_verifier_terminal(
            "STATUS: DONE_NORMAL\nPHENOMENA:\nloss fell steadily\nPHENOMENA_END:"
        )
        self.assertEqual(status, "DONE_NORMAL")
        self.assertIn("loss fell", phenomena)
        self.assertEqual(error, "")
        for bad in (
            "STATUS: DONE_NORMAL",
            "STATUS: DONE_NORMAL\nPHENOMENA:\nPHENOMENA_END:",
            "STATUS: UNKNOWN\nPHENOMENA:\nx\nPHENOMENA_END:",
            "STATUS: DONE_NORMAL\nSTATUS: CRASHED\nPHENOMENA:\nx\nPHENOMENA_END:",
        ):
            self.assertTrue(harness.validate_verifier_terminal(bad)[2])
        self.assertEqual(harness.validate_verifier_terminal("STATUS: CRASHED")[0], "CRASHED")

    def test_pid_receipts_use_one_pidfd_identity_and_never_numeric_kill(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
            identity = {
                "pid": 123,
                "start_time": "456",
                "uid": os.getuid(),
                "cwd": td,
                "command": "python train.py",
            }
            common = (
                mock.patch.object(harness, "_pidfd_available", return_value=True),
                mock.patch.object(harness.os, "pidfd_open", return_value=44, create=True),
                mock.patch.object(harness.os, "close"),
            )
            with common[0], common[1] as pidfd_open, common[2]:
                with mock.patch.object(harness, "process_identity", return_value=identity):
                    self.assertTrue(harness.pid_receipt_matches(dict(identity), td))
                    pidfd_open.assert_called_with(123, 0)
                    for key, value in (
                        ("start_time", "wrong"), ("uid", os.getuid() + 1),
                        ("cwd", outside_td), ("command", "other"),
                    ):
                        receipt = dict(identity)
                        receipt[key] = value
                        self.assertFalse(harness.pid_receipt_matches(receipt, td))
                    self.assertFalse(harness.pid_receipt_matches({**identity, "pid": None}, td))
                    self.assertFalse(harness.pid_receipt_matches({**identity, "uid": "0"}, td))

            with mock.patch.object(harness, "_pidfd_available", return_value=True), \
                    mock.patch.object(harness.os, "pidfd_open", return_value=45, create=True), \
                    mock.patch.object(harness.os, "close"), \
                    mock.patch.object(harness, "process_identity", return_value=None), \
                    mock.patch.object(harness.os, "kill") as numeric_kill, \
                    mock.patch.object(harness.signal, "pidfd_send_signal", create=True) as pidfd_signal:
                self.assertFalse(harness.signal_pid_receipt(identity, td))
                numeric_kill.assert_not_called()
                pidfd_signal.assert_not_called()

            reused = {**identity, "start_time": "new-process"}
            with mock.patch.object(harness, "_pidfd_available", return_value=True), \
                    mock.patch.object(harness.os, "pidfd_open", return_value=46, create=True), \
                    mock.patch.object(harness.os, "close"), \
                    mock.patch.object(harness, "process_identity", return_value=reused), \
                    mock.patch.object(harness.os, "kill") as numeric_kill, \
                    mock.patch.object(harness.signal, "pidfd_send_signal", create=True) as pidfd_signal:
                self.assertFalse(harness.signal_pid_receipt(identity, td))
                numeric_kill.assert_not_called()
                pidfd_signal.assert_not_called()

            with mock.patch.object(harness, "_pidfd_available", return_value=False), \
                    mock.patch.object(harness.os, "kill") as numeric_kill:
                self.assertFalse(harness.signal_pid_receipt(identity, td))
                numeric_kill.assert_not_called()
        self.assertFalse(harness.pid_receipt_matches(123, "/tmp"))

    def test_error_replan_diagnostic_reaches_next_planner_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            current = workspace / "V1_baseline"
            current.mkdir()
            config = types.SimpleNamespace(codebase_path=str(current))
            planner_prompts = []

            def call_agent(role, prompt, **kwargs):
                self.assertEqual(role, "planner")
                planner_prompts.append(prompt)
                return "plan without a version directive"

            orchestrator_reply = {
                "action": "advance",
                "target": "planner",
                "prompt": "Produce the next plan.",
                "summary": "",
                "cycle_done": False,
                "success": False,
                "reason": "",
            }
            with mock.patch.object(harness, "run_orchestrator_agent_turn", return_value=orchestrator_reply), \
                    mock.patch.object(harness, "call_agent", side_effect=call_agent), \
                    mock.patch.object(harness, "record_session_uuid"):
                result, resulting_cwd = harness.run_cycle_v2(
                    2, config, str(workspace), str(current),
                    replan_diagnostic="source version V9_missing does not exist",
                )

            self.assertEqual(result.status, "ERROR_REPLAN")
            self.assertEqual(resulting_cwd, str(current))
            self.assertEqual(len(planner_prompts), 1)
            self.assertIn("ERROR_REPLAN DIAGNOSTIC FROM THE PREVIOUS ATTEMPT", planner_prompts[0])
            self.assertIn("source version V9_missing does not exist", planner_prompts[0])
            self.assertIn("Produce the next plan.", planner_prompts[0])

    def _run_to_verifier(self, workspace, verifier_outputs):
        current = workspace / "V1_baseline"
        current.mkdir()
        (current / "code.py").write_text("pass\n")
        config = types.SimpleNamespace(codebase_path=str(current))
        targets = iter(("planner", "executer", "secretary", "verifier"))

        def orchestrator_turn(*args, **kwargs):
            target = next(targets)
            return {
                "action": "advance",
                "target": target,
                "prompt": f"Run {target}.",
                "summary": "",
                "cycle_done": False,
                "success": False,
                "reason": "",
            }

        verifier_outputs = iter(verifier_outputs)

        def call_agent(role, prompt, **kwargs):
            if role == "planner":
                return "## Version\n- New: V2_trial\n- From: V1_baseline"
            if role == "verifier":
                return next(verifier_outputs)
            return f"{role} completed"

        with mock.patch.object(harness, "ORCHESTRATOR_DIR", str(workspace)), \
                mock.patch.object(harness, "run_orchestrator_agent_turn", side_effect=orchestrator_turn), \
                mock.patch.object(harness, "call_agent", side_effect=call_agent), \
                mock.patch.object(harness, "record_session_uuid"):
            return harness.run_cycle_v2(1, config, str(workspace), str(current))

    def test_verifier_failure_statuses_return_actionable_error_replan(self):
        for status in ("KILLED_BUDGET", "KILLED_EARLY_STOP", "CRASHED", "ERROR"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as td:
                observation = f"{status.lower()} after step 12; inspect training.log"
                output = f"STATUS: {status}\nPHENOMENA:\n{observation}\nPHENOMENA_END:"
                result, _ = self._run_to_verifier(Path(td), [output])
                self.assertEqual(result.status, "ERROR_REPLAN")
                self.assertIn(f"STATUS: {status}", result.error_context)
                self.assertIn(observation, result.error_context)
                self.assertIn("Evaluator was skipped", result.error_context)

    def test_repeated_verifier_subprocess_failures_return_error_replan(self):
        with tempfile.TemporaryDirectory() as td:
            result, _ = self._run_to_verifier(
                Path(td),
                [
                    "[AGENT ERROR] verifier timed out after 10s",
                    "[AGENT ERROR] verifier exited with code 1: unavailable",
                ],
            )
        self.assertEqual(result.status, "ERROR_REPLAN")
        self.assertIn("Verifier subprocess failed twice", result.error_context)
        self.assertIn("timed out after 10s", result.error_context)
        self.assertIn("exited with code 1", result.error_context)

    def test_verifier_error_replan_diagnostic_reaches_next_planner(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            failure, failed_cwd = self._run_to_verifier(
                workspace,
                ["STATUS: CRASHED\nPHENOMENA:\nOOM at step 12\nPHENOMENA_END:"],
            )
            queued_diagnostic, reset_count, boundary_reached = harness._prepare_replan_attempt(
                failure.error_context, 3, 3,
            )
            self.assertTrue(boundary_reached)
            self.assertEqual(reset_count, 0)
            self.assertEqual(queued_diagnostic, failure.error_context)

            planner_prompts = []
            config = types.SimpleNamespace(codebase_path=failed_cwd)

            def call_agent(role, prompt, **kwargs):
                planner_prompts.append(prompt)
                return "plan without a version directive"

            reply = {
                "action": "advance",
                "target": "planner",
                "prompt": "Produce the next plan.",
                "summary": "",
                "cycle_done": False,
                "success": False,
                "reason": "",
            }
            with mock.patch.object(harness, "ORCHESTRATOR_DIR", str(workspace)), \
                    mock.patch.object(harness, "run_orchestrator_agent_turn", return_value=reply), \
                    mock.patch.object(harness, "call_agent", side_effect=call_agent), \
                    mock.patch.object(harness, "record_session_uuid"):
                result, _ = harness.run_cycle_v2(
                    2, config, str(workspace), failed_cwd,
                    replan_diagnostic=queued_diagnostic,
                )

        self.assertEqual(result.status, "ERROR_REPLAN")
        self.assertEqual(len(planner_prompts), 1)
        self.assertIn("ERROR_REPLAN DIAGNOSTIC FROM THE PREVIOUS ATTEMPT", planner_prompts[0])
        self.assertIn("STATUS: CRASHED", planner_prompts[0])
        self.assertIn("OOM at step 12", planner_prompts[0])

    def test_jsonl_recovery_is_exact_invocation_scoped_and_terminal(self):
        def event(text, reason):
            return json.dumps({
                "type": "assistant",
                "message": {"stop_reason": reason, "content": [{"type": "text", "text": text}]},
            }) + "\n"

        with tempfile.TemporaryDirectory() as td:
            exact = Path(td) / "exact.jsonl"
            stale = Path(td) / "stale.jsonl"
            exact.write_text(event("historical", "end_turn"))
            stale.write_text(event("wrong session", "end_turn"))
            offset = exact.stat().st_size
            with exact.open("a") as f:
                f.write(event("intermediate", "tool_use"))
            self.assertIsNone(harness._tail_jsonl_assistant_text(str(exact), offset))
            with exact.open("a") as f:
                f.write(event("final", "end_turn"))
            self.assertEqual(harness._tail_jsonl_assistant_text(str(exact), offset), "final")
            self.assertIsNone(harness._tail_jsonl_assistant_text(str(stale), stale.stat().st_size))

    def _fork_process(self, target, *args):
        process = multiprocessing.get_context("fork").Process(target=target, args=args)
        process.start()
        return process

    def _recv(self, connection, timeout=3):
        self.assertTrue(connection.poll(timeout), "child did not respond before timeout")
        return connection.recv()

    def _join(self, process, timeout=3):
        process.join(timeout)
        if process.is_alive():
            process.terminate()
            process.join(2)
            self.fail("child process exceeded timeout")
        self.assertEqual(process.exitcode, 0)

    def test_only_one_cross_process_claim_and_restore_preserves_fifo(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(harness, "ORCHESTRATOR_DIR", td):
            comments = Path(td) / harness.HUMAN_COMMENTS_FILENAME
            harness._append_message_file(str(comments), "first")
            first = harness.consume_human_comments()
            harness._append_message_file(str(comments), "second")

            parent_conn, child_conn = multiprocessing.get_context("fork").Pipe()
            child = self._fork_process(_child_claim_result, child_conn, str(comments))
            child_conn.close()
            self.assertIsNone(self._recv(parent_conn))
            parent_conn.close()
            self._join(child)

            self.assertTrue(harness._restore_message_claim(first))
            parent_conn, child_conn = multiprocessing.get_context("fork").Pipe()
            child = self._fork_process(_child_claim_result, child_conn, str(comments))
            child_conn.close()
            text, token = self._recv(parent_conn)
            parent_conn.close()
            self._join(child)
            # The child exited with an active claim, so stale recovery must retry
            # the original prefix before the append rather than reversing FIFO.
            self.assertEqual(text, "first\nsecond")
            recovered = harness.consume_human_comments()
            self.assertEqual(recovered.text, "first\nsecond")
            self.assertNotEqual(recovered.token, token)
            self.assertTrue(harness._ack_message_claim(recovered))
            self.assertIsNone(harness.consume_human_comments())

    def test_forked_claim_copy_cannot_restore_or_ack_parent_ownership(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(harness, "ORCHESTRATOR_DIR", td):
            comments = Path(td) / harness.HUMAN_COMMENTS_FILENAME
            harness._append_message_file(str(comments), "owned")
            claim = harness.consume_human_comments()

            parent_conn, child_conn = multiprocessing.get_context("fork").Pipe()
            child = self._fork_process(_child_finish_inherited_claim, child_conn, claim)
            child_conn.close()
            self.assertEqual(self._recv(parent_conn), (False, False))
            parent_conn.close()
            self._join(child)

            stale_token = copy.copy(claim)
            stale_token.token = "stale-token"
            self.assertFalse(harness._ack_message_claim(stale_token))
            self.assertFalse(harness._restore_message_claim(stale_token))
            self.assertTrue(harness._ack_message_claim(claim))
            self.assertFalse(harness._ack_message_claim(claim))
            self.assertFalse(harness._restore_message_claim(claim))
            self.assertIsNone(harness.consume_human_comments())

    def test_cooperative_split_write_is_claimed_as_one_atomic_message(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(harness, "ORCHESTRATOR_DIR", td):
            comments = Path(td) / harness.HUMAN_COMMENTS_FILENAME
            writer_parent, writer_child = multiprocessing.get_context("fork").Pipe()
            writer = self._fork_process(_child_locked_split_append, writer_child, str(comments))
            writer_child.close()
            self.assertEqual(self._recv(writer_parent), "half-written")

            claim_parent, claim_child = multiprocessing.get_context("fork").Pipe()
            claimer = self._fork_process(_child_claim_result, claim_child, str(comments))
            claim_child.close()
            self.assertFalse(claim_parent.poll(0.2), "consumer claimed a cooperative partial write")
            writer_parent.send("finish")
            self.assertEqual(self._recv(writer_parent), "complete")
            writer_parent.close()
            self._join(writer)

            text, _ = self._recv(claim_parent)
            claim_parent.close()
            self._join(claimer)
            self.assertEqual(text, "first-half-second-half")
            recovered = harness.consume_human_comments()
            self.assertEqual(recovered.text, "first-half-second-half")
            self.assertTrue(harness._ack_message_claim(recovered))

    def test_unlocked_open_append_during_claim_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(harness, "ORCHESTRATOR_DIR", td):
            comments = Path(td) / harness.HUMAN_COMMENTS_FILENAME
            comments.write_bytes(b"first\n")
            parent_conn, child_conn = multiprocessing.get_context("fork").Pipe()
            writer = self._fork_process(_child_unlocked_append, child_conn, str(comments), b"second\n")
            child_conn.close()
            self.assertEqual(self._recv(parent_conn), "opened")

            original_write_state = harness._write_message_state
            appended = False

            def append_while_claiming(path, state):
                nonlocal appended
                if not appended and state.get("claim") is not None:
                    appended = True
                    parent_conn.send("append")
                    self.assertEqual(self._recv(parent_conn), "appended")
                return original_write_state(path, state)

            with mock.patch.object(harness, "_write_message_state", side_effect=append_while_claiming):
                first = harness.consume_human_comments()
            parent_conn.close()
            self._join(writer)

            self.assertEqual(first.text, "first")
            self.assertEqual(comments.read_bytes(), b"first\nsecond\n")
            self.assertTrue(harness._ack_message_claim(first))
            second = harness.consume_human_comments()
            self.assertEqual(second.text, "second")
            self.assertTrue(harness._ack_message_claim(second))
            self.assertEqual(comments.read_bytes(), b"first\nsecond\n")

    def test_crashed_owner_is_recovered_with_new_token(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(harness, "ORCHESTRATOR_DIR", td):
            comments = Path(td) / harness.HUMAN_COMMENTS_FILENAME
            harness._append_message_file(str(comments), "retry me")
            parent_conn, child_conn = multiprocessing.get_context("fork").Pipe()
            child = self._fork_process(_child_claim_then_crash, child_conn, str(comments))
            child_conn.close()
            text, stale_token = self._recv(parent_conn)
            parent_conn.close()
            self.assertEqual(text, "retry me")
            self._join(child)

            recovered = harness.consume_human_comments()
            self.assertEqual(recovered.text, "retry me")
            self.assertNotEqual(recovered.token, stale_token)
            self.assertTrue(harness._ack_message_claim(recovered))
            self.assertIsNone(harness.consume_human_comments())

            harness._append_message_file(str(comments), "ack before crash")
            parent_conn, child_conn = multiprocessing.get_context("fork").Pipe()
            child = self._fork_process(_child_ack_then_crash, child_conn, str(comments))
            child_conn.close()
            self.assertEqual(self._recv(parent_conn), ("ack before crash", True))
            parent_conn.close()
            self._join(child)
            self.assertIsNone(harness.consume_human_comments())

    def test_success_retry_recursive_polling_and_order_for_both_channels(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(harness, "ORCHESTRATOR_DIR", td):
            comments = Path(td) / harness.HUMAN_COMMENTS_FILENAME
            harness._append_message_file(str(comments), "comment-one")
            first = harness.consume_human_comments()
            self.assertIsNone(harness.consume_human_comments())
            harness._append_message_file(str(comments), "comment-two")
            self.assertIsNone(harness.consume_human_comments())
            self.assertTrue(harness._restore_message_claim(first))
            retry = harness.consume_human_comments()
            self.assertEqual(retry.text, "comment-one\ncomment-two")
            self.assertTrue(harness._ack_message_claim(retry))
            harness._append_message_file(str(comments), "comment-three")
            third = harness.consume_human_comments()
            self.assertEqual(third.text, "comment-three")
            self.assertTrue(harness._ack_message_claim(third))

            interrupt = Path(td) / harness.HUMAN_INTERRUPT_FILENAME
            for message in ("urgent-one", "urgent-two", "urgent-three"):
                harness._append_message_file(str(interrupt), f"{message}\n**end**")
            message, first = harness._check_human_interrupt()
            self.assertEqual(message, "urgent-one")
            self.assertIsNone(harness._check_human_interrupt())
            self.assertTrue(harness._restore_message_claim(first))
            delivered = []
            for _ in range(3):
                message, current = harness._check_human_interrupt()
                delivered.append(message)
                self.assertTrue(harness._ack_message_claim(current))
            self.assertEqual(delivered, ["urgent-one", "urgent-two", "urgent-three"])
            self.assertIsNone(harness._check_human_interrupt())

    def test_empty_interrupt_is_committed_before_next_fifo_message(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(harness, "ORCHESTRATOR_DIR", td):
            interrupt = Path(td) / harness.HUMAN_INTERRUPT_FILENAME
            harness._append_message_file(str(interrupt), "**end**")
            harness._append_message_file(str(interrupt), "deliver me\n**end**")
            message, claim = harness._check_human_interrupt()
            self.assertEqual(message, "deliver me")
            self.assertTrue(harness._ack_message_claim(claim))
            self.assertIsNone(harness._check_human_interrupt())

    def test_message_owner_liveness_is_fail_closed_and_identity_safe(self):
        owner = harness._message_process_identity()
        self.assertIsNotNone(owner)
        reused = {**owner, "start_time": str(int(owner["start_time"]) + 1)}

        with mock.patch.object(harness.os, "kill", side_effect=PermissionError), \
                mock.patch.object(harness, "_message_process_identity", return_value=None):
            self.assertTrue(
                harness._message_owner_is_live(owner),
                "permission denial plus unverifiable identity must preserve ownership",
            )
        with mock.patch.object(harness.os, "kill", side_effect=PermissionError), \
                mock.patch.object(harness, "_message_process_identity", return_value=owner):
            self.assertFalse(harness._message_owner_is_live(reused),
                             "a readable identity mismatch proves PID reuse")
        with mock.patch.object(harness.os, "kill", side_effect=ProcessLookupError), \
                mock.patch.object(harness, "_message_process_identity") as identity:
            self.assertFalse(harness._message_owner_is_live(owner),
                             "ProcessLookupError positively establishes owner death")
            identity.assert_not_called()
        with mock.patch.object(harness.os, "kill", return_value=None), \
                mock.patch.object(harness, "_message_process_identity", return_value=owner):
            self.assertTrue(harness._message_owner_is_live(owner))

    def test_unverifiable_permission_denied_owner_cannot_be_reclaimed(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(harness, "ORCHESTRATOR_DIR", td):
            comments = Path(td) / harness.HUMAN_COMMENTS_FILENAME
            harness._append_message_file(str(comments), "preserve me")
            claim = harness.consume_human_comments()
            self.assertIsNotNone(claim)
            with mock.patch.object(harness.os, "kill", side_effect=PermissionError), \
                    mock.patch.object(harness, "_message_process_identity", return_value=None):
                self.assertIsNone(harness.consume_human_comments())
            self.assertTrue(harness._ack_message_claim(claim))

    def test_ack_failure_is_an_error_and_releases_both_channels_for_retry(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(harness, "ORCHESTRATOR_DIR", td), \
                mock.patch.object(harness, "AGENTS_DIR", str(Path(td) / "agents")), \
                mock.patch.dict(harness.AGENTS, {"planner": "planner-session", "executer": "exec-session"}):
            harness._initialized.update({("planner", "planner-session"), ("executer", "exec-session")})
            comments = Path(td) / harness.HUMAN_COMMENTS_FILENAME
            interrupt = Path(td) / harness.HUMAN_INTERRUPT_FILENAME
            harness._append_message_file(str(comments), "retry comment")
            try:
                with mock.patch.object(
                        harness, "_run_with_heartbeat",
                        return_value=subprocess.CompletedProcess([], 0, "planner output", "")), \
                        mock.patch.object(harness, "_ack_message_claim", return_value=False), \
                        mock.patch.object(harness, "append_agent_history") as history, \
                        mock.patch.object(harness, "log_agent_thought") as thought:
                    result = harness.call_agent("planner", "work")
                self.assertTrue(harness.is_agent_error(result))
                history.assert_not_called()
                thought.assert_not_called()
                retry = harness.consume_human_comments()
                self.assertEqual(retry.text, "retry comment")
                self.assertTrue(harness._ack_message_claim(retry))

                harness._append_message_file(str(interrupt), "retry interrupt\n**end**")
                calls = 0

                def interrupted_then_complete(cmd, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        message, claim = harness._check_human_interrupt()
                        raise harness.HumanInterrupt(message, claim)
                    return subprocess.CompletedProcess(cmd, 0, "interrupt output", "")

                with mock.patch.object(harness, "_run_with_heartbeat", side_effect=interrupted_then_complete), \
                        mock.patch.object(harness, "_ack_message_claim", return_value=False), \
                        mock.patch.object(harness, "append_agent_history") as history, \
                        mock.patch.object(harness, "log_agent_thought") as thought:
                    result = harness.call_agent("executer", "work")
                self.assertTrue(harness.is_agent_error(result))
                history.assert_not_called()
                thought.assert_not_called()
                message, retry = harness._check_human_interrupt()
                self.assertEqual(message, "retry interrupt")
                self.assertTrue(harness._ack_message_claim(retry))
            finally:
                harness._initialized.discard(("planner", "planner-session"))
                harness._initialized.discard(("executer", "exec-session"))

    def test_successful_delivery_commits_before_publishing_side_effects(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(harness, "ORCHESTRATOR_DIR", td), \
                mock.patch.object(harness, "AGENTS_DIR", str(Path(td) / "agents")), \
                mock.patch.dict(harness.AGENTS, {"planner": "planner-session", "executer": "exec-session"}):
            harness._initialized.update({("planner", "planner-session"), ("executer", "exec-session")})
            comments = Path(td) / harness.HUMAN_COMMENTS_FILENAME
            interrupt = Path(td) / harness.HUMAN_INTERRUPT_FILENAME
            harness._append_message_file(str(comments), "ordered")
            events = []
            original_ack = harness._ack_message_claim

            def ack(claim):
                events.append("ack")
                return original_ack(claim)

            try:
                with mock.patch.object(
                        harness, "_run_with_heartbeat",
                        return_value=subprocess.CompletedProcess([], 0, "output", "")), \
                        mock.patch.object(harness, "_ack_message_claim", side_effect=ack), \
                        mock.patch.object(harness, "append_agent_history", side_effect=lambda *args: events.append("history")), \
                        mock.patch.object(harness, "log_agent_thought", side_effect=lambda *args: events.append("thought")):
                    self.assertEqual(harness.call_agent("planner", "work"), "output")
                self.assertEqual(events, ["ack", "history", "thought"])
                self.assertIsNone(harness.consume_human_comments())

                events.clear()
                harness._append_message_file(str(interrupt), "ordered interrupt\n**end**")
                calls = 0

                def interrupted_then_complete(cmd, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        message, claim = harness._check_human_interrupt()
                        raise harness.HumanInterrupt(message, claim)
                    return subprocess.CompletedProcess(cmd, 0, "interrupt output", "")

                with mock.patch.object(harness, "_run_with_heartbeat", side_effect=interrupted_then_complete), \
                        mock.patch.object(harness, "_ack_message_claim", side_effect=ack), \
                        mock.patch.object(harness, "append_agent_history", side_effect=lambda *args: events.append("history")), \
                        mock.patch.object(harness, "log_agent_thought", side_effect=lambda *args: events.append("thought")):
                    self.assertEqual(harness.call_agent("executer", "work"), "interrupt output")
                self.assertEqual(events, ["ack", "history", "thought"])
                self.assertIsNone(harness._check_human_interrupt())
            finally:
                harness._initialized.discard(("planner", "planner-session"))
                harness._initialized.discard(("executer", "exec-session"))

    def test_message_channels_and_sidecars_reject_symlinks(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(harness, "ORCHESTRATOR_DIR", td):
            target = Path(td) / "target.txt"
            target.write_text("do not touch")
            for name in (harness.HUMAN_COMMENTS_FILENAME, harness.HUMAN_INTERRUPT_FILENAME):
                channel = Path(td) / name
                channel.symlink_to(target)
                harness._append_message_file(str(channel), "malicious")
                self.assertIsNone(harness._claim_message_file(str(channel)))
                self.assertEqual(target.read_text(), "do not touch")
                channel.unlink()

                channel.write_text("safe\n")
                lock = Path(td) / f".{name}.queue.lock"
                lock.symlink_to(target)
                self.assertIsNone(harness._claim_message_file(str(channel)))
                self.assertEqual(target.read_text(), "do not touch")
                lock.unlink()

                claim = harness._claim_message_file(str(channel))
                self.assertIsNotNone(claim)
                self.assertEqual(stat.S_IMODE((Path(td) / f".{name}.queue.lock").stat().st_mode), 0o600)
                state_path = Path(td) / f".{name}.queue.state"
                self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
                self.assertTrue(harness._ack_message_claim(claim))
                state_path.unlink()
                state_path.symlink_to(target)
                self.assertIsNone(harness._claim_message_file(str(channel)))
                self.assertEqual(target.read_text(), "do not touch")
                state_path.unlink()
                (Path(td) / f".{name}.queue.lock").unlink()
                channel.unlink()

            with tempfile.TemporaryDirectory() as outside_td:
                outside_channel = Path(outside_td) / "human_comments.txt"
                outside_channel.write_text("outside\n")
                self.assertIsNone(harness._claim_message_file(str(outside_channel)))
                self.assertFalse((Path(outside_td) / ".human_comments.txt.queue.lock").exists())

    def test_resumed_interrupt_delivery_cannot_reclaim_its_active_channel(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(harness, "ORCHESTRATOR_DIR", td), \
                mock.patch.object(harness, "AGENTS_DIR", str(Path(td) / "agents")), \
                mock.patch.dict(harness.AGENTS, {"executer": "session"}):
            harness._initialized.add(("executer", "session"))
            interrupt = Path(td) / harness.HUMAN_INTERRUPT_FILENAME
            harness._append_message_file(str(interrupt), "first\n**end**")
            calls = []

            def run(cmd, **kwargs):
                calls.append((list(cmd), kwargs["input_text"]))
                if len(calls) == 1:
                    message, claim = harness._check_human_interrupt()
                    harness._append_message_file(str(interrupt), "second\n**end**")
                    raise harness.HumanInterrupt(message, claim)
                self.assertIsNone(harness._check_human_interrupt())
                return subprocess.CompletedProcess(cmd, 0, "delivered", "")

            try:
                with mock.patch.object(harness, "_run_with_heartbeat", side_effect=run), \
                        mock.patch.object(harness, "append_agent_history"), \
                        mock.patch.object(harness, "log_agent_thought"):
                    self.assertEqual(harness.call_agent("executer", "work"), "delivered")
            finally:
                harness._initialized.discard(("executer", "session"))

            self.assertEqual(len(calls), 2)
            self.assertIn("--resume", calls[1][0])
            self.assertIn("first", calls[1][1])
            message, claim = harness._check_human_interrupt()
            self.assertEqual(message, "second")
            self.assertTrue(harness._ack_message_claim(claim))
            self.assertIsNone(harness._check_human_interrupt())


class ProcessAndCheckpointTests(unittest.TestCase):
    def test_real_sigterm_ignoring_descendant_is_killed_with_whole_group(self):
        child_code = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(60)"
        )
        leader_code = (
            "import signal,subprocess,sys,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
            "print(p.pid, flush=True); time.sleep(60)"
        )
        for terminate, group_exists in (
            (init._terminate_process_group, init._process_group_exists),
            (harness._kill_proc, harness._process_group_exists),
        ):
            proc = subprocess.Popen(
                [sys.executable, "-c", leader_code], stdout=subprocess.PIPE,
                text=True, start_new_session=True,
            )
            child_pid = int(proc.stdout.readline().strip())
            proc.stdout.close()
            try:
                terminate(proc)
                self.assertFalse(group_exists(proc.pid))
                deadline = time.monotonic() + 2
                while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(Path(f"/proc/{child_pid}").exists())
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_completed_heartbeat_process_clears_global_process_reference(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(harness, "ORCHESTRATOR_DIR", td):
            result = harness._run_with_heartbeat(
                [sys.executable, "-c", "print('done')"],
                input_text="", timeout=5, cwd=td, role="executer", session_id="test-session",
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "done")
        self.assertIsNone(harness._current_proc)

    def test_burned_orchestrator_uuid_updates_cycle_for_all_later_turns(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(harness, "AGENTS_DIR", td), \
                mock.patch.object(harness, "ORCHESTRATOR_DIR", td), \
                mock.patch.object(harness, "record_session_uuid"), \
                mock.patch.object(harness.uuid, "uuid4", side_effect=["fresh-1", "fresh-2"]):
            Path(td, "orchestrator.md").write_text("system")
            session = harness.CycleSession(1, "original")
            calls = []

            def run(cmd, **kwargs):
                calls.append((list(cmd), kwargs["session_id"]))
                if len(calls) <= 2:
                    return subprocess.CompletedProcess(cmd, 1, "", "already in use")
                return subprocess.CompletedProcess(cmd, 0, '{"action":"end_cycle","summary":"x","reason":"x","cycle_done":true}', "")

            harness._initialized.clear()
            with mock.patch.object(harness, "_run_with_heartbeat", side_effect=run):
                first = harness.call_agent(
                    harness.ORCHESTRATOR_ROLE, "turn one",
                    session_id_override=session.orchestrator_session_id,
                    session_id_update=lambda value: setattr(session, "orchestrator_session_id", value),
                )
                self.assertTrue(harness.is_agent_error(first))
                self.assertEqual(session.orchestrator_session_id, "fresh-2")
                second = harness.call_agent(
                    harness.ORCHESTRATOR_ROLE, "turn two",
                    session_id_override=session.orchestrator_session_id,
                    session_id_update=lambda value: setattr(session, "orchestrator_session_id", value),
                )
            self.assertFalse(harness.is_agent_error(second))
            self.assertEqual([sid for _, sid in calls], ["original", "fresh-1", "fresh-2"])
            self.assertIn("fresh-2", calls[2][0])

    def test_checkpoint_loader_uses_safe_pickle_default_and_safetensors(self):
        class Tensor:
            shape = (1,)
            dtype = "float"

            def float(self):
                return self

        calls = []
        fake_torch = types.ModuleType("torch")

        def fake_load(path, **kwargs):
            calls.append((path, kwargs))
            return {"weight": Tensor()}

        fake_torch.load = fake_load
        with mock.patch.dict(sys.modules, {"torch": fake_torch}):
            checkpoint._load_state_dict("model.pt")
            checkpoint._load_state_dict("trusted.pkl", allow_unsafe_pickle=True)
        self.assertTrue(calls[0][1]["weights_only"])
        self.assertFalse(calls[1][1]["weights_only"])
        self.assertEqual(calls[0][1]["map_location"], "cpu")

        package = types.ModuleType("safetensors")
        safe_torch = types.ModuleType("safetensors.torch")
        safe_torch.load_file = mock.Mock(return_value={"weight": Tensor()})
        package.torch = safe_torch
        with mock.patch.dict(sys.modules, {"safetensors": package, "safetensors.torch": safe_torch}):
            checkpoint._load_state_dict("model.safetensors")
        safe_torch.load_file.assert_called_once_with("model.safetensors")
        self.assertEqual(len(calls), 2)


class SkillContractTests(unittest.TestCase):
    def test_root_skill_keeps_commands_internal_and_user_requests_natural_language(self):
        text = (ROOT / "easy-auto-research/SKILL.md").read_text()
        self.assertNotIn("## CLI reference", text)
        self.assertIn("The human interface is natural language only.", text)
        self.assertIn("Never instruct the user to run, copy,", text)
        self.assertNotRegex(text, r"(?m)^```bash$")
        command_blocks = list(re.finditer(r"(?ms)^> ```bash\n.*?^> ```$", text))
        self.assertTrue(command_blocks)
        for block in command_blocks:
            preceding = text[max(0, block.start() - 300):block.start()]
            self.assertIn("INTERNAL IMPLEMENTATION — SKILL AGENT ONLY", preceding)

    def _run_json(self, script, *args):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        with tempfile.TemporaryDirectory() as td:
            result = subprocess.run(
                [sys.executable, str(script), *args], cwd=td, env=env,
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @unittest.skipIf(importlib.util.find_spec("torch") is None, "torch is not installed")
    def test_inspect_batch_json_includes_diagnostics(self):
        script = ROOT / "easy-auto-research/scripts/Skills/inspect-batch/scripts/inspect_batch.py"
        with tempfile.TemporaryDirectory() as td:
            Path(td, "fixture_adapter.py").write_text(
                "import torch\n"
                "from torch.utils.data import TensorDataset\n"
                "def make_dataset():\n"
                "    x = torch.tensor([[[0.0]], [[2.0]], [[3.0]], [[4.0]], [[5.0]]])\n"
                "    y = torch.tensor([0, 0, 0, 0, 1])\n"
                "    return TensorDataset(x, y)\n"
            )
            report = self._run_json(
                script, "--sys-path", td, "--adapter", "fixture_adapter:make_dataset",
                "--batch-size", "5", "--json",
            )
        self.assertEqual(report["label_imbalance_ratio"], 4.0)
        self.assertEqual([item["level"] for item in report["diagnostics"]],
                         ["warning", "warning"])
        self.assertIn("one random batch", report["note"].lower())

    def test_estimate_vram_json_includes_verdict_and_suggestion(self):
        script = ROOT / "easy-auto-research/scripts/Skills/estimate-vram/scripts/estimate_vram.py"
        report = self._run_json(
            script, "--params", "500000000", "--batch-size", "64", "--optimizer", "adamw",
            "--dtype", "bf16", "--act-bytes-per-sample", "104857600", "--gpu-gb", "8",
            "--json",
        )
        self.assertEqual(report["verdict"], "likely_oom")
        self.assertGreater(report["over_by_gb"], 0)
        self.assertIsInstance(report["suggested_batch_size"], int)
        self.assertGreaterEqual(report["suggested_batch_size"], 1)

    def test_analyze_hpo_json_is_complete_and_rejects_invalid_runs(self):
        script = ROOT / "easy-auto-research/scripts/Skills/analyze-hpo/scripts/analyze_hpo.py"
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
            base = Path(td)
            for number, lr, score in ((1, 0.1, 0.5), (2, 0.2, 0.6), (3, 0.3, 0.7)):
                run = base / f"V{number}_run"
                run.mkdir()
                (run / "config.json").write_text(json.dumps({"lr": lr}))
                (run / "results.json").write_text(json.dumps({"val_acc": score}))
            malformed = base / "V999junk"
            malformed.mkdir()
            (malformed / "results.json").write_text('{"val_acc": 99}')
            outside = Path(outside_td)
            (outside / "results.json").write_text('{"val_acc": 100}')
            (base / "V4_escape").symlink_to(outside, target_is_directory=True)

            report = self._run_json(
                script, "--base-dir", td, "--metric-key", "val_acc",
                "--hparam-keys", "lr", "--json",
            )
        self.assertEqual([run["run"] for run in report["runs"]],
                         ["V1_run", "V2_run", "V3_run"])
        self.assertEqual(report["best_run"]["run"], "V3_run")
        self.assertEqual(report["correlations"][0]["hparam"], "lr")
        self.assertAlmostEqual(report["correlations"][0]["pearson_r"], 1.0)

    def test_reset_project_blocks_are_scoped_rechecked_and_symlink_safe(self):
        skill_text = (ROOT / "easy-auto-research/scripts/Skills/reset-project/SKILL.md").read_text()
        bash_blocks = re.findall(r"```bash\n(.*?)```", skill_text, re.DOTALL)
        self.assertGreaterEqual(len(bash_blocks), 2)

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
            run_root = Path(td)
            project = run_root / "project"
            project.mkdir()
            for name in ("harness.py", "init.py"):
                (project / name).write_text("pass\n")
            (project / "templates").mkdir()
            (project / "agents").mkdir()
            (project / "agents/.sessions.json").write_text("{}")
            (project / "uuid_ledger.jsonl").write_text("preserve\n")
            (project / "human_comments.txt").write_text("comment\n")
            (project / "human_interrupt.txt").write_text("interrupt\n")
            workspace = run_root / "WorkSpace"
            workspace.mkdir()
            (workspace / "artifact").write_text("generated\n")

            preview = bash_blocks[0].replace("/absolute/path/to/project", str(project))
            result = subprocess.run(
                ["bash", "-c", preview], cwd=project, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Running project processes to review:\nProject entries:", result.stdout)

            destructive = bash_blocks[1].replace("/absolute/path/to/project", str(project))
            experiment_script = project / "experiment.py"
            experiment_script.write_text("import time\ntime.sleep(60)\n")
            live = subprocess.Popen([sys.executable, str(experiment_script)], cwd=project)
            try:
                result = subprocess.run(
                    ["bash", "-c", destructive], cwd=project,
                    capture_output=True, text=True, check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("reset aborted", result.stderr)
                self.assertTrue(workspace.exists())
            finally:
                live.terminate()
                live.wait(timeout=5)

            outside = Path(outside_td) / "outside.txt"
            outside.write_text("must survive\n")
            (project / "human_comments.txt").unlink()
            (project / "human_comments.txt").symlink_to(outside)
            result = subprocess.run(
                ["bash", "-c", destructive], cwd=project,
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing unsafe queue path", result.stderr)
            self.assertEqual(outside.read_text(), "must survive\n")
            self.assertTrue(workspace.exists())
            (project / "human_comments.txt").unlink()
            (project / "human_comments.txt").write_text("comment\n")
            sidecars = tuple(
                project / f".{channel}.queue.{kind}"
                for channel in ("human_comments.txt", "human_interrupt.txt")
                for kind in ("lock", "state")
            )
            for sidecar in sidecars:
                sidecar.write_text("{}\n")
            unrelated_sidecar = project / ".human_comments.txt.queue.user-notes"
            unrelated_sidecar.write_text("preserve\n")
            result = subprocess.run(
                ["bash", "-c", destructive], cwd=project,
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(workspace.exists())
            self.assertEqual((project / "uuid_ledger.jsonl").read_text(), "preserve\n")
            self.assertEqual((project / "human_comments.txt").read_bytes(), b"")
            self.assertEqual((project / "human_interrupt.txt").read_bytes(), b"")
            self.assertFalse(any(sidecar.exists() for sidecar in sidecars))
            self.assertEqual(unrelated_sidecar.read_text(), "preserve\n")

    def _reset_project_destructive_block(self, project):
        skill_text = (ROOT / "easy-auto-research/scripts/Skills/reset-project/SKILL.md").read_text()
        bash_blocks = re.findall(r"```bash\n(.*?)```", skill_text, re.DOTALL)
        self.assertGreaterEqual(len(bash_blocks), 2)
        return bash_blocks[1].replace("/absolute/path/to/project", str(project))

    def _post_init_project(self, run_root):
        project = run_root / "project"
        project.mkdir()
        for name in ("harness.py", "init.py"):
            (project / name).write_text("pass\n")
        (project / "templates").mkdir()
        (project / "agents").mkdir()
        (project / "goal.md").write_text("goal\n")
        (project / "uuid_ledger.jsonl").write_text("preserve\n")
        return project

    def test_reset_project_treats_absent_queue_files_as_empty(self):
        with tempfile.TemporaryDirectory() as td:
            run_root = Path(td)
            project = self._post_init_project(run_root)
            workspace = run_root / "WorkSpace"
            workspace.mkdir()
            (workspace / "artifact").write_text("generated\n")
            destructive = self._reset_project_destructive_block(project)

            # A valid post-init project may never have received human input, so
            # neither queue journal exists yet; absence must not block the reset.
            self.assertFalse((project / "human_comments.txt").exists())
            self.assertFalse((project / "human_interrupt.txt").exists())
            result = subprocess.run(
                ["bash", "-c", destructive], cwd=project,
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(workspace.exists())
            self.assertFalse((project / "human_comments.txt").exists())
            self.assertFalse((project / "human_interrupt.txt").exists())
            self.assertEqual((project / "uuid_ledger.jsonl").read_text(), "preserve\n")
            self.assertEqual((project / "goal.md").read_text(), "goal\n")

            # One present and one absent queue is also a valid post-init state.
            workspace.mkdir()
            (project / "human_interrupt.txt").write_text("interrupt\n")
            result = subprocess.run(
                ["bash", "-c", destructive], cwd=project,
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(workspace.exists())
            self.assertEqual((project / "human_interrupt.txt").read_bytes(), b"")
            self.assertFalse((project / "human_comments.txt").exists())

    def test_reset_project_still_refuses_links_and_special_queue_paths(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
            run_root = Path(td)
            project = self._post_init_project(run_root)
            destructive = self._reset_project_destructive_block(project)
            outside = Path(outside_td) / "outside.txt"
            outside.write_text("must survive\n")
            queue = project / "human_comments.txt"

            def assert_refused():
                workspace = run_root / "WorkSpace"
                if not workspace.exists():
                    workspace.mkdir()
                result = subprocess.run(
                    ["bash", "-c", destructive], cwd=project,
                    capture_output=True, text=True, check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Refusing unsafe queue path", result.stderr)
                self.assertEqual(outside.read_text(), "must survive\n")
                self.assertTrue(workspace.exists())

            queue.symlink_to(outside)
            assert_refused()
            queue.unlink()

            os.link(outside, queue)
            assert_refused()
            queue.unlink()

            os.mkfifo(queue)
            assert_refused()
            queue.unlink()

            queue.mkdir()
            assert_refused()
            queue.rmdir()

            queue.write_text("comment\n")
            sidecar = project / ".human_comments.txt.queue.state"
            sidecar.symlink_to(outside)
            assert_refused()
            self.assertEqual(queue.read_bytes(), b"comment\n")

    def test_documented_skill_commands_resolve_without_unset_variables(self):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        documented = set()
        for skill_md in (ROOT / "easy-auto-research/scripts/Skills").rglob("SKILL.md"):
            text = skill_md.read_text()
            documented.update(re.findall(r"easy-auto-research/\S+?\.py", text))
        self.assertGreaterEqual(len(documented), 7)
        with tempfile.TemporaryDirectory() as td:
            for relative in sorted(documented):
                script = ROOT / relative
                self.assertTrue(script.is_file(), relative)
                result = subprocess.run(
                    [sys.executable, str(script), "--help"], cwd=td, env=env,
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, f"{relative}: {result.stderr}")


class CliAndReleaseTests(unittest.TestCase):
    def test_argparse_defaults_and_opt_in_wiring(self):
        init_args = init.build_arg_parser().parse_args([])
        harness_args = harness.build_arg_parser().parse_args([])
        self.assertFalse(init_args.run_import_preflight)
        self.assertFalse(init_args.dangerously_skip_agent_permissions)
        self.assertFalse(harness_args.dangerously_skip_agent_permissions)
        self.assertTrue(init.build_arg_parser().parse_args(["--fresh"]).fresh)
        with self.assertRaises(SystemExit):
            init.build_arg_parser().parse_args(["--fresh", "--resume"])
        with self.assertRaises(SystemExit):
            init.build_arg_parser().parse_args(["--cli", "claude"])
        with self.assertRaises(SystemExit):
            harness.build_arg_parser().parse_args(["--cli", "claude"])
        self.assertEqual(init.build_claude_cmd()[0], "claude")
        self.assertEqual(harness.build_claude_cmd()[0], "claude")

        with mock.patch.object(harness, "SKIP_AGENT_PERMISSIONS", True):
            self.assertIn("--dangerously-skip-permissions", harness.build_claude_cmd())
        with mock.patch.object(init, "SKIP_AGENT_PERMISSIONS", True):
            self.assertIn("--dangerously-skip-permissions", init.build_claude_cmd())

    def test_init_preflight_only_runs_when_requested(self):
        with tempfile.TemporaryDirectory() as td:
            project = str(Path(td) / "project")
            data = {"codebase_path": td, "what": "x", "dos": ["x"], "donts": ["y"]}
            common = ["--fresh", "--output-dir", project, "--model", "m"]
            patches = (
                mock.patch.object(init, "check_cli_available", return_value=True),
                mock.patch.object(init, "validate_model_with_cli", return_value=(True, "")),
                mock.patch.object(init, "_write_config"),
                mock.patch.object(init, "prepare_spec", return_value=("spec.json", data)),
                mock.patch.object(init, "run_interviewer"),
                mock.patch.object(init, "preflight_codebase_import"),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5] as preflight:
                init.main(common)
                preflight.assert_not_called()
                init.main(common + ["--run-import-preflight"])
                preflight.assert_called_once_with(project, data)

    def test_watch_root_no_match_and_multiple_match(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"HOME": td}):
            sid = "session"
            first = Path(td) / ".claude/projects/x" / f"{sid}.jsonl"
            second = Path(td) / ".claude/projects/y" / f"{sid}.jsonl"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_text("")
            second.write_text("")
            self.assertIn(watch.find_jsonl(sid), {str(first), str(second)})
            with self.assertRaises(SystemExit):
                watch.find_jsonl("missing")

    def test_installer_is_executable_and_replaces_existing_copy(self):
        installer = ROOT / "install.sh"
        self.assertTrue(installer.stat().st_mode & stat.S_IXUSR)
        with tempfile.TemporaryDirectory() as td:
            destination = Path(td) / "skills"
            stale = destination / "easy-auto-research/stale.txt"
            stale.parent.mkdir(parents=True)
            stale.write_text("old")
            result = subprocess.run(
                [str(installer), str(destination)], capture_output=True, text=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(stale.exists())
            self.assertTrue((destination / "easy-auto-research/SKILL.md").is_file())

    def test_installer_excludes_runtime_artifacts_from_polluted_source(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = Path(td) / "checkout"
            fixture.mkdir()
            shutil.copy2(ROOT / "install.sh", fixture / "install.sh")
            shutil.copytree(ROOT / "easy-auto-research", fixture / "easy-auto-research")
            source = fixture / "easy-auto-research"
            artifacts = (
                source / "scripts/human_interrupt.txt",
                source / "scripts/.human_interrupt.txt.queue.lock",
                source / "goal.md",
                source / "research_spec.json",
                source / ".knowledge_digest.md",
                source / "agents/planner.md",
                source / "WorkSpace/V2_trial/training.log",
                source / "scripts/__pycache__/runtime.pyc",
            )
            for artifact in artifacts:
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text("runtime\n")

            destination = Path(td) / "skills"
            result = subprocess.run(
                [str(fixture / "install.sh"), str(destination)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = destination / "easy-auto-research"
            self.assertTrue((installed / "SKILL.md").is_file())
            for artifact in artifacts:
                relative = artifact.relative_to(source)
                self.assertFalse((installed / relative).exists(), str(relative))

    def test_installer_clean_home_defaults_to_claude_skills(self):
        installer = ROOT / "install.sh"
        with tempfile.TemporaryDirectory() as td:
            env = {**os.environ, "HOME": td, "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run(
                [str(installer)], capture_output=True, text=True, check=False, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = Path(td) / ".claude/skills/easy-auto-research/SKILL.md"
            self.assertTrue(installed.is_file())

    def test_installer_rejects_source_and_nested_destinations(self):
        installer = ROOT / "install.sh"
        source_marker = ROOT / "easy-auto-research/SKILL.md"
        for destination in (ROOT, ROOT / "easy-auto-research"):
            result = subprocess.run(
                [str(installer), str(destination)], capture_output=True, text=True, check=False
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must not be the source tree", result.stderr)
            self.assertTrue(source_marker.is_file())
        with tempfile.TemporaryDirectory() as td:
            alias = Path(td) / "repo-alias"
            alias.symlink_to(ROOT, target_is_directory=True)
            result = subprocess.run(
                [str(alias / "install.sh"), str(ROOT)], capture_output=True, text=True, check=False
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must not be the source tree", result.stderr)
            self.assertTrue(source_marker.is_file())

    def test_release_tree_and_brand(self):
        self.assertTrue(sys.dont_write_bytecode)
        self.assertEqual(PREEXISTING_BYTECODE_ARTIFACTS, ())
        skill = ROOT / "easy-auto-research"
        self.assertEqual((skill / "VERSION").read_text(), "1.0.0\n")
        humanizer_license = skill / "scripts/Skills/humanizer/LICENSE"
        self.assertTrue(humanizer_license.is_file())
        self.assertIn("Copyright (c) 2025 Siqi Chen", humanizer_license.read_text())
        self.assertIn("Copyright (c) 2026 Juncheng Wang", (ROOT / "LICENSE").read_text())
        self.assertTrue((ROOT / "NOTICE").is_file())
        self.assertEqual(
            {p.stem for p in (skill / "templates").glob("*.md") if p.stem not in {"group_duty", "interviewer"}},
            {"planner", "executer", "verifier", "evaluator", "orchestrator", "secretary"},
        )
        expected_images = {
            ROOT / "assets/easy-auto-research-logo.png",
            ROOT / "assets/easy-auto-research-teaser.png",
        }
        self.assertEqual(set(ROOT.rglob("*.png")), expected_images)
        self.assertFalse(any(ROOT.rglob("*.zip")))
        self.assertFalse(any(ROOT.rglob("*.tar.gz")))
        logo_image = ROOT / "assets/easy-auto-research-logo.png"
        teaser_image = ROOT / "assets/easy-auto-research-teaser.png"
        self.assertGreater(logo_image.stat().st_size, 10_000)
        self.assertGreater(teaser_image.stat().st_size, 100_000)
        self.assertIn("assets/easy-auto-research-logo.png", (ROOT / "README.md").read_text())
        self.assertIn("assets/easy-auto-research-teaser.png", (ROOT / "README.md").read_text())
        self.assertFalse(any(ROOT.rglob("*.pyc")))
        self.assertFalse(any(p.is_dir() for p in ROOT.rglob("__pycache__")))
        self.assertFalse(any(ROOT.rglob(".pytest_cache")))
        runtime_names = {
            "human_comments.txt", "human_interrupt.txt", "agent_thoughts.log",
            "uuid_ledger.jsonl", ".sessions.json", ".knowledge_digest.md",
            ".plan_ledger.jsonl", ".metric_ledger.jsonl", ".last_phenomena.md",
            ".ar_model",
        }
        runtime_artifacts = [path for path in ROOT.rglob("*") if path.name in runtime_names]
        runtime_artifacts.extend(ROOT.rglob(".human_*.txt.queue.*"))
        self.assertEqual(runtime_artifacts, [])
        ignored = (ROOT / ".gitignore").read_text().splitlines()
        self.assertIn(".pytest_cache/", ignored)
        self.assertIn(".ruff_cache/", ignored)
        self.assertIn("README-preview.html", ignored)
        self.assertFalse(any(p.is_dir() for p in ROOT.rglob(".pytest_cache")))
        self.assertFalse(any(p.is_dir() for p in ROOT.rglob(".ruff_cache")))
        runtime_artifact_names = {
            "WorkSpace", "CycleReport", "solutions", "agents", "agent_history",
            "agent_interactions", "agent_thoughts.log", "research_log.md",
            "uuid_ledger.jsonl", ".sessions.json", ".knowledge_digest.md",
            ".plan_ledger.jsonl", ".metric_ledger.jsonl", ".last_phenomena.md",
            "human_comments.txt", "human_interrupt.txt", "goal.md", "PROJECT_BRIEF.md",
            "research_spec.json", "PREFLIGHT.md", ".ar_model",
        }
        leaked = [
            path.relative_to(skill)
            for path in skill.rglob("*")
            if path.name in runtime_artifact_names
            or path.name.startswith(".training_pid")
            or re.fullmatch(r"\.human_(?:comments|interrupt)\.txt\.queue\..+", path.name)
        ]
        self.assertEqual(leaked, [])
        self.assertNotIn("--dangerously-skip-permissions", harness.build_claude_cmd())
        orchestrator = (skill / "templates/orchestrator.md").read_text()
        executer = (skill / "templates/executer.md").read_text()
        for template in (orchestrator, executer):
            self.assertIn("integer `pid`", template)
            self.assertIn("string `start_time`", template)
            self.assertIn("integer `uid`", template)
        self.assertNotIn("echo $! > .training_pid", orchestrator)
        skill_text = (skill / "SKILL.md").read_text()
        self.assertIn("# Easy-Auto-Research for Deep Learning", skill_text)
        self.assertIn("name: easy-auto-research", skill_text)

        prohibited_terms = [
            "ten" + "cent",
            "腾" + "讯",
            "code" + "buddy",
            "c" + "nb",
            "ten" + "cent64",
            "advanced micro" + " devices",
            "r" + "ocm",
            "r" + "ocm-smi",
        ]
        vendor_words = ["a" + "md", "r" + "oc"]
        prohibited = re.compile(
            "|".join(re.escape(term) for term in prohibited_terms)
            + r"|\b(?:" + "|".join(vendor_words) + r")\b",
            re.IGNORECASE,
        )
        public_files = [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]
        for path in public_files:
            self.assertIsNone(prohibited.search(str(path.relative_to(ROOT))), str(path))
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            self.assertIsNone(prohibited.search(text), str(path))


if __name__ == "__main__":
    unittest.main()
