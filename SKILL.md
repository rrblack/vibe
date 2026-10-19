---
name: vibe
display-name: Vibe (Claude agent delegation)
description: "Delegate tasks to the local Claude API agent (haiku/sonnet/opus
  5.5) via the live delegate.mjs session: research, coding, file edits,
  subagents. Use when Kyle asks to delegate or offload work to Claude."
---

# Vibe: delegate to the Claude API agent (live, visible sessions only)

Kyle's rule: every Claude run must be visible live in the phone app (Harness
Remote) and in the Bionic chat. The ONLY way to delegate is `delegate.mjs`,
which runs Claude as a normal vibe session on the Harness Bridge
(`claude_cli.py --rpc`). Full protocol:
`C:\Users\Kyle\Documents\Claude Projects\Remote Access App\DELEGATE.md`

## The flow

1. Write the task into a prompt file (it must carry all context, expected
   output and constraints; Claude has no chat with Kyle except through the app).

2. Open the live pane once (Bionic desktop): `open_url_in_app_browser`
   with `http://127.0.0.1:8787`. Kyle watches Claude there, live. That pane
   is the progress view; do not poll `wait` in a tight loop to "watch".

3. Start the session:

```
cd "C:\Users\Kyle\Documents\Claude Projects\Remote Access App"
node delegate.mjs start --cwd "<folder Claude works in>" --prompt-file "<prompt.txt>" [--model haiku|sonnet|opus|fable] [--effort low|medium|high] --title "<short title>"
   -> SESSION <id>
```

   (`--prompt "<text>"` works instead of `--prompt-file` for a short task.)

4. Wait, and keep waiting:

```
node delegate.mjs wait <id>          # blocks up to 100 s, prints what is new
   exit 0 DONE | 3 RUNNING (call wait again) | 4 KILLED (idle) | 5 WAITING (question, answer in the app) | 1 error
```

   `wait` has one job: detect the end of the turn. Call it a few times at
   most per turn, not as a watcher (the live pane or the phone app is the
   watcher). Stop waiting only on 0, 4 or 5 (or 1 = error, report it).

5. Collect the answer:

```
node delegate.mjs result <id>        # final reply of the last turn
```

6. Follow-ups and stopping:

```
node delegate.mjs send <id> --prompt "<follow-up>"   # next turn, same session (keeps context)
node delegate.mjs stop <id>
```

## Model and effort

- `--model haiku`: quick and cheap (short questions, simple lookups, parallel small jobs).
  Short questions use exactly the same start / wait / result flow.
- `--model sonnet`: normal coding and research work.
- `--model opus --effort high`: hard jobs (deep debugging, big refactors, hard reasoning).
- `fable` is also available. Effort is low | medium | high.

## Watchdog and output

- Built in: no activity for 300 s (`--idle`) -> the session is stopped. A tool
  call that is still running counts as activity.
- There is NO total time cap. Long jobs are fine; just keep calling `wait`.
- Permissions are auto (same as the old `-y`), so scope the work with `--cwd`
  and verify the changes yourself before reporting done.
- Output per wait is capped (`--think-cap 2000`, `--text-cap 6000` chars) to
  protect Bionic's context; the app always has the full stream.
- Needs the Harness Bridge running on this PC.

## Retired: claude_cli.py --json (blocked)

`claude_cli.py --json` is now BLOCKED by the harness: it refuses to run (exit 2)
and prints the delegate.mjs usage. It was the invisible pattern: a detached
one-shot with nothing in the app or in Bionic, and a crash mid-run lost the
result. Never launch claude_cli.py detached, never poll vibe_result.json.
`VIBE_ALLOW_JSON=1` re-enables --json as a debugging escape hatch only (still
invisible); never use it for real work.

## What claude_cli.py itself is (reference)

- Folder: `C:\Users\Kyle\Documents\Claude Projects\Coding Harness`
  (`claude_cli.py`, stdlib only, Python 3.14, runs with `py`).
- Kyle runs the interactive REPL himself with `vibe` (or `py claude_cli.py`)
  in any terminal. The bridge runs it in `--rpc` mode for the live sessions.
- Tools: read_file, write_file, list_dir, run_shell, http_request,
  get_current_datetime, live web search, and spawn_subagent (roles:
  researcher, code_reviewer, general).
- Flags worth knowing: `-y/--yolo` (skip confirmation prompts),
  `-e/--effort low|medium|high` (adaptive thinking, summarized thinking shown),
  `-m/--model haiku|sonnet|opus|fable`, `-d/--cwd`, `-w/--workspace`.
- Transient Anthropic API errors (429/5xx/529, dropped connections) are retried
  up to 3 times (5 s, 10 s backoff) before any stream data arrives.
- The API key comes from the ANTHROPIC_API_KEY env or `anthropic_key.txt` next
  to the script. Never print it.

## When to delegate vs use your own tools

- Delegate (delegate.mjs): multi-step coding in folders, parallel research,
  code review, long refactors, or whenever Kyle names Claude, haiku, sonnet,
  or opus.
- Your own tools: quick answers, small edits in this workspace, anything you
  can do directly.
