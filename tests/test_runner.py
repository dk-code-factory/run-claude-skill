import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/run-claude-skill/scripts/run.py"
SPEC = importlib.util.spec_from_file_location("runner", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def envelope(result="Review complete", **kwargs):
    return {"type": "result", "subtype": "success", "is_error": False,
            "result": result, **kwargs}


class ResultTests(unittest.TestCase):
    def test_success_preserves_findings_without_imposing_a_schema(self):
        for text in ["No findings", "Standards: minor bug", '{"custom_review": []}',
                     "Authentication failed is the expected error in this unit test."]:
            with self.subTest(text=text):
                result = runner.decode_result(json.dumps(envelope(text)), "warning", 0)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["output"], text)

    def test_failure_categories_have_recovery_and_nonzero_exit(self):
        cases = [
            ("Not logged in · Please run /login", "auth_required"),
            ("authentication_failed", "auth_required"),
            ("API Error: 401 invalid OAuth token", "auth_required"),
            ("OAuth token expired", "auth_required"),
            ("You've hit your limit · resets Oct 22 at 3pm", "usage_limit"),
            ("Session usage limit reached", "usage_limit"),
            ("Out of tokens", "usage_limit"),
            ("Credit balance is too low", "usage_limit"),
            ("Prompt is too long", "context_limit"),
            ("context_length_exceeded", "context_limit"),
            ("max_output_tokens", "output_limit"),
            ("error_max_turns", "turn_limit"),
            ("error_max_budget_usd", "budget_limit"),
            ("permission_denied: Read", "permission_denied"),
            ("403 organization access denied", "permission_denied"),
            ("429 rate_limit_error", "rate_limited"),
            ("ECONNRESET", "network_error"),
            ("fetch failed", "network_error"),
            ("529 overloaded", "provider_error"),
            ("Unknown skill: missing", "skill_not_found"),
            ("Unexpected crash", "execution_error"),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                result = runner.decode_result(json.dumps(envelope(message, errors=[message], is_error=True,
                                                                       subtype="error_during_execution")), "", 1)
                self.assertEqual(result["status"], expected)
                self.assertFalse(result["success"])
                self.assertTrue(result["recovery"])
                self.assertNotEqual(runner.EXIT_CODES[expected], 0)

    def test_failure_exit_overrides_success_envelope(self):
        result = runner.decode_result(json.dumps(envelope()), "process crashed", 12)
        self.assertFalse(result["success"])
        self.assertEqual(result["claude_exit_code"], 12)

    def test_success_exit_does_not_hide_account_limit(self):
        result = runner.decode_result(json.dumps(envelope("You've hit your limit; resets 9pm")), "", 0)
        self.assertEqual(result["status"], "usage_limit")
        self.assertIn("9pm", result["message"])

    def test_structured_errors_and_subtype_are_examined(self):
        for fields, expected in [
            ({"subtype": "error_max_turns"}, "turn_limit"),
            ({"errors": [{"type": "authentication_failed"}]}, "auth_required"),
        ]:
            result = runner.decode_result(json.dumps(envelope("", is_error=True, **fields)), "", 0)
            self.assertEqual(result["status"], expected)

    def test_partial_review_text_cannot_override_cli_failure_reason(self):
        partial = "401 invalid credentials: this auth test has a bug."
        for subtype, errors, expected in [
            ("error_max_turns", [], "turn_limit"),
            ("error_max_budget_usd", [], "budget_limit"),
            ("error_during_execution", ["ECONNRESET"], "network_error"),
        ]:
            with self.subTest(subtype=subtype):
                text = json.dumps(envelope(partial, is_error=True, subtype=subtype, errors=errors))
                result = runner.decode_result(text, "", 1)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["output"], partial)

    def test_per_minute_rate_limit_is_not_account_exhaustion(self):
        text = "Number of request tokens has exceeded your per-minute rate limit"
        result = runner.decode_result(json.dumps(envelope("", is_error=True, errors=[text])), "", 1)
        self.assertEqual(result["status"], "rate_limited")

    def test_unicode_apostrophe_limit_notice_is_a_failure(self):
        result = runner.decode_result(json.dumps(envelope("You’ve hit your limit; resets 9pm")), "", 0)
        self.assertEqual(result["status"], "usage_limit")

    def test_raw_auth_failure_on_stdout_has_token_recipe(self):
        result = runner.decode_result("Not logged in. Please run /login", "", 1)
        self.assertEqual(result["status"], "auth_required")
        steps = "\n".join(result["recovery"])
        self.assertIn("claude setup-token", steps)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", steps)
        self.assertIn("same shell", steps)

    def test_missing_invalid_empty_and_contradictory_envelopes_fail(self):
        for text in ["", "looks done", "{}", "[]", "null", json.dumps(envelope("")),
                     json.dumps(envelope(None)), '{"type":"result","result":"done"}']:
            with self.subTest(text=text):
                result = runner.decode_result(text, "", 0)
                self.assertFalse(result["success"])

    def test_array_envelope_uses_final_result(self):
        text = json.dumps([{"type": "system"}, envelope("Done")])
        self.assertEqual(runner.decode_result(text, "", 0)["output"], "Done")

    def test_permission_denials_keep_partial_result_but_fail(self):
        text = json.dumps(envelope("Partial review", permission_denials=[{"tool_name": "Read"}]))
        result = runner.decode_result(text, "", 0)
        self.assertEqual(result["status"], "permission_denied")
        self.assertEqual(result["output"], "Partial review")
        self.assertIn("Read", result["diagnostic"])

    def test_credentials_are_redacted_from_success_and_failure(self):
        secret = "opaque-long-value-for-testing"
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": secret}):
            for value in [secret, "sk-ant-oat01-EXAMPLE-not-a-real-token", "Bearer opaque-secret"]:
                with self.subTest(value=value):
                    for failed in [False, True]:
                        text = json.dumps(envelope(value, is_error=failed))
                        result = runner.decode_result(text, value if failed else "", int(failed))
                        self.assertNotIn(value, json.dumps(result))
                        self.assertIn("[REDACTED]", json.dumps(result))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / "checkout"
        self.cwd.mkdir()
        (self.cwd / ".git").mkdir()
        self.config = self.root / "config"
        self.config.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "invocation.json"
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.config), PATH=str(self.bin),
                        FAKE_LOG=str(self.log), FAKE_REPLY=json.dumps(envelope()))
        # The fake CLI is a real separate process; no live Claude or credentials are used.
        fake = self.bin / "claude"
        fake.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, subprocess, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
root = pathlib.Path(args[args.index('--plugin-dir') + 1])
record = {'args': args, 'prompt': prompt, 'cwd': os.getcwd(), 'pid': os.getpid(),
          'nested_marker': os.environ.get('CLAUDECODE'),
          'plugin': json.loads((root / '.claude-plugin/plugin.json').read_text())}
record['support'] = [p.read_text() for p in root.glob('skills/*/references/evidence.txt')]
if os.environ.get('FAKE_HANG'):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])
    record['child_pid'] = child.pid
pathlib.Path(os.environ['FAKE_LOG']).write_text(json.dumps(record))
if os.environ.get('FAKE_HANG'): time.sleep(90)
print(os.environ.get('FAKE_REPLY', ''))
print(os.environ.get('FAKE_STDERR', ''), file=sys.stderr)
sys.exit(int(os.environ.get('FAKE_EXIT', '0')))
''')
        fake.chmod(0o755)
        self.plugin = self.make_plugin(self.root / "plugin")
        self.registry({"reviewer@test-market": [{"scope": "user", "installPath": str(self.plugin)}]})

    def make_plugin(self, path, *, name="reviewer", skill="review", custom=False, command=False):
        path.mkdir()
        (path / ".claude-plugin").mkdir()
        data = {"name": name}
        if command:
            target = path / "commands" / f"{skill}.md"
        else:
            base = "nested/engineering" if custom else "skills"
            target = path / base / skill / "SKILL.md"
            if custom:
                data["skills"] = f"./{base}/{skill}"
        target.parent.mkdir(parents=True)
        target.write_text(f"---\nname: {skill}\n---\nReview the selected scope.\n")
        (path / ".claude-plugin/plugin.json").write_text(json.dumps(data))
        return path

    def registry(self, plugins):
        path = self.config / "plugins/installed_plugins.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"plugins": plugins}))

    def cli(self, *options, env=None, skill="reviewer:review", text_mode=False):
        args = [sys.executable, str(SCRIPT), "--skill", skill, "--cwd", str(self.cwd), *options]
        if not text_mode:
            args.append("--json")
        proc = subprocess.run(args, capture_output=True, text=True,
                              env=env or self.env, timeout=10)
        if text_mode:
            return proc
        self.assertEqual(proc.stderr, "", proc.stderr)
        return proc, json.loads(proc.stdout)

    def test_plugin_runs_with_literal_prompt_and_controlled_permissions(self):
        marker = self.cwd / "must-not-exist"
        prompt = f"Review `touch {marker}` $(touch {marker})\nquoted 'arguments'"
        proc, result = self.cli("--prompt", prompt)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(result["status"], "ok")
        record = json.loads(self.log.read_text())
        self.assertEqual(record["prompt"], "/reviewer:review " + prompt)
        self.assertEqual(record["cwd"], str(self.cwd))
        self.assertEqual(record["args"][record["args"].index("--permission-mode") + 1], "plan")
        self.assertIn("--permission-prompts", record["args"])
        self.assertIn("--no-session-persistence", record["args"])
        self.assertFalse(marker.exists())

    def test_stdin_prompt_and_text_output(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), "--skill", "reviewer:review",
                               "--cwd", str(self.cwd), "--prompt", "-"],
                              input="Review\nthis", text=True, capture_output=True, env=self.env, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "Review complete")
        self.assertEqual(json.loads(self.log.read_text())["prompt"], "/reviewer:review Review\nthis")

    def test_independent_child_can_run_when_parent_is_claude(self):
        proc, result = self.cli(env=dict(self.env, CLAUDECODE="1"))
        self.assertEqual(proc.returncode, 0)
        self.assertIsNone(json.loads(self.log.read_text())["nested_marker"])

    def test_launcher_cannot_select_itself(self):
        proc, result = self.cli(skill="run-claude-skill:run-claude-skill")
        self.assertEqual(result["status"], "invalid_request")
        self.assertFalse(self.log.exists())

    def test_explicit_local_skill_loads_complete_folder_without_installing_it(self):
        source = self.root / "local-review"
        source.mkdir()
        (source / "SKILL.md").write_text("---\nname: local-review\n---\nRead references/evidence.txt")
        (source / "references").mkdir()
        (source / "references/evidence.txt").write_text("supporting instructions")
        proc, result = self.cli("--skill-file", str(source), skill="local-review")
        self.assertEqual(proc.returncode, 0)
        record = json.loads(self.log.read_text())
        self.assertEqual(record["support"], ["supporting instructions"])
        self.assertTrue(record["prompt"].endswith(":local-review"))
        self.assertFalse(Path(record["args"][record["args"].index("--plugin-dir") + 1]).exists())
        self.assertFalse((self.config / "skills/local-review").exists())

    def test_personal_skill_wins_over_project_skill(self):
        for root in [self.config, self.cwd / ".claude"]:
            path = root / "skills/inspect/SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text("---\nname: inspect\n---\nInspect")
        with patch.dict(os.environ, self.env):
            result = runner.resolve("inspect", cwd=self.cwd)
        self.assertEqual(result["file"], self.config / "skills/inspect/SKILL.md")

    def test_custom_manifest_and_legacy_plugin_command(self):
        for kind in ["custom", "command"]:
            with self.subTest(kind=kind):
                plugin = self.make_plugin(self.root / kind, **{kind: True})
                proc, result = self.cli("--plugin-dir", str(plugin))
                self.assertEqual(proc.returncode, 0)

    def test_unrelated_project_installation_is_not_selected(self):
        self.registry({"reviewer@market": [{"scope": "project", "projectPath": str(self.root / "elsewhere"),
                                            "installPath": str(self.plugin)}]})
        proc, result = self.cli()
        self.assertEqual(result["status"], "skill_not_found")
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_ambiguous_installation_requires_explicit_root(self):
        other = self.make_plugin(self.root / "other")
        self.registry({"reviewer@a": [{"installPath": str(self.plugin)}],
                       "reviewer@b": [{"installPath": str(other)}]})
        proc, result = self.cli()
        self.assertEqual(result["status"], "skill_ambiguous")
        self.assertFalse(self.log.exists())
        proc, result = self.cli("--plugin-dir", str(other))
        self.assertEqual(proc.returncode, 0)

    def test_missing_skill_has_source_specific_install_recipe(self):
        proc, result = self.cli("--marketplace", "publisher/catalog", skill="missing:review")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("claude plugin install missing --marketplace publisher/catalog", " ".join(result["recovery"]))
        self.assertFalse(self.log.exists())

    def test_missing_skill_without_source_does_not_invent_repository(self):
        proc, result = self.cli(skill="missing:review")
        self.assertIn("publisher or the user", " ".join(result["recovery"]))
        self.assertNotIn("github.com/missing", json.dumps(result))

    def test_stale_registry_has_exact_plugin_repair_recipe(self):
        self.registry({"reviewer@known-market": [{"installPath": str(self.root / "gone")} ]})
        proc, result = self.cli()
        self.assertIn("reviewer@known-market", " ".join(result["recovery"]))

    def test_missing_cli_has_install_recipe_without_launch(self):
        proc, result = self.cli(env=dict(self.env, PATH=""))
        self.assertEqual(result["status"], "claude_not_found")
        self.assertIn("https://claude.ai/install.sh", " ".join(result["recovery"]))
        self.assertFalse(self.log.exists())

    def test_invalid_inputs_are_structured_and_do_not_launch(self):
        for options in [("--timeout", "nan"), ("--timeout", "inf"), ("--timeout", "0"),
                        ("--max-turns", "0"), ("--timeout", "word"), ("--unknown-flag",),
                        ("--permission-mode", "bypassPermissions"), ("--cwd", str(self.root / "gone"))]:
            with self.subTest(options=options):
                proc, result = self.cli(*options)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(result["status"], "invalid_request")
                self.assertFalse(self.log.exists())

    def test_manifest_cannot_escape_plugin_root(self):
        (self.plugin / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "reviewer", "skills": "../"}))
        proc, result = self.cli()
        self.assertEqual(result["status"], "invalid_request")
        self.assertFalse(self.log.exists())

    def test_malformed_registry_reports_error_without_traceback(self):
        (self.config / "plugins/installed_plugins.json").write_text("{broken")
        proc, result = self.cli()
        self.assertEqual(result["status"], "invalid_request")
        self.assertFalse(self.log.exists())

    def test_model_failure_runs_once_and_never_reports_clean_review(self):
        env = dict(self.env, FAKE_REPLY=json.dumps(envelope("Not logged in", is_error=True)), FAKE_EXIT="1")
        proc, result = self.cli(env=env)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(result["status"], "auth_required")
        self.assertFalse(result["success"])
        text = self.cli(env=env, text_mode=True)
        self.assertEqual(text.stdout, "")
        self.assertIn("claude setup-token", text.stderr)

    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    def test_timeout_stops_process_group(self):
        proc, result = self.cli("--timeout", "0.5", env=dict(self.env, FAKE_HANG="1"))
        self.assertEqual(proc.returncode, 6)
        self.assertEqual(result["status"], "timeout")
        self.assert_processes_stopped()

    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    def test_termination_stops_child_and_reports_interruption(self):
        self.check_interruption(signal.SIGTERM)

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "POSIX terminal hangup")
    def test_terminal_hangup_stops_child_and_reports_interruption(self):
        self.check_interruption(signal.SIGHUP)

    def check_interruption(self, interruption):
        proc = subprocess.Popen([sys.executable, str(SCRIPT), "--skill", "reviewer:review",
                                 "--cwd", str(self.cwd), "--json"],
                                env=dict(self.env, FAKE_HANG="1"), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 5
        while not self.log.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.log.exists())
        proc.send_signal(interruption)
        stdout, stderr = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 130)
        self.assertEqual(json.loads(stdout)["status"], "interrupted")
        self.assert_processes_stopped()

    def assert_processes_stopped(self):
        record = json.loads(self.log.read_text())
        for pid in [record["pid"], record["child_pid"]]:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            # A killed descendant can remain a zombie until init reaps it.
            stat = Path(f"/proc/{pid}/stat")
            if stat.exists():
                self.assertEqual(stat.read_text().split(")", 1)[1].strip()[0], "Z")
            else:
                self.fail(f"Child {pid} is still running")


class PackagingTests(unittest.TestCase):
    def test_both_hosts_select_the_same_self_contained_skill(self):
        for host in ["claude", "codex"]:
            manifest = json.loads((ROOT / f".{host}-plugin/plugin.json").read_text())
            self.assertEqual(manifest["version"], "1.0.0")
            self.assertEqual(manifest["name"], "run-claude-skill")
            self.assertTrue((ROOT / manifest["skills"] / "run-claude-skill/SKILL.md").is_file())
            self.assertTrue((ROOT / manifest["skills"] / "run-claude-skill/scripts/run.py").is_file())


if __name__ == "__main__":
    unittest.main()
