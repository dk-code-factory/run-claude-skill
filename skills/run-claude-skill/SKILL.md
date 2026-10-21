---
name: run-claude-skill
description: Run a user-selected Claude skill in a separate Claude Code CLI process, including code reviews, and report missing skills, authentication failures, usage limits, and other execution errors.
---

Use the selected Claude skill for the user's task. Resolve this skill's own
directory from its loaded location; do not depend on the working directory.
The runner uses only Python's standard library and Claude Code. It does not
need DK workflows, reviewer contracts, or private configuration.

Run `scripts/run.py` with Python 3.11+ from this skill directory, supplying:

```sh
python3 /path/to/run-claude-skill/scripts/run.py \
  --skill pr-review-toolkit:review-pr \
  --cwd /path/to/repository --prompt '123 code tests errors types comments' --json
```

- Select the exact `plugin:skill` requested by the user. An installed personal
  or project skill can use its plain name. For a local skill folder, supply
  `--skill NAME --skill-file /path/to/SKILL.md`; the complete folder stays
  available to Claude. `--plugin-dir` selects an explicit plugin checkout.
- Pass the user's scope and arguments through `--prompt`; use `--prompt -`
  and stdin for a large prompt. Pin a review's scope when supplied. Do not add
  another review methodology, findings schema, publication step, or workflow.
- Default permissions are `plan`, suitable for reviews. For tasks requiring
  writes, choose `default` or `acceptEdits` only within the user's authorized
  scope. The runner never bypasses permissions. `--timeout`, `--max-turns`,
  and `--model` are optional. Permission prompts are disabled in the child;
  an action needing unanswered approval is denied and reported.
- Treat only `status: ok` as successful execution. Present `output` faithfully.
  This status means Claude completed its run, not that its findings were
  independently verified. Every failure has a nonzero exit status and recovery
  instructions. Partial output must remain labeled incomplete, never a clean
  review or successful task. Do not silently switch skills or retry failed runs.
- For missing skills, include the installation recipe. Supply `--marketplace
  owner/repo` when the source is known; otherwise ask for the publisher's source
  instead of guessing. Installation and authentication are separate actions;
  do not perform them automatically. For auth failures, give the long-lived
  `claude setup-token` recipe from the report. Never request or display a token.

Read [recovery guidance](references/recovery.md) when auth, installation, or a
failure needs further explanation. If the bundled runner itself is absent,
the installation is incomplete: reinstall `run-claude-skill` from
`dk-code-factory/run-claude-skill`, then retry. Do not substitute a different runner.
