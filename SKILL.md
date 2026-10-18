---
name: vibe
display-name: Vibe (Claude agent delegation)
description: "Delegate tasks to the local Claude API agent (haiku/sonnet/opus
  5.5) through the vibe CLI: research, coding, file edits, subagents. Use when
  Kyle asks to delegate or offload work to Claude."
---

# Vibe: delegate to the Claude API agent

`vibe` is a Claude Code-style agentic harness at
`C:\Users\Kyle\Documents\Claude Projects\Coding Harness\claude_cli.py`
(stdlib only, runs with `py`). It runs Claude 5.5-line models with tools:
read_file, write_file, list_dir, run_shell, http_request, live web search, and
spawn_subagent (roles: researcher, code_reviewer, general).

## Calling it (one-shot, JSON)

Always add `-T` (--trace) so you can see the work log (it goes to stderr; stdout
stays pure JSON) and relay what Claude did to Kyle.

Safe read-only mode (default; research, analysis, review, Q&A). In this mode
run_shell, write_file, and non-GET http_request are auto-denied:

```
py "C:\Users\Kyle\Documents\Claude Projects\Coding Harness\claude_cli.py" --json -T -m haiku --no-search "your fully self-contained task"
```

Full mode (file writes + shell + non-GET HTTP run without prompts). Use only when
Kyle asked for changes:

```
py "C:\Users\Kyle\Documents\Claude Projects\Coding Harness\claude_cli.py" --json -T -y -m sonnet -d "C:\path\to\project" "task"
```

stdout is pure JSON: `{"model", "answer", "input_tokens", "output_tokens"}`.
stderr (with -T) is the work log: each `[tool] name`, its `-> result`,
subagent starts/done, and streamed narration. Show Kyle a concise summary of
that work, then the answer. The harness also writes the result JSON to
`vibe_result.json` next to the script (reliable for detached runs).

## Flags

- `-m/--model`: `haiku` (default, fast/cheap), `sonnet` (balanced), `opus` (hardest), `fable`; full IDs work too (claude-sonnet-5-5, ...)
- `-e/--effort`: low | medium | high
- `-d/--cwd`: working directory the agent uses for files and shell; scope the work here
- `-y/--yolo`: skip confirmations (without it, writes/shell are denied in --json mode)
- `-T/--trace`: emit the work log to stderr (recommended; keeps stdout pure JSON)
- `--max-steps N`: optional cap on agent steps per task (default 0 = unlimited); set e.g. 12 only if Kyle wants a budget on the job
- `--trace-log PATH`: also append the work log to PATH (default: vibe_trace.log next to the script)
- `--no-search`: disable live web search
- `-t/--max-tokens`: output cap (default 8192)
- `-w/--workspace`: a saved workspace folder from workspaces.json

## Watch the work live

Every traced run appends to the work log:
`C:\Users\Kyle\Documents\Claude Projects\Coding Harness\vibe_trace.log`
To watch a delegated run as it goes, open a second terminal:

```
tail -f "C:\Users\Kyle\Documents\Claude Projects\Coding Harness\vibe_trace.log"
```
(Windows 11 ships tail; fallback: powershell -c "Get-Content -Wait 'C:\Users\Kyle\Documents\Claude Projects\Coding Harness\vibe_trace.log'")

## Long jobs: live relay (you are the caller)

For delegated jobs likely to take more than ~15s, run the harness detached and relay progress to Kyle between polls. Short one-shot delegations stay on the direct foreground call above.

Step 1, launch (one shell call; quote the task inside the ArgumentList; add `-y` for change-making jobs):

```
cd "/c/Users/Kyle/Documents/Claude Projects/Coding Harness"
rm -f vibe_result.json vibe_run.err
SIZE=$(stat -c%s vibe_trace.log 2>/dev/null || echo 0); echo $SIZE > .run_offset
powershell -NoProfile -Command "\$p = Start-Process -FilePath 'py' -WorkingDirectory 'C:\Users\Kyle\Documents\Claude Projects\Coding Harness' -ArgumentList 'claude_cli.py --json -T -m haiku --no-search \"your task text\"' -RedirectStandardError 'C:\Users\Kyle\Documents\Claude Projects\Coding Harness\vibe_run.err' -NoNewWindow -PassThru; Set-Content -Path 'C:\Users\Kyle\Documents\Claude Projects\Coding Harness\vibe_run.pid' -Value \$p.Id -Encoding ascii"
```

Step 2, poll (repeat until ===DONE===; each call covers ~25s; it always prints the new work log lines first, also on the final call, so relay them; send Kyle a one-line progress note from the new log lines between polls):

```
cd "/c/Users/Kyle/Documents/Claude Projects/Coding Harness"
DONE=; for i in 1 2 3 4 5 6 7 8; do [ -s vibe_result.json ] && DONE=1
CUR=$(stat -c%s vibe_trace.log 2>/dev/null || echo 0); OFF=$(cat .run_offset 2>/dev/null || echo 0)
if [ "$CUR" -gt "$OFF" ]; then tail -c +$((OFF+1)) vibe_trace.log; echo $CUR > .run_offset; fi
[ -n "$DONE" ] && break; sleep 3; done
[ -s vibe_result.json ] && echo ===DONE=== || echo ===RUNNING===
```

Step 3, collect: `cat vibe_result.json` (model, answer, tokens). If still RUNNING after repeated polls, check the pid with `tasklist //FI "PID eq $(cat vibe_run.pid)"` and, if the process is dead, show the tail of `vibe_run.err`.

## Rules for you (the caller)

- Always use `-T`; read the stderr work log and show Kyle what Claude did, not just the final answer.
- Long jobs (expected over ~15s): use the live relay protocol above so Kyle sees progress in chat as it happens, not just the final result.
- For report-sized outputs raise the token cap (-t 65536). Steps are unlimited by default; if you do cap them with --max-steps, an exhausted budget ends with a salvage write-up, and truncated tool calls come back as a clean retry error.
- Default to safe mode for anything that only reads, analyzes, researches, or reviews.
- Use `-y` only when Kyle explicitly asked for changes, and always scope with `-d`.
- The delegated agent has no chat with Kyle: the task string must carry all context, expected output, and constraints.
- Verify `-y` results yourself (read the changed files, run the checks) before reporting done.
- Budget: haiku for simple and parallel work, sonnet for real coding, opus only for hard problems or when Kyle asks.
- The API key comes from the ANTHROPIC_API_KEY env or `anthropic_key.txt` next to the script. Never print it.
- Kyle runs the interactive REPL himself with `vibe` in any terminal; you use the --json path.

## When to use vibe vs your own tools

- vibe: multi-step coding in folders, parallel research, code review, long refactors, or whenever Kyle names Claude, haiku, sonnet, or opus.
- your own tools: quick answers, small edits in this workspace, anything you can do directly.
