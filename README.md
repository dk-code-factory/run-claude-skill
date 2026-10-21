# Claude Skill

Run a selected Claude skill in a separate Claude Code CLI process. Review
skills supply their own methodology; this wrapper supplies execution and
failure reporting. It is independent of DK workflows and private settings.

Requires Python 3.11+ and Claude Code 2.1.259+ on macOS, Linux, or WSL, plus the
selected Claude skill and its prerequisites. Uses only Python's standard library.

## Install

Codex:

```sh
codex plugin marketplace add dk-code-factory/run-claude-skill
codex plugin add run-claude-skill@run-claude-skill
```

Claude Code:

```sh
claude plugin install run-claude-skill --marketplace dk-code-factory/run-claude-skill
```

The same self-contained skill directory works in both hosts. Start a new host
session after installation if the skill is not yet listed. Ask the host:

```text
Use run-claude-skill to run pr-review-toolkit:review-pr on PR 123.
```

Or use the runner directly from a checkout:

```sh
git clone https://github.com/dk-code-factory/run-claude-skill.git
python3 run-claude-skill/skills/run-claude-skill/scripts/run.py \
  --skill pr-review-toolkit:review-pr --cwd /path/to/repo \
  --prompt '123 code tests errors types comments' --json
```

For an unpublished skill:

```sh
python3 skills/run-claude-skill/scripts/run.py \
  --skill my-review --skill-file /path/to/my-review/SKILL.md \
  --cwd /path/to/repo --prompt 'Review committed changes from origin/main to HEAD.'
```

Local skills load through a temporary namespaced plugin; the entire skill folder
and its supporting files stay available. Installed plugins resolve from Claude's
registry, marketplace entries, and manifests, respecting project scope. The
selected plugin is enabled for the child process without changing saved settings.
Ambiguous installations require
`--plugin-dir`. Personal/project skills can also be selected by plain name.
Use `--marketplace OWNER/REPO` to provide a verified source for missing-plugin
installation instructions. The runner never guesses repositories or installs
software automatically.

## Output and failures

Text mode prints Claude's result on success. Failures go to stderr with recovery
steps and a nonzero exit status. `--json` prints one object on stdout:

```json
{"status":"auth_required","success":false,"message":"Not logged in","output":"","recovery":["Run claude setup-token interactively; inject its token through CLAUDE_CODE_OAUTH_TOKEN."],"skill":"my-review"}
```

`output` is the task's unparsed text, with recognized credentials redacted. A
failed run may include partial output; it is not a completed review. `ok` means
Claude completed execution, not that the result was independently verified.
The runner checks both process status and Claude's JSON envelope, including
reported tool denials. Unexpected or empty responses fail explicitly.

| Exit | Meaning |
| --- | --- |
| 0 | Completed |
| 2 | Invalid input, missing CLI/skill, or ambiguous installation |
| 3 | Authentication needed |
| 4 | Usage, rate, context, output, budget, or turn limit |
| 5 | Permissions or account access denied |
| 6 | Timeout |
| 7 | Network or provider failure |
| 8 | Invalid or empty Claude response |
| 9 | Other execution failure |
| 130 | Interrupted |

Auth failures include a recipe for `claude setup-token`, which creates a
long-lived **subscription OAuth token**, and hidden-input instructions for
`CLAUDE_CODE_OAUTH_TOKEN`. API-key and cloud-provider logins remain supported by
Claude itself. Missing skills include exact install instructions when their
source is known, or ask for the publisher's source. See
[recovery guidance](skills/run-claude-skill/references/recovery.md).

## Execution controls

Default `--permission-mode plan` suits reviews. Explicit `default` or
`acceptEdits` supports authorized write tasks. No bypass mode is exposed.
Unanswered permission prompts are denied. Claude's own configured hooks and
MCP servers still load; plan mode is not an operating-system sandbox. Review
selected plugins and repository configuration as you normally would.

Default wall timeout is 1200 seconds; override `--timeout`. `--max-turns` and
`--model` are optional. Prompts use stdin and argument vectors, never shell
execution. Timeout and interruption stop the child process group on POSIX.
The independent child clears only Claude's inherited `CLAUDECODE` session
marker, matching its Agent SDK behavior; provider credentials still inherit.
No automatic model retries, session persistence, framework state, or custom
review contracts are added. Claude itself may retry provider requests until
the runner's wall timeout. No caller credentials are stored or modified.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use fake CLI processes and temporary configurations; they require no
credentials or live model calls. Manifests and self-marketplaces make this
repository independently installable, without another organization catalog.
