# vibe

An agentic CLI harness for the Claude API, in the style of Claude Code and Bionic. One file, stdlib only, no dependencies.

It gives Claude a terminal job: read and write files, run shell commands, search the web, delegate to subagents, and think at the level you choose. The conversation persists per folder and resumes between runs.

Part of the [Harness Remote](https://github.com/rrblack/harness-remote) release: the app's **Vibe (Claude API)** harness starts this script in `--rpc` mode, so the phone drives it exactly like pi (streamed thinking, tool calls, permission cards, effort picker, stop).

## Requirements

- Python 3.9+ (stdlib only; tested on 3.14)
- An Anthropic API key with credits. Every run spends Claude credits.

## Setup

```
git clone https://github.com/rrblack/vibe
cd vibe
```

Give it a key, either way:

```
set ANTHROPIC_API_KEY=sk-ant-...    (Windows)
export ANTHROPIC_API_KEY=...        (macOS/Linux)
```

or put the key in `anthropic_key.txt` next to `claude_cli.py`. That file is gitignored and must never be committed. The key is read on the machine and only sent to `api.anthropic.com`.

## Usage

Interactive REPL (like running `claude` in a terminal):

```
py claude_cli.py
```

As you type, a live suggestion menu appears under the prompt. Tab accepts (Tab again cycles), Up/Down browse history, Escape clears the line, Ctrl+C cancels the line.

One-shot, like `claude "do X"`:

```
py claude_cli.py "list the files here and count them"
```

Flags:

| Flag | Meaning |
|---|---|
| `-m/--model NAME` | model to use (default `claude-haiku-5-5`; aliases: `haiku`, `sonnet`, `opus`, `fast`, `smart`) |
| `-d/--cwd PATH` | working directory the tools operate in |
| `-w/--workspace NAME` | switch to a saved workspace (folder alias) |
| `-e/--effort LEVEL` | thinking effort: `low` \| `medium` \| `high` (adaptive thinking, 5.5-line models) |
| `-t/--max-tokens N` | max output tokens per call (default 8192) |
| `-y/--yolo` | skip confirmation prompts (auto-approve tools) |
| `-f/--fresh` | start a brand-new session, do not resume the saved one |
| `--max-steps N` | optional cap on agent steps per task (default 0 = unlimited) |
| `--json` | one-shot JSON mode: isolated run, quiet, prints `{"model","answer","input_tokens","output_tokens"}` |
| `--trace/-T` | with `--json`, also emit the work log (tool calls, results, subagents) to stderr |
| `--trace-log PATH` | append the work log to a file another terminal can watch live |
| `--no-cache` | disable prompt caching (on by default) |
| `--no-search` | disable the web search tool |

## Tools

`run_shell`, `read_file`, `write_file`, `list_dir`, `http_request`, `get_current_datetime`, `web_search` (Anthropic server-side live search), `spawn_subagent` (delegate a subtask to a subagent: `researcher` \| `code_reviewer` \| `general`).

Tools that change anything ask for confirmation first, unless you pass `-y` or flip `/yolo`.

## Slash commands

```
/help /model [name] /models /tools /subagents /cwd [path] /ws [name]
/effort [low|medium|high|off] /yolo /search [on|off] /reset /cost /cache [on|off]
/save [path] /load [path] /quit
```

## Workspaces and sessions

- **Workspaces:** named folder aliases in `workspaces.json` next to the script. `/ws` lists them; `/ws <name>` switches folder and loads that folder's conversation; `/ws add <name> <path>` and `/ws rm <name>` manage the list.
- **Sessions:** the conversation auto-saves and resumes between runs, stored per folder in `<folder>/.claude_session.json`. `/reset` or `--fresh` clears it.

## Prompt caching

On by default. It marks the tools plus system prefix and the growing conversation with ephemeral `cache_control` breakpoints, so multi-step jobs and repeat runs bill the shared prefix as cheap cache reads instead of fresh input tokens. `/cache off` or `--no-cache` disables it. `/cost` shows the running totals for the session, including cache writes and reads.

## Orchestration (JSON mode)

`--json` keeps stdout pure JSON (`{"model","answer","input_tokens","output_tokens"}`) for scripts, and `--trace` adds the full work log to stderr. `--trace-log PATH` tees the log to a file you can `tail -f` from another terminal.

## RPC mode (harness-remote)

`--rpc` speaks the pi-compatible RPC record protocol over stdin/stdout (`prompt`, `abort`, `extension_ui_request/response`, `set_thinking_level`). That is how [Harness Remote](https://github.com/rrblack/harness-remote) runs vibe from the phone: the app sees streamed thinking, each tool call with its output, permission cards you answer from the phone, and the effort picker applies to the next turn. The bridge launches it with `-m opus -e high -t 65536` by default; see the harness's README, section *Vibe (Claude API)*.

## Bionic skill (SKILL.md)

`SKILL.md` at the repo root is a Bionic/Claude skill that teaches a local agent how to delegate work to this harness. It covers the one-shot JSON path (`py claude_cli.py --json -T -m haiku "task"`; `-y` only for change-making jobs, scoped with `-d`), the live relay protocol for long jobs (poll the work log before checking for DONE, so progress is always relayed, not just the final answer), and when to use vibe vs the agent's own tools. Install by copying it into a skills folder (e.g. `~/.lmstudio/skills/vibe/`).

The visible, live delegation path used by the phone app and the bridge is `delegate.mjs` in [harness-remote](https://github.com/rrblack/harness-remote) (README section *Delegating to Claude from Bionic*).

## Windows shortcut

`vibe.cmd` runs the harness from any folder with your machine's path. Adjust the path if the folder moves.
