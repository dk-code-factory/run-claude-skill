# Recovery

The runner reports execution status separately from Claude's task output. It
never reruns a model automatically. Partial work may have side effects when a
write-capable permission mode was explicitly selected; inspect before retrying.

## No authentication or expired credentials

For subscription automation, Claude calls its credential a **long-lived OAuth
token**, not a permanent device token. It requires an eligible Claude
subscription. No fixed lifetime is assumed; expiry or revocation requires a
new token. Do this yourself in an interactive terminal:

```sh
claude setup-token
```

Follow its browser authorization URL and terminal prompts. On a headless host,
open the displayed URL in a browser on another machine and complete the flow
as directed. Do not run this command inside a captured agent tool call: its
output contains the secret.

In Bash or Zsh, paste the generated token at a hidden prompt:

```sh
printf 'Claude OAuth token (hidden): '
read -r -s CLAUDE_CODE_OAUTH_TOKEN
printf '\n'
export CLAUDE_CODE_OAUTH_TOKEN
claude auth status --text
```

Run the caller from that same shell. For a service or coding agent, provide
`CLAUDE_CODE_OAUTH_TOKEN` through its secret environment and restart that process.
An `export` in a child shell does not modify an already-running parent agent.
Do not put a real token in shell history, a repository, a chat, or diagnostics.

If deliberately switching from API-key billing to subscription OAuth, clear
conflicting `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` launch settings. Do not
clear them when the user intends API or third-party provider authentication;
repair those credentials using the provider's normal login instead. Interactive
Claude login is also available with `claude auth login`. A successful local
auth-status check does not prove the provider will accept a subsequent request.

Sources: [CLI reference](https://code.claude.com/docs/en/cli-reference),
[OAuth environment variable](https://code.claude.com/docs/en/env-vars),
[authentication](https://code.claude.com/docs/en/authentication).

## Missing skill, plugin, or CLI

Install the exact selected skill; do not substitute a different reviewer. For
a plugin with a known marketplace source:

```sh
claude plugin install PLUGIN --marketplace OWNER/REPOSITORY
```

For example, toolkit is published in Anthropic's official marketplace:

```sh
claude plugin install pr-review-toolkit --marketplace anthropics/claude-plugins-official
```

Use `claude plugin list --json` to inspect installed identities and paths. For
damaged installations, refresh the marketplace and update the exact plugin.
For an unpublished standalone skill, obtain its **complete directory** and
place it in `~/.claude/skills/NAME/`, or supply `--skill-file`. The runner also
honors `CLAUDE_CONFIG_DIR`. Ask the user for the source when unknown; a plugin
name is not evidence of its repository owner. Multiple matching installations
require an explicit `--plugin-dir`.

If `claude` is absent, follow [Claude's setup guide](https://code.claude.com/docs/en/setup).
On macOS, Linux, or WSL:

```sh
curl -fsSL https://claude.ai/install.sh | bash
claude --version
```

For this wrapper's own installation, follow the repository README at
https://github.com/dk-code-factory/run-claude-skill . No scripts install software
or change credentials automatically.

## Limits and other failures

| Status | Action |
| --- | --- |
| `usage_limit` | Wait for the reported reset or repair account credits/plan; keep any reset time in the report. |
| `rate_limited` | Wait for the provider's retry time, then retry explicitly. |
| `context_limit` / `output_limit` | Reduce scope or request shorter output; previous output is incomplete. |
| `turn_limit` / `budget_limit` | Review the caller's run limits before starting again. |
| `permission_denied` | Inspect denied tools or account policy; authorize only the access the task needs. |
| `timeout` / `interrupted` | Child process group is stopped on POSIX. Inspect partial effects, then narrow scope or adjust timeout. |
| `network_error` / `provider_error` | Repair connectivity, proxy/certificates, or wait for service recovery. |
| `invalid_response` | Do not infer completion from missing/invalid JSON; inspect CLI compatibility or noisy hooks. |
| `execution_error` | Inspect the diagnostic. Unsupported flags may require `claude update`. |

The runner checks both CLI exit status and Claude's result envelope. Some CLI
failures appear on stdout; some account limits appear inside a successful
envelope. Recognized failure notices still produce failure. Unknown failures
remain failures with redacted evidence; they are never converted to no findings.

Sources: [programmatic execution](https://code.claude.com/docs/en/headless),
[permission handling](https://code.claude.com/docs/en/permissions).
