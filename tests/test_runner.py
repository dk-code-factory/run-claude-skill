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
                     "Error: foo.py:10 drops the returned promise.",
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

    def test_result_only_cli_failure_notices_have_specific_recovery(self):
        for notice, expected in [("Out of tokens", "usage_limit"),
                                 ("Not authenticated", "auth_required"),
                                 ("Usage limit reached", "usage_limit"),
                                 ("Rate limit reached", "rate_limited"),
                                 ("No API key found", "auth_required")]:
            with self.subTest(notice=notice):
                result = runner.decode_result(json.dumps(envelope(
                    notice, is_error=True, subtype="error_during_execution")), "", 1)
                self.assertEqual(result["status"], expected)
                self.assertTrue(result["recovery"])

    def test_structured_error_wins_over_auth_text_in_partial_answer(self):
        result = runner.decode_result(json.dumps(envelope(
            "Not authenticated is the wrong UI state", is_error=True,
            errors=["ECONNRESET"])), "", 1)
        self.assertEqual(result["status"], "network_error")

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

    def test_truncation_cannot_expose_credential_prefix(self):
        secret = "opaque-long-secret-for-test-0123456789"
        diagnostic = "a" * 7990 + secret
        with patch.dict(os.environ, {"EXAMPLE_TOKEN": secret}):
            for stdout in ["", json.dumps(envelope("Partial", is_error=True, errors=[diagnostic]))]:
                with self.subTest(stdout=bool(stdout)):
                    result = runner.decode_result(stdout, diagnostic, 1)
                    self.assertNotIn(secret[:10], json.dumps(result))
                    if not stdout:
                        self.assertIn("[REDACTED]", result["message"])

    def test_json_escaped_credentials_are_redacted(self):
        secret = 'testing-only-opaque"secret\\012345-ñ'
        with patch.dict(os.environ, {"EXAMPLE_PASSWORD": secret}):
            for ensure_ascii in [True, False]:
                text = json.dumps(envelope("Partial", is_error=True, errors=[secret]),
                                  ensure_ascii=ensure_ascii)
                failure = runner.decode_result(text, "", 1)
                metadata = json.loads(failure["message"].split("\n", 1)[1])
                self.assertEqual(metadata, ["[REDACTED]"])
                text = json.dumps(envelope("Partial", permission_denials=[{"tool_input": secret}]),
                                  ensure_ascii=ensure_ascii)
                denial = runner.decode_result(text, "", 0)
                self.assertEqual(json.loads(denial["diagnostic"]), [{"tool_input": "[REDACTED]"}])
                # Raw malformed stdout can contain the same encoded credential.
                raw = runner.decode_result(text + "noise", "", 1)
                self.assertNotIn(json.dumps(secret, ensure_ascii=ensure_ascii)[1:-1], raw["message"])

    def test_generic_error_prefix_still_classifies_failed_envelope(self):
        text = json.dumps(envelope("Error: Not logged in", is_error=True,
                                   subtype="error_during_execution"))
        self.assertEqual(runner.decode_result(text, "", 1)["status"], "auth_required")


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
if '--plugin-dir' in args:
    root = pathlib.Path(args[args.index('--plugin-dir') + 1])
    installed_name = root.name
else:
    settings = json.loads(args[args.index('--settings') + 1])
    identity = next(key for key, enabled in settings['enabledPlugins'].items() if enabled)
    registry = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR']) / 'plugins/installed_plugins.json'
    root = pathlib.Path(json.loads(registry.read_text())['plugins'][identity][0]['installPath'])
    installed_name = identity.split('@', 1)[0]
manifest = root / '.claude-plugin/plugin.json'
record = {'args': args, 'prompt': prompt, 'cwd': os.getcwd(), 'pid': os.getpid(),
          'nested_marker': os.environ.get('CLAUDECODE'),
          'plugin': json.loads(manifest.read_text()) if manifest.is_file() else {'name': installed_name}}
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

    def marketplace(self, entries):
        location = self.root / "marketplace"
        catalog = location / ".claude-plugin/marketplace.json"
        catalog.parent.mkdir(parents=True, exist_ok=True)
        catalog.write_text(json.dumps({"name": "test-market", "plugins": entries}))
        (self.config / "plugins/known_marketplaces.json").write_text(json.dumps({
            "test-market": {"installLocation": str(location)},
        }))

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

    def test_root_project_skill_wins_over_nested_skill_for_plain_name(self):
        nested = self.cwd / "packages/web"
        nested.mkdir(parents=True)
        for project in [self.cwd, nested]:
            path = project / ".claude/skills/inspect/SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text("Inspect selected changes")
        with patch.dict(os.environ, self.env):
            target = runner.resolve("inspect", cwd=nested)
        self.assertEqual(target["file"], self.cwd / ".claude/skills/inspect/SKILL.md")

    def test_project_skill_wins_over_personal_legacy_command(self):
        command = self.config / "commands/inspect.md"
        command.parent.mkdir()
        command.write_text("Legacy instructions")
        skill = self.cwd / ".claude/skills/inspect/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("Current skill instructions")
        with patch.dict(os.environ, self.env):
            self.assertEqual(runner.resolve("inspect", cwd=self.cwd)["file"], skill)

    def test_linked_worktree_inherits_main_checkout_skills_only(self):
        # Model Git's on-disk worktree metadata without requiring Git at runtime.
        worktree = self.root / "linked"
        worktree.mkdir()
        git_dir = self.cwd / ".git/worktrees/linked"
        git_dir.mkdir(parents=True)
        (git_dir / "commondir").write_text("../..\n")
        (worktree / ".git").write_text(f"gitdir: {git_dir}\n")
        main_skill = self.cwd / ".claude/skills/inspect/SKILL.md"
        main_skill.parent.mkdir(parents=True)
        main_skill.write_text("Inspect main checkout")
        main_command = self.cwd / ".claude/commands/other.md"
        main_command.parent.mkdir()
        main_command.write_text("Main-only command")
        with patch.dict(os.environ, self.env):
            target = runner.resolve("inspect", cwd=worktree)
            self.assertEqual(target["file"], main_skill)
            with self.assertRaises(runner.Failure) as failure:
                runner.resolve("other", cwd=worktree)
            self.assertEqual(failure.exception.result["status"], "skill_not_found")
            own_skill = worktree / ".claude/skills/inspect/SKILL.md"
            own_skill.parent.mkdir(parents=True)
            own_skill.write_text("Inspect linked checkout")
            self.assertEqual(runner.resolve("inspect", cwd=worktree)["file"], own_skill)
            # Even an empty skills directory opts out of inheritance.
            own_skill.unlink()
            with self.assertRaises(runner.Failure):
                runner.resolve("inspect", cwd=worktree)

    def test_local_skill_resolves_by_alias_and_folder_name(self):
        path = self.config / "skills/alias-folder/SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: alias\n---\nInspect")
        for name in ["alias", "alias-folder"]:
            with self.subTest(name=name):
                proc, result = self.cli(skill=name)
                self.assertEqual(proc.returncode, 0, result)
                record = json.loads(self.log.read_text())
                self.assertTrue(record["prompt"].endswith(":alias"))

    def test_symlinked_skill_without_name_uses_loaded_folder_name(self):
        source = self.root / "source-folder"
        source.mkdir()
        (source / "SKILL.md").write_text("Inspect selected changes")
        skills = self.config / "skills"
        skills.mkdir()
        (skills / "inspect").symlink_to(source, target_is_directory=True)
        proc, result = self.cli(skill="inspect")
        self.assertEqual(proc.returncode, 0, result)
        self.assertTrue(json.loads(self.log.read_text())["prompt"].endswith(":inspect"))

    def test_local_alias_does_not_shadow_existing_directory_name(self):
        for root, folder, name in [(self.config, "other", "inspect"),
                                   (self.cwd / ".claude", "inspect", "project-review")]:
            path = root / "skills" / folder / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text(f"---\nname: {name}\n---\nInspect")
        with patch.dict(os.environ, self.env):
            target = runner.resolve("inspect", cwd=self.cwd)
        self.assertEqual(target["file"], self.cwd / ".claude/skills/inspect/SKILL.md")

    def test_duplicate_local_alias_requires_exact_file(self):
        for folder in ["first", "second"]:
            path = self.config / "skills" / folder / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text("---\nname: alias\n---\nInspect")
        proc, result = self.cli(skill="alias")
        self.assertEqual(result["status"], "skill_ambiguous")
        self.assertFalse(self.log.exists())

    def test_custom_manifest_and_legacy_plugin_command(self):
        for kind in ["custom", "command"]:
            with self.subTest(kind=kind):
                plugin = self.make_plugin(self.root / kind, **{kind: True})
                proc, result = self.cli("--plugin-dir", str(plugin))
                self.assertEqual(proc.returncode, 0)

    def test_manifestless_plugin_uses_directory_namespace(self):
        (self.plugin / ".claude-plugin/plugin.json").unlink()
        plugin = self.plugin.with_name("reviewer")
        self.plugin.rename(plugin)
        proc, result = self.cli("--plugin-dir", str(plugin))
        self.assertEqual(proc.returncode, 0, result)
        self.assertEqual(json.loads(self.log.read_text())["prompt"], "/reviewer:review")

    def test_installed_manifestless_plugin_keeps_marketplace_namespace(self):
        (self.plugin / ".claude-plugin/plugin.json").unlink()
        cache = self.root / "plugins/cache/catalog/reviewer/1.0.0"
        cache.parent.mkdir(parents=True)
        self.plugin.rename(cache)
        evidence = cache / "skills/review/references/evidence.txt"
        evidence.parent.mkdir()
        evidence.write_text("Bundled evidence")
        self.registry({"reviewer@catalog": [{"scope": "user", "installPath": str(cache)}]})
        proc, result = self.cli()
        self.assertEqual(proc.returncode, 0, result)
        record = json.loads(self.log.read_text())
        self.assertEqual(record["plugin"]["name"], "reviewer")
        self.assertEqual(record["prompt"], "/reviewer:review")
        self.assertEqual(record["support"], ["Bundled evidence"])
        self.assertNotIn("--plugin-dir", record["args"])
        settings = json.loads(record["args"][record["args"].index("--settings") + 1])
        self.assertEqual(settings, {"enabledPlugins": {"reviewer@catalog": True}})
        self.assertFalse((cache / ".claude-plugin/plugin.json").exists())

    def test_manifestless_root_skill_keeps_cache_basename_fallback(self):
        cache = self.root / "plugins/cache/catalog/reviewer/1.0.0"
        cache.mkdir(parents=True)
        (cache / "SKILL.md").write_text("Review selected changes")
        self.registry({"reviewer@catalog": [{"installPath": str(cache)}]})
        proc, result = self.cli(skill="reviewer:1.0.0")
        self.assertEqual(proc.returncode, 0, result)
        record = json.loads(self.log.read_text())
        self.assertEqual(record["plugin"]["name"], "reviewer")
        self.assertEqual(record["prompt"], "/reviewer:1.0.0")
        self.assertNotIn("--plugin-dir", record["args"])

    def test_plugin_skill_can_include_its_own_namespace(self):
        path = self.plugin / "skills/review/SKILL.md"
        path.parent.rename(path.parent.with_name("inspect"))
        path = self.plugin / "skills/inspect/SKILL.md"
        path.write_text("---\nname: reviewer:review\n---\nInspect")
        proc, result = self.cli()
        self.assertEqual(proc.returncode, 0, result)
        self.assertEqual(json.loads(self.log.read_text())["prompt"], "/reviewer:review")

    def test_plugin_root_skill_fallback(self):
        (self.plugin / "skills/review/SKILL.md").unlink()
        (self.plugin / "skills/review").rmdir()
        (self.plugin / "skills").rmdir()
        (self.plugin / "SKILL.md").write_text("---\nname: review\n---\nInspect")
        proc, result = self.cli()
        self.assertEqual(proc.returncode, 0, result)
        self.log.unlink()
        (self.plugin / "skills").mkdir()
        proc, result = self.cli()
        self.assertEqual(result["status"], "skill_not_found")
        self.assertFalse(self.log.exists())

    def test_nested_plugin_commands_keep_directory_segments(self):
        command = self.plugin / "commands/db/review.md"
        command.parent.mkdir(parents=True)
        command.write_text("Inspect database changes")
        (self.plugin / "skills/review/SKILL.md").unlink()
        proc, result = self.cli()
        self.assertEqual(result["status"], "skill_not_found")
        self.assertFalse(self.log.exists())
        proc, result = self.cli(skill="reviewer:db:review")
        self.assertEqual(proc.returncode, 0, result)
        self.assertEqual(json.loads(self.log.read_text())["prompt"], "/reviewer:db:review")

    def test_plugin_skill_discovery_excludes_supporting_skill_templates(self):
        template = self.plugin / "skills/review/fixtures/example/SKILL.md"
        template.parent.mkdir(parents=True)
        template.write_text("---\nname: review\n---\nExample template")
        proc, result = self.cli()
        self.assertEqual(proc.returncode, 0, result)
        self.log.unlink()
        (self.plugin / "skills/review/SKILL.md").unlink()
        proc, result = self.cli()
        self.assertEqual(result["status"], "skill_not_found")
        self.assertFalse(self.log.exists())

    def test_nested_local_command_resolves_without_plugin_installation(self):
        for root in [self.config, self.cwd / ".claude"]:
            with self.subTest(root=root):
                command = root / "commands/frontend/component.md"
                command.parent.mkdir(parents=True)
                command.write_text("Build selected component")
                proc, result = self.cli(skill="frontend:component")
                self.assertEqual(proc.returncode, 0, result)
                self.assertTrue(json.loads(self.log.read_text())["prompt"].endswith(":component"))
                command.unlink()

    def test_explicit_plugin_root_overrides_same_named_local_command(self):
        command = self.config / "commands/reviewer/review.md"
        command.parent.mkdir(parents=True)
        command.write_text("Legacy local review")
        proc, result = self.cli("--plugin-dir", str(self.plugin))
        self.assertEqual(proc.returncode, 0, result)
        self.assertEqual(json.loads(self.log.read_text())["prompt"], "/reviewer:review")

    def test_dotted_skill_name_is_used_for_plugin_and_local_invocations(self):
        skill = self.plugin / "skills/review/SKILL.md"
        skill.write_text("---\nname: review.pr\n---\nReview selected PR")
        proc, result = self.cli(skill="reviewer:review.pr")
        self.assertEqual(proc.returncode, 0, result)
        proc, result = self.cli("--skill-file", str(skill), skill="review.pr")
        self.assertEqual(proc.returncode, 0, result)
        self.assertTrue(json.loads(self.log.read_text())["prompt"].endswith(":review.pr"))

    def test_frontmatter_name_comments_preserve_selected_identity(self):
        path = self.plugin / "skills/review/SKILL.md"
        for name in ["alias # public invocation", "'alias' # public invocation", '"alias" # comment']:
            with self.subTest(name=name):
                path.write_text(f"---\nname: {name}\n---\nInspect")
                proc, result = self.cli(skill="reviewer:alias")
                self.assertEqual(proc.returncode, 0, result)
                proc, result = self.cli("--skill-file", str(path), skill="review")
                self.assertEqual(proc.returncode, 0, result)
                self.assertTrue(json.loads(self.log.read_text())["prompt"].endswith(":alias"))

    def test_command_map_supports_inline_and_named_source(self):
        source = self.plugin / "chosen.md"
        source.write_text("---\nname: ignored\n---\nReview")
        manifest = self.plugin / ".claude-plugin/plugin.json"
        manifest.write_text(json.dumps({"name": "reviewer", "commands": {
            "inline": {"content": "Review selected changes."},
            "from-file": {"source": "./chosen.md"},
        }}))
        for skill in ["reviewer:inline", "reviewer:from-file", "reviewer:review"]:
            with self.subTest(skill=skill):
                proc, result = self.cli(skill=skill)
                self.assertEqual(proc.returncode, 0, result)
                self.assertEqual(json.loads(self.log.read_text())["prompt"], "/" + skill)

    def test_declared_commands_replace_default_directory(self):
        default = self.plugin / "commands/review.md"
        default.parent.mkdir()
        default.write_text("Default review")
        custom = self.plugin / "chosen/other.md"
        custom.parent.mkdir()
        custom.write_text("---\nname: ignored\n---\nChosen command")
        manifest = self.plugin / ".claude-plugin/plugin.json"
        manifest.write_text(json.dumps({"name": "reviewer", "commands": "./chosen"}))
        # Remove the independent skill so only command resolution is tested.
        (self.plugin / "skills/review/SKILL.md").unlink()
        proc, result = self.cli()
        self.assertEqual(result["status"], "skill_not_found")
        self.assertFalse(self.log.exists())
        proc, result = self.cli(skill="reviewer:other")
        self.assertEqual(proc.returncode, 0, result)
        custom.rename(custom.with_name("review.md"))
        proc, result = self.cli()
        self.assertEqual(proc.returncode, 0, result)

    def test_command_map_source_cannot_escape_plugin_root(self):
        manifest = self.plugin / ".claude-plugin/plugin.json"
        manifest.write_text(json.dumps({"name": "reviewer", "commands": {
            "review": {"source": "../outside.md"},
        }}))
        proc, result = self.cli()
        self.assertEqual(result["status"], "invalid_request")
        self.assertFalse(self.log.exists())

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

    def test_registry_identity_can_differ_from_manifest_namespace(self):
        self.registry({"catalog-name@market": [{"scope": "user", "installPath": str(self.plugin)}]})
        proc, result = self.cli()
        self.assertEqual(proc.returncode, 0, result)
        self.assertEqual(json.loads(self.log.read_text())["prompt"], "/reviewer:review")
        proc, result = self.cli(skill="reviewer:missing")
        self.assertEqual(result["status"], "skill_not_found")
        self.assertIn("catalog-name@market", " ".join(result["recovery"]))

    def test_marketplace_supplies_manifestless_plugin_components(self):
        (self.plugin / ".claude-plugin/plugin.json").unlink()
        skill = self.plugin / "custom/inspect/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("Inspect selected changes")
        self.marketplace([{"name": "reviewer", "source": "./reviewer", "strict": False,
                           "skills": ["./custom/"]}])
        settings = self.config / "settings.json"
        original = json.dumps({"enabledPlugins": {"reviewer@test-market": False}})
        settings.write_text(original)
        proc, result = self.cli(skill="reviewer:inspect")
        self.assertEqual(proc.returncode, 0, result)
        record = json.loads(self.log.read_text())
        overlay = json.loads(record["args"][record["args"].index("--settings") + 1])
        self.assertEqual(overlay, {"enabledPlugins": {"reviewer@test-market": True}})
        self.assertNotIn("--plugin-dir", record["args"])
        self.assertEqual(settings.read_text(), original)
        self.assertFalse((self.plugin / ".claude-plugin/plugin.json").exists())

    def test_marketplace_adds_to_existing_manifest_components(self):
        skill = self.plugin / "custom/inspect/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("Inspect selected changes")
        self.marketplace([{"name": "reviewer", "source": "./reviewer",
                           "skills": "./custom", "commands": {"extra": {"content": "Extra review"}}}])
        for name in ["review", "inspect", "extra"]:
            proc, result = self.cli(skill="reviewer:" + name)
            self.assertEqual(proc.returncode, 0, result)

    def test_marketplace_strict_false_component_conflict_fails(self):
        self.marketplace([{"name": "reviewer", "source": "./reviewer", "strict": False,
                           "skills": ["./skills"]}])
        proc, result = self.cli()
        self.assertEqual(result["status"], "invalid_request")
        self.assertFalse(self.log.exists())

    def test_marketplace_root_skill_selection_excludes_default_scan(self):
        skill = self.plugin / "custom/inspect/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("Inspect selected changes")
        self.marketplace([{"name": "reviewer", "source": "./", "skills": ["./custom"]}])
        proc, result = self.cli()
        self.assertEqual(result["status"], "skill_not_found")
        self.assertFalse(self.log.exists())
        proc, result = self.cli(skill="reviewer:inspect")
        self.assertEqual(proc.returncode, 0, result)

    def test_marketplace_component_paths_cannot_escape_plugin_root(self):
        self.marketplace([{"name": "reviewer", "source": "./reviewer", "skills": ["../outside"]}])
        proc, result = self.cli()
        self.assertEqual(result["status"], "invalid_request")
        self.assertFalse(self.log.exists())

    def test_unrelated_malformed_plugins_do_not_block_selected_plugin(self):
        broken = self.root / "broken"
        (broken / ".claude-plugin").mkdir(parents=True)
        (broken / ".claude-plugin/plugin.json").write_text("{broken")
        self.registry({"unrelated@market": [{"installPath": str(broken)}],
                       "bad-record@market": "invalid", "bad-path@market": [None],
                       "catalog-name@market": [{"installPath": str(self.plugin)}]})
        proc, result = self.cli()
        self.assertEqual(proc.returncode, 0, result)

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

    def test_invalid_project_path_reports_structured_error(self):
        for project in [123, [], {}, None, ""]:
            with self.subTest(project=project):
                self.registry({"reviewer@market": [{"scope": "project", "projectPath": project,
                                                   "installPath": str(self.plugin)}]})
                proc, result = self.cli()
                self.assertEqual(proc.returncode, 2)
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
            self.assertEqual(manifest["version"], "1.0.2")
            self.assertEqual(manifest["name"], "run-claude-skill")
            self.assertTrue((ROOT / manifest["skills"] / "run-claude-skill/SKILL.md").is_file())
            self.assertTrue((ROOT / manifest["skills"] / "run-claude-skill/scripts/run.py").is_file())


if __name__ == "__main__":
    unittest.main()
