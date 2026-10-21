#!/usr/bin/env python3
"""Run one selected Claude skill without another workflow framework."""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*\Z")
AUTH_RECIPE = [
    "For a Claude subscription, run `claude setup-token` in an interactive terminal. "
    "Open its authorization URL in a browser and finish the displayed prompts. "
    "This issues a long-lived OAuth token; it is not a permanent device credential.",
    "In Bash or Zsh, enter the generated token without echo or shell-history storage: "
    "`read -r -s CLAUDE_CODE_OAUTH_TOKEN; export CLAUDE_CODE_OAUTH_TOKEN`. "
    "Press Enter after pasting, then launch this runner from that same shell. "
    "For an agent or service, inject it through that process's secret environment.",
    "Run `claude auth status --text`, then retry once after fixing authentication. "
    "For normal interactive login, use `claude auth login`. For API-key or cloud "
    "provider authentication, repair that provider's credentials instead.",
    "If deliberately switching from API-key billing to subscription OAuth, remove "
    "conflicting ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN settings from the launch "
    "environment. Do not print tokens, paste them into chat, or commit them. "
    "Replace expired or revoked tokens with `claude setup-token` and restart the caller.",
]
EXIT_CODES = {
    "ok": 0, "invalid_request": 2, "skill_not_found": 2,
    "skill_ambiguous": 2, "claude_not_found": 2, "auth_required": 3,
    "usage_limit": 4, "rate_limited": 4, "context_limit": 4,
    "output_limit": 4, "budget_limit": 4, "turn_limit": 4,
    "permission_denied": 5, "timeout": 6, "network_error": 7,
    "provider_error": 7, "invalid_response": 8, "execution_error": 9,
    "interrupted": 130,
}


def redact(value: str) -> str:
    for key, secret in os.environ.items():
        if len(secret) >= 8 and re.search(r"(?:TOKEN|API_KEY|PASSWORD|SECRET)$", key):
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"\bsk-ant-[A-Za-z0-9_-]+", "[REDACTED]", value)
    value = re.sub(r"(?i)(\bBearer\s+)[^\s\"'<>]+", r"\1[REDACTED]", value)
    return value


def outcome(status: str, message: str, *, output: str = "", recovery=(), **extra) -> dict:
    return {"status": status, "success": status == "ok", "message": redact(message),
            "output": redact(output), "recovery": [redact(step) for step in recovery], **extra}


class Failure(Exception):
    def __init__(self, status: str, message: str, recovery=()):
        self.result = outcome(status, message, recovery=recovery)
        super().__init__(message)


def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser().resolve()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise Failure("invalid_request", f"Cannot read configuration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise Failure("invalid_request", f"Expected a JSON object in {path}")
    return value


def skill_name(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        frontmatter = text[4:].split("\n---", 1)[0]
        match = re.search(r"(?m)^name:\s*(['\"]?)([A-Za-z0-9][A-Za-z0-9_-]*)\1\s*$", frontmatter)
        if match:
            return match[2]
    return path.parent.name if path.name == "SKILL.md" else path.stem


def install_recipe(skill: str, marketplace: str | None = None, identity: str | None = None) -> list[str]:
    namespace = skill.split(":", 1)[0]
    if marketplace:
        command = shlex.join(["claude", "plugin", "install", namespace, "--marketplace", marketplace])
        return [f"Install the selected plugin: `{command}`. Then retry the original request."]
    if identity:
        return [f"Refresh its marketplace with `claude plugin marketplace update`, then run "
                f"`{shlex.join(['claude', 'plugin', 'install', identity])}`. "
                "If already installed but damaged, use `claude plugin update` for that exact identity."]
    if ":" in skill:
        return [f"Get the marketplace repository for {namespace} from its publisher or the user. "
                "Do not infer a GitHub owner from the plugin name.",
                f"Replace <owner/repo> with that source, then run "
                f"`claude plugin install {shlex.quote(namespace)} --marketplace '<owner/repo>'`. "
                "Retry with the exact plugin:skill command name."]
    return [f"Install the complete {skill} skill folder (SKILL.md and supporting files) into "
            f"{config_dir() / 'skills' / skill}, or supply --skill-file /path/to/SKILL.md. "
            "If published as a plugin, install that plugin from its documented marketplace "
            "and select plugin:skill. Ask for the source if unknown."]


def confined(root: Path, entry: str) -> Path:
    if not isinstance(entry, str):
        raise Failure("invalid_request", "Plugin skill and command paths must be strings")
    path = (root / entry).resolve()
    if not path.is_relative_to(root.resolve()):
        raise Failure("invalid_request", f"Plugin manifest path leaves its root: {entry}")
    return path


def plugin_procedure(root: Path, namespace: str, name: str) -> Path | None:
    manifest = root / ".claude-plugin/plugin.json"
    if not manifest.is_file():
        return None
    data = read_json(manifest)
    if data.get("name") != namespace:
        raise Failure("invalid_request", f"Plugin manifest at {root} does not identify {namespace}")
    matches = set()
    # Default directories load alongside custom paths, as Claude's plugin format specifies.
    for key, default in [("skills", "./skills"), ("commands", "./commands")]:
        declared = data.get(key, [])
        entries = [declared] if isinstance(declared, str) else declared
        if not isinstance(entries, list):
            raise Failure("invalid_request", f"Invalid {key} declaration in {manifest}")
        for entry in [default, *entries]:
            path = confined(root, entry)
            if path.is_file():
                candidates = [path]
            elif path.is_dir():
                candidates = path.rglob("SKILL.md" if key == "skills" else "*.md")
            else:
                continue
            for candidate in candidates:
                candidate = confined(root, str(candidate.relative_to(root)))
                if skill_name(candidate) == name:
                    matches.add(candidate)
    if len(matches) > 1:
        raise Failure("skill_ambiguous", f"Multiple procedures identify {namespace}:{name} in {root}",
                      ["Fix duplicate skill names in the plugin, or pass one exact --skill-file."])
    return next(iter(matches), None)


def resolve(skill: str, *, cwd: Path, skill_file=None, plugin_dir=None, marketplace=None) -> dict:
    skill = skill.removeprefix("/")
    parts = skill.split(":")
    if len(parts) > 2 or any(not NAME.fullmatch(part) for part in parts):
        raise Failure("invalid_request", "Use a skill name or plugin:skill; pass arguments through --prompt")
    if skill in {"run-claude-skill", "run-claude-skill:run-claude-skill"}:
        raise Failure("invalid_request", "Select the task skill, not this launcher itself")
    if skill_file and plugin_dir:
        raise Failure("invalid_request", "--skill-file and --plugin-dir cannot be combined")
    if skill_file:
        path = Path(skill_file).expanduser().resolve()
        if path.is_dir():
            path /= "SKILL.md"
        if not path.is_file() or path.name != "SKILL.md":
            raise Failure("skill_not_found", f"Local SKILL.md is missing: {path}", install_recipe(parts[-1]))
        actual = skill_name(path)
        if actual != skill or not NAME.fullmatch(actual):
            raise Failure("invalid_request", f"--skill must match local skill name {actual!r}")
        return {"skill": skill, "file": path}
    if len(parts) == 2:
        namespace, name = parts
        if plugin_dir:
            root = Path(plugin_dir).expanduser().resolve()
            if not plugin_procedure(root, namespace, name):
                raise Failure("skill_not_found", f"Skill {skill} is unavailable at {root}",
                              install_recipe(skill, marketplace))
            return {"skill": skill, "root": root}
        registry = config_dir() / "plugins/installed_plugins.json"
        plugins = read_json(registry).get("plugins", {}) if registry.exists() else {}
        if not isinstance(plugins, dict):
            raise Failure("invalid_request", f"Invalid plugin registry: {registry}")
        matches, identities = [], []
        for identity, records in plugins.items():
            if identity.split("@", 1)[0] != namespace:
                continue
            identities.append(identity)
            if not isinstance(records, list):
                raise Failure("invalid_request", f"Invalid registry records for {identity}")
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get("installPath"), str):
                    raise Failure("invalid_request", f"Invalid installation record for {identity}")
                if record.get("scope") in ("project", "local"):
                    project = record.get("projectPath")
                    if not project or not cwd.is_relative_to(Path(project).expanduser().resolve()):
                        continue
                root = Path(record["installPath"]).expanduser().resolve()
                if plugin_procedure(root, namespace, name):
                    matches.append(root)
        matches = list(dict.fromkeys(matches))
        if len(matches) == 1:
            return {"skill": skill, "root": matches[0]}
        if len(matches) > 1:
            raise Failure("skill_ambiguous", f"Multiple installations provide {skill}",
                          ["Select the intended plugin with --plugin-dir /exact/plugin/root."])
        identity = identities[0] if len(identities) == 1 else None
        raise Failure("skill_not_found", f"Claude skill {skill} is not installed or its files are missing",
                      install_recipe(skill, marketplace, identity))
    if plugin_dir:
        raise Failure("invalid_request", "--plugin-dir requires plugin:skill")
    # Personal skills win over project skills, matching Claude's documented precedence.
    roots = [config_dir()]
    for parent in [cwd, *cwd.parents]:
        roots.append(parent / ".claude")
        if (parent / ".git").exists():
            break
    for root in dict.fromkeys(roots):
        matches = []
        for path in [root / "skills" / skill / "SKILL.md", root / "commands" / f"{skill}.md"]:
            if path.is_file() and skill_name(path) == skill:
                matches.append(path.resolve())
        if matches:
            # A skill takes precedence over an identically named legacy command.
            return {"skill": skill, "file": matches[0]}
    raise Failure("skill_not_found", f"Local Claude skill {skill} was not found", install_recipe(skill))


@contextlib.contextmanager
def loaded_target(target: dict):
    if "root" in target:
        yield target["skill"], target["root"], None
        return
    source = target["file"]
    # A temporary plugin gives the exact selected file an unambiguous namespace.
    with tempfile.TemporaryDirectory(prefix="run-claude-skill-target-") as directory:
        root = Path(directory)
        namespace = root.name
        (root / ".claude-plugin").mkdir()
        (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": namespace}))
        if source.name == "SKILL.md":
            destination = root / "skills" / target["skill"]
            destination.parent.mkdir()
            destination.symlink_to(source.parent, target_is_directory=True)
        else:
            (root / "commands").symlink_to(source.parent, target_is_directory=True)
        yield namespace + ":" + target["skill"], root, source.parent


def classify(text: str) -> tuple[str, list[str]]:
    rules = [
        ("auth_required", r"authentication[_ ](?:failed|error|required)|not (?:logged|signed) in|"
         r"invalid[_ ](?:api[_ ]key|credentials|oauth[_ ]token)|(?:expired|revoked|invalid).{0,30}(?:oauth |access |auth )?token|"
         r"token.{0,30}(?:expired|revoked)|\b401\b|please (?:run |use )?/?login", AUTH_RECIPE),
        ("skill_not_found", r"unknown (?:skill|slash command)|(?:skill|slash command).{0,80}(?:not found|not available|does not exist)",
         ["Check the exact skill name and installation; run `claude plugin list --json`. "
          "Install the selected plugin from its publisher's marketplace, then retry."]),
        ("usage_limit", r"insufficient_quota|billing_error",
         ["Repair account credits, quota, or billing before explicitly retrying."]),
        ("rate_limited", r"rate[_ ]limit|too many requests|\b429\b",
         ["Wait for the provider's retry/reset time, then retry explicitly. No automatic rerun was made."]),
        ("usage_limit", r"(?:usage|weekly|session|token|quota).{0,30}limit.{0,20}(?:reached|exceeded|exhausted)|"
         r"(?:hit|reached|exceeded).{0,30}(?:your |the )?(?:usage |weekly |session )?limit|"
         r"out of (?:tokens|credits)|insufficient.{0,15}(?:credits|quota)|credit balance.{0,20}(?:low|exhausted)|usage_limit|tokens_exhausted",
         ["Wait until the reported usage reset, or adjust the account's plan or credits. "
          "Preserve any reset time in the diagnostic. Do not retry automatically or treat this as a completed review."]),
        ("context_limit", r"prompt is too long|context.{0,20}(?:limit|exceeded|full)|context_length_exceeded",
         ["Reduce the input or review scope, then start a fresh run."]),
        ("output_limit", r"max_output_tokens|output.{0,15}(?:token limit|truncat)|maximum.{0,15}output tokens",
         ["Request a shorter result or smaller scope; the previous output is incomplete."]),
        ("budget_limit", r"error_max_budget_usd|max(?:imum)? budget|budget.{0,15}(?:exceeded|exhausted)",
         ["Review the caller's spending limit before starting a new run."]),
        ("turn_limit", r"error_max_turns|max(?:imum)? turns|turn limit",
         ["Increase --max-turns only if appropriate, or narrow the request. The previous run is incomplete."]),
        ("permission_denied", r"permission[_ ]denied|not permitted|access[_ ]denied|\b403\b",
         ["Inspect the denied tool, organization policy, or provider access. "
          "Grant only the permissions needed for this task; do not bypass permissions automatically."]),
        ("network_error", r"ECONN\w+|ENOTFOUND|EAI_AGAIN|ETIMEDOUT|fetch failed|network error|"
         r"connection (?:error|failed|refused|reset)|failed to fetch|unable to connect|certificate|SSL|TLS|timed out",
         ["Check connectivity, proxy, and certificate settings; retry after repairing the connection."]),
        ("provider_error", r"overloaded|\b(?:500|502|503|529)\b|server_error",
         ["Check provider availability and retry later. No automatic rerun was made."]),
    ]
    for status, pattern, recipe in rules:
        if re.search(pattern, text, re.I | re.S):
            return status, recipe
    return "execution_error", ["Inspect the redacted diagnostic and `claude --version`. "
                               "For unsupported CLI options, update Claude Code with `claude update`. "
                               "Fix the reported problem before retrying."]


def decode_result(stdout: str, stderr: str, code: int) -> dict:
    try:
        envelope = json.loads(stdout)
        if isinstance(envelope, list):
            results = [x for x in envelope if isinstance(x, dict) and x.get("type") == "result"]
            envelope = results[-1] if results else None
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict) or envelope.get("type") != "result":
        diagnostic = (stderr + "\n" + stdout).strip()
        status, recovery = classify(diagnostic)
        if status == "execution_error" and code == 0:
            status = "invalid_response"
            recovery = ["Claude returned no valid JSON result envelope. Check CLI compatibility "
                        "and hooks that write to stdout; do not infer successful completion."]
        return outcome(status, diagnostic[:8000] or "Claude produced no result", recovery=recovery,
                       claude_exit_code=code)
    result = envelope.get("result")
    output = result if isinstance(result, str) else ""
    failed = code != 0 or envelope.get("is_error") is not False or envelope.get("subtype") != "success"
    # Some CLI versions report account limits as a successful plain-text result.
    cli_error = re.match(r"\A\s*(?:API Error:|Error:|Not (?:logged|signed) in(?:\s*[.·!]|\s*Please|\s*$)|"
                         r"You['’]ve hit your limit\b|You have hit your limit\b|"
                         r"Unknown (?:skill|slash command):)",
                         output, re.I)
    failure_notice = failed and re.match(
        r"\A\s*(?:Not (?:logged|signed) in\b|authentication[_ ](?:failed|error)|"
        r"invalid (?:API key|credentials)|(?:OAuth |access )?token (?:expired|revoked))", output, re.I)
    if failed or cli_error:
        subtype = str(envelope.get("subtype", ""))
        known_limit = subtype in {"error_max_turns", "error_max_budget_usd", "error_max_output_tokens"}
        errors = envelope.get("errors", [])
        # Partial task output can mention authentication or quotas while reviewing
        # those features. Classify CLI metadata, not arbitrary review prose.
        evidence = "\n".join(filter(None, [subtype, json.dumps(errors) if errors else "",
                                              output if cli_error or failure_notice else "", stderr]))
        status, recovery = classify(subtype if known_limit else evidence)
        return outcome(status, evidence.strip()[:8000], output=output, recovery=recovery,
                       claude_exit_code=code)
    if envelope.get("permission_denials"):
        return outcome("permission_denied", "Claude reported denied tools; completion cannot be assumed.",
                       output=output, recovery=["Inspect denied tools and authorize only required access "
                                                "before retrying. Partial output is not a completed review."],
                       diagnostic=redact(json.dumps(envelope["permission_denials"]))[:8000],
                       claude_exit_code=code)
    if not isinstance(result, str) or not result.strip():
        return outcome("invalid_response", "Claude returned an empty result", claude_exit_code=code,
                       recovery=["Inspect Claude configuration and retry after fixing the empty response."])
    return outcome("ok", "Claude skill completed", output=result, claude_exit_code=code)


def stop_process(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    proc.communicate()


def run(args) -> dict:
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise Failure("invalid_request", "--timeout must be a finite positive number")
    if args.max_turns is not None and args.max_turns <= 0:
        raise Failure("invalid_request", "--max-turns must be positive")
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise Failure("invalid_request", f"Working directory does not exist: {cwd}")
    target = resolve(args.skill, cwd=cwd, skill_file=args.skill_file,
                     plugin_dir=args.plugin_dir, marketplace=args.marketplace)
    binary = shutil.which("claude")
    if not binary:
        raise Failure("claude_not_found", "Claude Code CLI is not installed or is absent from PATH", [
            "Install Claude Code using https://code.claude.com/docs/en/setup . "
            "On macOS/Linux/WSL: `curl -fsSL https://claude.ai/install.sh | bash`.",
            "Open a new terminal if needed, confirm `claude --version`, then retry. "
            "Use `claude setup-token` for subscription automation authentication.",
        ])
    with loaded_target(target) as (invocation, root, extra_dir):
        command = [binary, "--permission-mode", args.permission_mode, "--permission-prompts", "none",
                   "--no-session-persistence", "--output-format", "json", "--plugin-dir", str(root)]
        if extra_dir:
            command += ["--add-dir", str(extra_dir)]
        if args.max_turns:
            command += ["--max-turns", str(args.max_turns)]
        if args.model:
            command += ["--model", args.model]
        command += ["-p"]
        prompt = f"/{invocation}" + (" " + args.prompt if args.prompt else "")
        # Match the Agent SDK's independent-process behavior when called by
        # Claude itself. Keep provider credentials and permissions unchanged.
        child_env = dict(os.environ)
        child_env.pop("CLAUDECODE", None)
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=cwd, text=True, encoding="utf-8",
                                errors="replace", env=child_env, start_new_session=os.name == "posix")
        try:
            stdout, stderr = proc.communicate(prompt, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            stop_process(proc)
            return outcome("timeout", f"Claude exceeded {args.timeout:g} seconds; its process group was stopped.",
                           recovery=["Narrow the request or explicitly increase --timeout. "
                                     "The task may have run partially; inspect its effects before retrying."])
        except BaseException:
            stop_process(proc)
            raise
    result = decode_result(stdout, stderr, proc.returncode)
    if result["status"] == "skill_not_found":
        result["recovery"] = [redact(step) for step in install_recipe(args.skill, args.marketplace)]
    return result


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("invalid_request", message, ["Run this script with --help for supported arguments."])


def parser() -> argparse.ArgumentParser:
    value = Parser(description=__doc__)
    value.add_argument("--skill", required=True, help="Installed plugin:skill or local skill name")
    value.add_argument("--skill-file", help="Local SKILL.md or its complete skill directory")
    value.add_argument("--plugin-dir", help="Exact Claude plugin root; requires plugin:skill")
    value.add_argument("--marketplace", help="Publisher marketplace source used in missing-skill recovery instructions")
    value.add_argument("--prompt", default="", help="Skill arguments; use - to read stdin")
    value.add_argument("--cwd", default=str(Path.cwd()), help="Target working directory")
    value.add_argument("--timeout", type=float, default=1200, help="Wall-clock seconds, default 1200")
    value.add_argument("--max-turns", type=int, help="Optional Claude turn limit")
    value.add_argument("--model", help="Optional Claude model selector")
    value.add_argument("--permission-mode", choices=["plan", "default", "acceptEdits"], default="plan")
    value.add_argument("--json", action="store_true", help="Print structured status instead of text")
    return value


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    json_mode = "--json" in argv
    skill = None
    try:
        args = parser().parse_args(argv)
        skill = args.skill
        if args.prompt == "-":
            args.prompt = sys.stdin.read()
        result = run(args)
    except Failure as exc:
        result = exc.result
    except KeyboardInterrupt:
        result = outcome("interrupted", "Claude skill run was interrupted; the child process was stopped.",
                         recovery=["Inspect partial effects before explicitly retrying."])
    except (OSError, ValueError) as exc:
        result = outcome("execution_error", str(exc), recovery=["Fix the reported local error before retrying."])
    result["skill"] = redact(skill) if skill else None
    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    elif result["success"]:
        print(result["output"])
    else:
        print(f"run-claude-skill: {result['status']}: {result['message']}", file=sys.stderr)
        for step in result["recovery"]:
            print(f"  {step}", file=sys.stderr)
        if result["output"]:
            print("Partial output (task did not complete):\n" + result["output"], file=sys.stderr)
    return EXIT_CODES[result["status"]]


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, interrupted)
    sys.exit(main())
