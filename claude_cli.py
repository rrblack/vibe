#!/usr/bin/env python3
"""claude_cli.py — a small agentic CLI harness (Claude Code / Bionic style).

Stdlib only. Runs with:  py claude_cli.py

Interactive (REPL) — type tasks, it uses tools and keeps the conversation:
    py claude_cli.py
    As you type, a live suggestion menu appears under the prompt.
    Tab accepts (Tab again cycles choices), Up/Down browse history,
    Escape clears the line, Ctrl+C cancels the line.
One-shot (like `claude "do X"`):
    py claude_cli.py "list the files here and count them"

Flags:
    -m/--model NAME     model to use (default claude-haiku-5-5); aliases: haiku, sonnet, opus, fable, fast, smart
    -d/--cwd PATH       working directory the tools operate in
    -w/--workspace NAME switch to a saved workspace (folder alias from workspaces.json)
    -e/--effort LEVEL   thinking effort: low | medium | high (adaptive thinking, 5.5-line models)
    -t/--max-tokens N   max output tokens per call (default 8192)
    -y/--yolo           skip confirmation prompts (auto-approve tools)
    -f/--fresh          start a brand-new session (do not resume the saved one)
    --json              one-shot JSON mode: isolated run, no session, quiet, prints
                        {"model","answer","input_tokens","output_tokens"} (for orchestration)
    --trace/-T          with --json, also emit the work log (tool calls, results, subagents,
                        narration) to stderr so stdout stays pure JSON
    --max-steps N       optional cap on agent steps per task (default 0 = unlimited);
                        pass e.g. 12 to budget a job, or the word "unlimited"
    --trace-log PATH    also append the work log to PATH (default: vibe_trace.log next to this
                        script) so another terminal can watch it live:  tail -f vibe_trace.log
    --no-cache          disable Anthropic prompt caching (on by default). Caching marks the
                        tools+system prefix and the growing conversation with ephemeral
                        breakpoints, so multi-step jobs and repeat runs bill the shared prefix
                        as cheap cache reads instead of fresh input tokens.

Workspaces:
    Named folder aliases live in workspaces.json next to this script.
    /ws lists them; /ws <name> switches folder and loads that folder's conversation;
    /ws add <name> <path> and /ws rm <name> manage the list.

Sessions:
    The conversation auto-saves and resumes between runs, stored per folder
    (<cwd>/.claude_session.json). Switching workspaces switches conversations.
    /reset clears it; --fresh starts over without loading it.

Built-in tools:
    run_shell, read_file, write_file, list_dir, http_request, get_current_datetime
    web_search      (Anthropic server-side live web search; toggle with /search or --no-search)
    spawn_subagent  (delegate a subtask to a subagent: researcher | code_reviewer | general)

Slash commands:
    /help /model [name] /models /tools /subagents /cwd [path] /ws [name]
    /effort [low|medium|high|off] /yolo /search [on|off] /reset /cost /cache [on|off]
    /save [path] /load [path] /quit
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

API_BASE = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-haiku-5-5"
MAX_STEPS = 0  # 0 = unlimited (no step budget); pass --max-steps N to cap a job

# Short aliases for common models (family -> current 5.5 line). Full IDs work too.
MODEL_ALIASES = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
    "fable": "claude-fable-5-1",
    "fast": "claude-haiku-5-5",
    "smart": "claude-opus-5-5",
}


def resolve_model(name):
    n = (name or "").strip().lower()
    return MODEL_ALIASES.get(n, (name or "").strip())


STATE = {}


# ---------------- key + low-level http ----------------
def resolve_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    kf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anthropic_key.txt")
    if os.path.exists(kf):
        with open(kf, "r", encoding="utf-8") as f:
            k = f.read().strip()
            if k:
                return k
    raise SystemExit("No API key. Set ANTHROPIC_API_KEY or create anthropic_key.txt next to this script.")


def list_models_api(key: str):
    req = urllib.request.Request(API_BASE + "/v1/models",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["data"]


_TRACE_LOG_HANDLE = None


def _emit(msg: str, end: str = "\n"):
    """Progress line. In quiet (json) mode it only shows with --trace and goes to stderr
    (keeps stdout pure JSON); in interactive mode it goes to stdout as before.
    When a trace log file is open (see main), every line is also appended there so
    another terminal can watch the run live:  tail -f vibe_trace.log"""
    if STATE.get("quiet") and not STATE.get("trace"):
        return
    target = sys.stderr if STATE.get("quiet") else sys.stdout
    target.write(msg + end)
    target.flush()
    if _TRACE_LOG_HANDLE is not None:
        try:
            _TRACE_LOG_HANDLE.write(msg + end)
            _TRACE_LOG_HANDLE.flush()
        except Exception:  # noqa: BLE001
            pass


def call_model(key: str, payload: dict, quiet: bool = False):
    """Stream a Messages API call. Returns (content_blocks, stop_reason, usage)."""
    payload = dict(payload)
    payload["stream"] = True
    req = urllib.request.Request(
        API_BASE + "/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json", "accept": "text/event-stream"},
        method="POST",
    )
    blocks = {}
    order = []
    stop_reason = None
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_write": 0, "cache_read": 0}

    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "message_start":
                u = ev.get("message", {}).get("usage", {})
                usage["input_tokens"] = u.get("input_tokens", 0)
                usage["cache_write"] = u.get("cache_creation_input_tokens", 0)
                usage["cache_read"] = u.get("cache_read_input_tokens", 0)
            elif t == "content_block_start":
                idx = ev["index"]
                block = dict(ev["content_block"])
                blocks[idx] = block
                order.append(idx)
                btype = block.get("type")
                if btype == "tool_use":
                    block["_input"] = ""
                    _emit(f"\n  [tool] {block.get('name')} …")
                elif btype == "server_tool_use":
                    block["_input"] = ""
                    _emit(f"\n  [server-tool] {block.get('name')} ...")
            elif t == "content_block_delta":
                idx = ev["index"]
                delta = ev.get("delta", {})
                dt = delta.get("type")
                if dt == "text_delta":
                    txt = delta.get("text", "")
                    if txt:
                        _emit(txt, end="")
                    blocks.setdefault(idx, {"type": "text", "text": ""})
                    blocks[idx]["text"] = blocks[idx].get("text", "") + txt
                elif dt == "thinking_delta":
                    blocks.setdefault(idx, {"type": "thinking", "thinking": ""})
                    blocks[idx]["thinking"] = blocks[idx].get("thinking", "") + delta.get("thinking", "")
                elif dt == "signature_delta":
                    blocks[idx]["signature"] = blocks[idx].get("signature", "") + delta.get("signature", "")
                elif dt == "input_json_delta":
                    blocks[idx]["_input"] = blocks[idx].get("_input", "") + delta.get("partial_json", "")
            elif t == "message_delta":
                stop_reason = ev.get("delta", {}).get("stop_reason")
                u = ev.get("usage", {})
                out = u.get("output_tokens")
                if out is not None:
                    usage["output_tokens"] = out
                if u.get("cache_creation_input_tokens") is not None:
                    usage["cache_write"] = u["cache_creation_input_tokens"]
                if u.get("cache_read_input_tokens") is not None:
                    usage["cache_read"] = u["cache_read_input_tokens"]
            elif t == "error":
                raise SystemExit("Model error: " + json.dumps(ev))

    content = []
    for idx in order:
        b = blocks[idx]
        if b.get("type") in ("tool_use", "server_tool_use"):
            raw_in = b.pop("_input", "")
            if raw_in.strip():
                try:
                    b["input"] = json.loads(raw_in)
                except json.JSONDecodeError:
                    # Output token cap hit mid tool call: JSON truncated. Flag it so the
                    # loop reports a clean error instead of executing a broken call or
                    # crashing in json.loads.
                    b["input"] = {}
                    b["_truncated"] = True
            else:
                b["input"] = {}
        content.append(b)
    return content, stop_reason, usage


# ---------------- tools ----------------
def expand_path(p: str) -> str:
    p = (p or "").strip() or "."
    if os.path.isabs(p):
        return p
    return os.path.abspath(os.path.join(STATE.get("cwd") or os.getcwd(), p))


def run_shell(command: str, cwd: str = None, timeout_s: int = 120):
    cwd = cwd or STATE.get("cwd") or os.getcwd()
    p = subprocess.run(command, shell=True, cwd=cwd, capture_output=True,
                       text=True, timeout=timeout_s)
    return {"exit_code": p.returncode,
            "stdout": p.stdout[-20000:], "stderr": p.stderr[-20000:]}


def read_file(path: str, max_chars: int = 20000):
    p = expand_path(path)
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        data = f.read()
    return {"path": p, "chars": len(data), "content": data[:max_chars]}


def write_file(path: str, content: str):
    p = expand_path(path)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return {"path": p, "bytes": len(content.encode("utf-8"))}


def list_dir(path: str = "."):
    p = expand_path(path)
    if not os.path.isdir(p):
        return {"error": "not a directory", "path": p}
    entries = [("D " if os.path.isdir(os.path.join(p, n)) else "F ") + n
               for n in sorted(os.listdir(p))]
    return {"path": os.path.abspath(p), "count": len(entries), "entries": entries[:500]}


def http_request(method: str, url: str, headers: dict = None,
                 body: str = None, timeout_s: int = 30):
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper(),
                                 headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return {"status": r.status, "body": r.read(20000).decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read(20000).decode("utf-8", "replace")}


def get_current_datetime():
    return {"datetime": datetime.now().isoformat(timespec="seconds")}


# ---------------- prompt caching ----------------
def _apply_cache_control(payload: dict):
    """Add ephemeral cache breakpoints so the tools+system prefix and the growing
    conversation are billed as cheap cache reads instead of fresh input on every
    agent step. Two breakpoints: end of system (covers tools+system, which is
    static for the whole job) and end of the last message (rolls forward as the
    conversation grows). Stale breakpoints are stripped first so we never leave
    more than these two in a request."""
    if not STATE.get("cache", True):
        return
    system = payload.get("system")
    if isinstance(system, str) and system:
        payload["system"] = [{"type": "text", "text": system,
                              "cache_control": {"type": "ephemeral"}}]
    elif isinstance(system, list) and system:
        for b in system:
            if isinstance(b, dict):
                b.pop("cache_control", None)
        if isinstance(system[-1], dict):
            system[-1]["cache_control"] = {"type": "ephemeral"}
    for m in payload.get("messages") or []:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    b.pop("cache_control", None)
    msgs = payload.get("messages") or []
    if msgs:
        last = msgs[-1]
        c = last.get("content")
        if isinstance(c, str) and c:
            last["content"] = [{"type": "text", "text": c,
                                "cache_control": {"type": "ephemeral"}}]
        elif isinstance(c, list) and c and isinstance(c[-1], dict):
            c[-1]["cache_control"] = {"type": "ephemeral"}


def _add_cost(cost: dict, usage: dict):
    if cost is None:
        return
    cost["in"] += usage.get("input_tokens", 0)
    cost["out"] += usage.get("output_tokens", 0)
    cost["cache_write"] += usage.get("cache_write", 0)
    cost["cache_read"] += usage.get("cache_read", 0)


# ---------------- subagents + agent loop ----------------
def agent_loop(task, *, system, model, tools, messages, max_steps=MAX_STEPS,
               cost=None, quiet=False):
    key = resolve_api_key()
    messages.append({"role": "user", "content": task})
    steps = 0
    while max_steps <= 0 or steps < max_steps:
        steps += 1
        payload = {"model": model, "max_tokens": STATE["max_tokens"],
                   "system": system, "tools": tools, "messages": messages}
        if STATE.get("effort"):
            payload["thinking"] = {"type": "adaptive"}
            payload["output_config"] = {"effort": STATE["effort"]}
        _apply_cache_control(payload)
        content, stop_reason, usage = call_model(key, payload, quiet=quiet)
        _add_cost(cost, usage)
        messages.append({"role": "assistant", "content": content})
        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        if not tool_uses:
            # No tool call this round. Covers normal completion and a text-only
            # response cut off by the token cap (nothing left to execute).
            if stop_reason == "max_tokens":
                _emit("\n  [warn] response hit the max_tokens cap; if the answer looks "
                      "cut off, raise -t or ask for smaller chunks")
            return "".join(b.get("text", "") for b in content if b.get("type") == "text")
        if stop_reason == "max_tokens":
            # Cut off mid response while emitting tool calls. Valid calls still run;
            # truncated ones get a clean error below so the model retries smaller.
            _emit("\n  [warn] response hit the max_tokens cap mid tool call")
        results = []
        for tu in tool_uses:
            name = tu["name"]
            args = tu.get("input", {}) or {}
            if tu.get("_truncated"):
                results.append({"type": "tool_result", "tool_use_id": tu["id"],
                                "content": ("[not executed] the tool-call JSON was truncated because "
                                            "the output token cap was hit mid call. Retry with a smaller "
                                            "payload (write files in smaller chunks or sections); the "
                                            "caller can also raise the cap with -t."),
                                "is_error": True})
                continue
            if needs_confirm(name, args) and not STATE["yolo"]:
                if not confirm(name, args):
                    results.append({"type": "tool_result", "tool_use_id": tu["id"],
                                    "content": "[denied by user]", "is_error": True})
                    continue
            fn = TOOL_IMPL.get(name)
            if fn is None:
                out, is_err = {"error": f"unknown tool {name}"}, True
            else:
                try:
                    out, is_err = fn(**args), False
                except Exception as e:  # noqa: BLE001
                    out, is_err = {"error": repr(e)}, True
            out_text = to_text(out)
            _emit(f"     -> {out_text[:300]}")
            results.append({"type": "tool_result", "tool_use_id": tu["id"],
                            "content": out_text, "is_error": is_err})
        messages.append({"role": "user", "content": results})
    # Step budget exhausted. Instead of returning a useless marker, make one final
    # tool-free call so the model can salvage and report its partial results.
    _emit(f"\n  [drain] step budget of {max_steps} exhausted; requesting a final write-up")
    salvage = ("You have used all " + str(max_steps) + " agent steps and no more tool calls are allowed. "
               "Produce your final answer now: report exactly what you completed, the concrete "
               "findings or content you already produced, and what remains to be done. If a file "
               "was the expected output, say precisely what is in it and what is missing.")
    try:
        payload = {"model": model, "max_tokens": STATE.get("max_tokens", 8192),
                   "system": system,
                   "messages": messages + [{"role": "user", "content": salvage}]}
        if STATE.get("effort"):
            payload["thinking"] = {"type": "adaptive"}
            payload["output_config"] = {"effort": STATE["effort"]}
        _apply_cache_control(payload)
        content, _stop, usage = call_model(key, payload, quiet=quiet)
        _add_cost(cost, usage)
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        return text or "(stopped: reached max steps; salvage call returned no text)"
    except Exception:  # noqa: BLE001
        return "(stopped: reached max steps; final salvage call failed)"


SUBAGENTS = {
    "researcher": {
        "system": "You are a research subagent. Use web search to find current, factual "
                  "information. Cite each fact with its source. Return a concise, "
                  "well-organized answer the main agent can relay.",
        "tools": ["web_search"],
    },
    "code_reviewer": {
        "system": "You are a code-review subagent. Review the provided code or file "
                  "paths for bugs, security issues, and clarity. Be specific and "
                  "actionable; reference file/line where possible.",
        "tools": ["read_file", "list_dir", "get_current_datetime"],
    },
    "general": {
        "system": "You are a focused subagent. Complete the given subtask using the "
                  "tools available to you and return a concise result for the main agent.",
        "tools": ["run_shell", "read_file", "write_file", "list_dir",
                  "http_request", "get_current_datetime"],
    },
}


def subagent_tools(names):
    names = set(names) - {"spawn_subagent"}
    tools = [t for t in TOOLS if t["name"] in names]
    if "web_search" in names:
        tools = tools + [WEB_SEARCH_TOOL]
    return tools


def spawn_subagent(task, role="general"):
    role = (role or "general").strip().lower()
    role_def = SUBAGENTS.get(role)
    if role_def is None:
        return {"error": f"unknown role '{role}'. Available: " + ", ".join(SUBAGENTS)}
    system = role_def["system"] + f" Current working directory: {STATE.get('cwd') or os.getcwd()}."
    tools = subagent_tools(role_def["tools"])
    _emit(f"\n  [subagent:{role}] {str(task)[:120]}")
    result = agent_loop(task, system=system, model=STATE["model"], tools=tools,
                        messages=[], cost=STATE["cost"], quiet=True,
                        max_steps=STATE.get("max_steps", MAX_STEPS))
    _emit(f"  [subagent:{role}] done ({len(result)} chars)")
    return {"role": role, "result": result}


TOOL_IMPL = {
    "run_shell": run_shell,
    "read_file": read_file,
    "write_file": write_file,
    "list_dir": list_dir,
    "http_request": http_request,
    "get_current_datetime": get_current_datetime,
    "spawn_subagent": spawn_subagent,
}

TOOLS = [
    {"name": "run_shell",
     "description": "Run a shell command via the system shell; return exit_code, stdout, stderr. For builds, tests, git, installs, etc.",
     "input_schema": {"type": "object", "properties": {
        "command": {"type": "string", "description": "The command line to run."},
        "cwd": {"type": "string", "description": "Optional working directory."},
        "timeout_s": {"type": "integer", "description": "Timeout seconds (default 120)."}},
        "required": ["command"]}},
    {"name": "read_file",
     "description": "Read a text file and return its contents (truncated).",
     "input_schema": {"type": "object", "properties": {
        "path": {"type": "string"}, "max_chars": {"type": "integer"}},
        "required": ["path"]}},
    {"name": "write_file",
     "description": "Create or overwrite a UTF-8 text file (parent directories are created).",
     "input_schema": {"type": "object", "properties": {
        "path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"]}},
    {"name": "list_dir",
     "description": "List files and subdirectories at a path (default '.').",
     "input_schema": {"type": "object", "properties": {
        "path": {"type": "string"}}, "required": []}},
    {"name": "http_request",
     "description": "Make an HTTP request; return status and (truncated) body. For calling APIs.",
     "input_schema": {"type": "object", "properties": {
        "method": {"type": "string"}, "url": {"type": "string"},
        "headers": {"type": "object"}, "body": {"type": "string"},
        "timeout_s": {"type": "integer"}},
        "required": ["method", "url"]}},
    {"name": "get_current_datetime",
     "description": "Get the current local date and time (ISO 8601).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "spawn_subagent",
     "description": "Delegate a subtask to a focused subagent (own context; returns a concise result). Roles: researcher (live web search), code_reviewer (review code/files), general (full tools). Use to isolate or parallelize work.",
     "input_schema": {"type": "object", "properties": {
        "task": {"type": "string", "description": "The specific subtask to complete."},
        "role": {"type": "string", "description": "researcher | code_reviewer | general (default general)."}},
        "required": ["task"]}},
]

# Anthropic server-side live web search (no beta header needed for this key).
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}


def main_tools():
    tools = list(TOOLS)
    if STATE.get("web_search", True):
        tools = tools + [WEB_SEARCH_TOOL]
    return tools


def needs_confirm(name: str, args: dict) -> bool:
    if name in ("run_shell", "write_file"):
        return True
    if name == "http_request":
        return str(args.get("method", "GET")).upper() not in ("GET", "HEAD")
    return False


# ---------------- helpers ----------------
def to_text(o):
    if isinstance(o, str):
        return o
    try:
        return json.dumps(o, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(o)


def confirm(name: str, args: dict) -> bool:
    if STATE.get("quiet"):
        return False  # non-interactive (e.g. --json): auto-deny destructive tools, never prompt
    preview = json.dumps(args, ensure_ascii=False)
    if len(preview) > 500:
        preview = preview[:500] + " …"
    print(f"  [confirm] {name}  {preview}", flush=True)
    try:
        ans = input("    run? [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def system_prompt() -> str:
    return (
        "You are an agentic CLI assistant. Complete tasks using the available tools, "
        "step by step: inspect before you change things, prefer small and safe commands, "
        "and verify results. You can run shell commands, read/write files, list directories, "
        "and make HTTP requests. For large or parallelizable work, delegate a subtask to a "
        "focused subagent with spawn_subagent (roles: researcher, code_reviewer, general). "
        f"Current working directory: {STATE.get('cwd') or os.getcwd()}. When finished, give a short summary."
    )


# ---------------- the agent loop ----------------
def run_turn(task: str):
    agent_loop(task, system=system_prompt(), model=STATE["model"],
               tools=main_tools(), messages=STATE["messages"],
               cost=STATE["cost"], quiet=False,
               max_steps=STATE.get("max_steps", MAX_STEPS))
    print("\n", flush=True)


# ---------------- REPL + commands ----------------
def banner():
    print("claude_cli - agentic harness (stdlib only)")
    print(f"  model   : {STATE['model']}   effort: {STATE.get('effort') or 'off'}")
    print(f"  cwd     : {STATE['cwd']}")
    print(f"  yolo    : {'ON (no confirm)' if STATE['yolo'] else 'off (confirm destructive)'}")
    print(f"  search  : {'on' if STATE.get('web_search', True) else 'off'} (Anthropic web_search)")
    print(f"  tools   : {', '.join(t['name'] for t in TOOLS)}")
    print("  suggestions appear as you type; Tab accepts, Up/Down history, Escape clears\n")


def do_command(cmd: str):
    parts = cmd.split(maxsplit=1)
    name = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    if name in ("/quit", "/exit", "/q"):
        raise SystemExit(0)
    if name == "/help":
        print(__doc__.strip())
    elif name == "/model":
        if arg:
            STATE["model"] = resolve_model(arg)
            print(f"model -> {STATE['model']}")
        else:
            print(f"current : {STATE['model']}")
            print("aliases : " + "  ".join(f"{k}={v}" for k, v in MODEL_ALIASES.items()))
            print("or a full id (see /models).  e.g.  /model opus   /model sonnet   /model haiku")
    elif name == "/models":
        for m in list_models_api(resolve_api_key()):
            print(f"  {m['id']:<28} {m.get('display_name', '')}")
    elif name == "/tools":
        for t in TOOLS:
            print(f"  {t['name']:<22} {t['description']}")
    elif name == "/subagents":
        for r, d in SUBAGENTS.items():
            print(f"  {r:<14} tools: {', '.join(d['tools'])}")
    elif name == "/cwd":
        if arg:
            STATE["cwd"] = os.path.abspath(arg)
            print(f"cwd -> {STATE['cwd']}")
        else:
            print(f"cwd = {STATE['cwd']}")
    elif name == "/ws":
        ws = load_workspaces()
        if not arg:
            print(f"current : {STATE['cwd']}")
            if ws:
                print("saved   :")
                for k in sorted(ws):
                    mark = "*" if os.path.abspath(ws[k]) == STATE["cwd"] else " "
                    print(f"  {mark} {k:<10} {ws[k]}")
            else:
                print("saved   : (none yet; /ws add <name> <path>)")
        elif arg.startswith("add "):
            bits = arg.split(None, 2)
            if len(bits) < 3:
                print("usage: /ws add <name> <path>")
            else:
                wsn, wsp = bits[1].strip(), bits[2].strip()
                ws[wsn] = os.path.abspath(wsp)
                if save_workspaces(ws):
                    print(f"added {wsn} -> {ws[wsn]}")
                else:
                    print("failed to save workspaces.json")
        elif arg.startswith("rm "):
            wsn = arg.split(None, 1)[1].strip()
            if wsn in ws:
                del ws[wsn]
                save_workspaces(ws)
                print(f"removed {wsn}")
            else:
                print(f"no workspace named '{wsn}'")
        else:
            target = ws.get(arg)
            if target is None:
                print(f"no workspace named '{arg}'. See /ws for the list.")
            else:
                switch_workspace(target)
    elif name == "/effort":
        if arg in ("low", "medium", "high"):
            STATE["effort"] = arg
            print(f"effort -> {arg} (adaptive thinking on)")
        elif arg in ("off", "default"):
            STATE["effort"] = None
            print("effort -> off (no thinking config sent)")
        else:
            print(f"current : {STATE.get('effort') or 'off'}")
            print("usage   : /effort low | medium | high | off")
    elif name == "/yolo":
        STATE["yolo"] = not STATE["yolo"]
        print(f"yolo -> {STATE['yolo']}")
    elif name == "/search":
        if arg in ("on", "off"):
            STATE["web_search"] = (arg == "on")
        else:
            STATE["web_search"] = not STATE.get("web_search", True)
        print(f"web_search -> {STATE['web_search']}")
    elif name == "/reset":
        STATE["messages"] = []
        print("conversation cleared")
    elif name == "/cost":
        c = STATE["cost"]
        print(f"tokens  in={c['in']}  out={c['out']}  "
              f"cache_write={c.get('cache_write', 0)}  cache_read={c.get('cache_read', 0)}")
    elif name == "/cache":
        if arg in ("on", "off"):
            STATE["cache"] = (arg == "on")
        else:
            STATE["cache"] = not STATE.get("cache", True)
        print(f"prompt caching -> {'on' if STATE.get('cache', True) else 'off'}")
    elif name == "/save":
        p = save_session(arg or None)
        print(f"saved -> {p}" if p else "save failed")
    elif name == "/load":
        p = load_session(arg or None)
        if p:
            print(f"loaded {len(STATE['messages'])} messages from {p}")
        else:
            print(f"no session to load (looked at {arg or default_session_path()})")
    else:
        print(f"unknown command {name} — try /help")


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def default_session_path():
    return os.path.join(STATE.get("cwd") or os.getcwd(), ".claude_session.json")


def save_session(path=None):
    p = path or default_session_path()
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"model": STATE["model"], "effort": STATE.get("effort"),
                       "messages": STATE["messages"]}, f, ensure_ascii=False)
        return p
    except Exception:  # noqa: BLE001
        return None


def load_session(path=None):
    p = path or default_session_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        STATE["messages"] = data.get("messages", [])
        if data.get("model"):
            STATE["model"] = data["model"]
        STATE["effort"] = data.get("effort")
        return p
    except Exception:  # noqa: BLE001
        return None


def workspaces_path():
    return os.path.join(SCRIPT_DIR, "workspaces.json")


def load_workspaces():
    p = workspaces_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {k: str(v) for k, v in data.items()}
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_workspaces(ws):
    try:
        with open(workspaces_path(), "w", encoding="utf-8") as f:
            json.dump(ws, f, ensure_ascii=False, indent=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def switch_workspace(target):
    target = os.path.abspath(target)
    if not os.path.isdir(target):
        print(f"folder does not exist: {target}")
        return
    if STATE["messages"]:
        save_session()  # saves the current conversation into the current (old) folder
    STATE["cwd"] = target
    STATE["messages"] = []
    STATE["cost"] = {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0}
    STATE["resumed"] = False
    p = load_session()
    if p is not None and STATE["messages"]:
        STATE["resumed"] = True
        print(f"workspace -> {target}  (resumed {len(STATE['messages'])} message(s))")
    else:
        print(f"workspace -> {target}  (fresh session)")


# ---------------- tab completion + history ----------------
SLASH_COMMANDS = [
    "/help", "/model", "/models", "/tools", "/subagents", "/cwd", "/ws",
    "/effort", "/yolo", "/search", "/reset", "/cost", "/cache", "/save", "/load", "/quit",
]

COMPLETION_WORDS = (
    list(MODEL_ALIASES)
    + list(MODEL_ALIASES.values())
    + ["low", "medium", "high", "off", "on", "add", "rm"]
)


def _completer(text, state):
    text = (text or "").strip()
    if text.startswith("/"):
        opts = [c for c in SLASH_COMMANDS if c.startswith(text)]
    elif text:
        pool = set(COMPLETION_WORDS) | set(load_workspaces())
        opts = sorted(w for w in pool if w.startswith(text))
    else:
        opts = sorted(SLASH_COMMANDS)
    if state < len(opts):
        return opts[state]
    return None


HISTORY_FILE = os.path.join(SCRIPT_DIR, ".vibe_history")
_readline = None


def setup_readline():
    """Tab completion + line history. Only on a real console, so piped input keeps working."""
    global _readline
    if not sys.stdin.isatty():
        return
    try:
        import readline as rl
    except ImportError:
        return
    try:
        if os.path.exists(HISTORY_FILE):
            rl.read_history_file(HISTORY_FILE)
        rl.set_completer(_completer)
        rl.set_completer_delims(" \t")  # keep "/" inside the word so /model completes whole
        rl.parse_and_bind("tab: complete")
    except Exception:  # noqa: BLE001
        return
    _readline = rl


def save_readline_history():
    if _readline is not None:
        try:
            _readline.write_history_file(HISTORY_FILE)
        except Exception:  # noqa: BLE001
            pass


# ---------------- live suggestions (menu while typing) ----------------
def _can_live_input():
    if not sys.stdin.isatty():
        return False
    try:
        import msvcrt  # noqa: F401
        return True
    except ImportError:
        return False


def _load_history_list():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8", errors="replace") as f:
                return [ln.rstrip("\r\n") for ln in f if ln.strip()]
        except Exception:  # noqa: BLE001
            pass
    return []


def _save_history_list(lines):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            for ln in lines[-500:]:
                f.write(ln + "\n")
    except Exception:  # noqa: BLE001
        pass


def _stdin_raw_mode():
    """Turn off driver echo + line buffering so we receive every keystroke.
    ENABLE_PROCESSED_INPUT stays ON so special keys (arrows, Home, End, Del)
    keep their standard 0x00/0xE0 extended format and Ctrl+C is still handled.
    Returns (k32, handle, previous_mode) or None if not a real console."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            prev = mode.value
            # off: ENABLE_LINE_INPUT(2) ENABLE_ECHO_INPUT(4) ENABLE_WINDOW_INPUT(8)
            # on : ENABLE_PROCESSED_INPUT(1)
            k32.SetConsoleMode(h, prev & ~(0x2 | 0x4 | 0x8))
            return (k32, h, prev)
    except Exception:  # noqa: BLE001
        pass
    return None


def _restore_stdin_mode(state):
    if state is None:
        return
    k32, h, prev = state
    try:
        k32.SetConsoleMode(h, prev)
    except Exception:  # noqa: BLE001
        pass


def _console_pos():
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        class CO(ctypes.Structure):
            _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short)]
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_short), ("top", ctypes.c_short),
                        ("right", ctypes.c_short), ("bottom", ctypes.c_short)]
        class INFO(ctypes.Structure):
            _fields_ = [("dwSize", CO), ("dwCursorPos", CO), ("wAttributes", ctypes.c_ushort),
                        ("srWindow", RECT), ("dwMaximumWindowSize", CO)]
        info = INFO()
        if not k32.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
            return None
        return info
    except Exception:  # noqa: BLE001
        return None


def _console_width():
    # Visible window width (not the scrollback buffer width) minus one column
    # of safety, so padded lines and menu lines never wrap. srWindow is a
    # SMALL_RECT: left/top/right/bottom (right and bottom are inclusive).
    info = _console_pos()
    if info is not None:
        try:
            w = int(info.srWindow.right) - int(info.srWindow.left) + 1
            if w > 10:
                return max(20, w - 1)
        except Exception:  # noqa: BLE001
            pass
    return 100


def _move_cursor(dx=0, dy=0):
    try:
        import ctypes
        info = _console_pos()
        if info is None:
            return
        x = max(0, int(info.dwCursorPos.x) + dx)
        y = max(0, int(info.dwCursorPos.y) + dy)
        class CO(ctypes.Structure):
            _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short)]
        ctypes.windll.kernel32.SetConsoleCursorPosition(
            ctypes.windll.kernel32.GetStdHandle(-11), CO(x, y))
    except Exception:  # noqa: BLE001
        pass


def _live_candidates(line):
    if not line.strip():
        return []
    i = len(line)
    while i > 0 and line[i - 1] not in " \t":
        i -= 1
    tok = line[i:]
    if line.startswith("/") and i == 0:
        return [c for c in SLASH_COMMANDS if c.startswith(line)]
    min_len = 1 if line.startswith("/") else 2
    if len(tok) >= min_len:
        pool = set(COMPLETION_WORDS) | set(load_workspaces())
        return sorted(w for w in pool if w.startswith(tok))
    return []


def live_input(prompt, history):
    """Read one line with a live suggestion menu under the prompt (as-you-type)."""
    import msvcrt
    buf = []
    pos = 0
    draft = None
    hist_idx = None
    original = None
    cycle_cands = None
    menu_idx = 0
    panel_lines = 0
    esc_state = 0  # 0 = none, 1 = saw ESC, 2 = saw ESC [
    esc_time = 0.0
    width = _console_width()
    out = sys.stdout
    raw_state = _stdin_raw_mode()

    def reset_tab_cycle():
        nonlocal original, cycle_cands, menu_idx
        original = None
        cycle_cands = None
        menu_idx = 0

    def full_reset():
        nonlocal draft, hist_idx
        draft = None
        hist_idx = None
        reset_tab_cycle()

    def erase_panel():
        # invariant: cursor is on the prompt line when this is called
        nonlocal panel_lines
        if panel_lines <= 0:
            return
        _move_cursor(dy=panel_lines)
        for i in range(panel_lines):
            out.write("\r" + " " * width)
            if i < panel_lines - 1:
                _move_cursor(dy=-1)
        _move_cursor(dy=-1)
        panel_lines = 0

    def render():
        # ends with the cursor on the prompt line, at the text cursor column
        nonlocal panel_lines
        erase_panel()
        line = "".join(buf)
        total = len(prompt) + len(line)
        if total <= width:
            out.write("\r" + prompt + line + " " * (width - total))
            target = len(prompt) + pos
            if target != width:
                _move_cursor(dx=target - width)
            cands = _live_candidates(line)
        else:
            # line is longer than the window: just show it, skip the menu
            out.write("\r" + prompt + line)
            target = len(prompt) + pos
            cands = []
        panel = []
        if cands:
            shown = cands[:8]
            if len(cands) > 8:
                shown = shown + ["\u2026"]
            text = "  " + "   ".join(shown)
            panel = [text[i:i + width] for i in range(0, len(text), width)][:4]
        for p in panel:
            out.write("\n" + p)
        panel_lines = len(panel)
        if panel_lines:
            _move_cursor(dy=-panel_lines)
            _move_cursor(dx=target - len(panel[-1]))
        out.flush()

    def history_back():
        nonlocal draft, hist_idx, pos
        if not history:
            return
        if draft is None:
            draft = list(buf)
            hist_idx = len(history) - 1
        elif hist_idx > 0:
            hist_idx -= 1
        buf[:] = list(history[hist_idx])
        pos = len(buf)

    def history_forward():
        nonlocal draft, hist_idx, pos
        if draft is None:
            return
        if hist_idx is not None and hist_idx < len(history) - 1:
            hist_idx += 1
            buf[:] = list(history[hist_idx])
        else:
            buf[:] = draft
            draft = None
            hist_idx = None
        pos = len(buf)

    def token_start(text):
        i = len(text)
        while i > 0 and text[i - 1] not in " \t":
            i -= 1
        return i

    try:
      render()
      while True:
        try:
            if esc_state == 1 and (time.time() - esc_time) > 0.08:
                # standalone ESC with no following key: clear the line
                esc_state = 0
                buf.clear()
                pos = 0
                full_reset()
                render()
                continue
            if not msvcrt.kbhit():
                time.sleep(0.01)
                continue
            ch = msvcrt.getwch()
        except KeyboardInterrupt:
            # Ctrl+C (arrives as a signal while PROCESSED_INPUT is on)
            if buf:
                buf.clear()
                pos = 0
                esc_state = 0
                full_reset()
                render()
                continue
            out.write("\r\n")
            out.flush()
            raise

        if ch in ("\x00", "\xe0"):
            # extended key (arrows, Home, End, Del, ...): read the code byte
            if not msvcrt.kbhit():
                continue  # stray prefix with no key following; ignore, never block
            ext = msvcrt.getwch()
            esc_state = 0
            if ext == "K":
                pos = max(0, pos - 1)
            elif ext == "M":
                pos = min(len(buf), pos + 1)
            elif ext == "G":
                pos = 0
            elif ext == "O":
                pos = len(buf)
            elif ext == "S":
                if pos < len(buf):
                    del buf[pos]
            elif ext == "H":
                history_back()
            elif ext == "P":
                history_forward()
            else:
                continue
            reset_tab_cycle()
            render()
            continue

        if ch == "\x1b":
            esc_state = 1
            esc_time = time.time()
            continue

        if esc_state == 1 and ch == "[":
            esc_state = 2
            continue
        if esc_state == 2 and ch in ("A", "B", "C", "D"):
            # VT-style arrow sequence (some hosts deliver these instead of 0x00 codes)
            esc_state = 0
            if ch == "A":
                history_back()
            elif ch == "B":
                history_forward()
            elif ch == "D":
                pos = max(0, pos - 1)
            else:
                pos = min(len(buf), pos + 1)
            reset_tab_cycle()
            render()
            continue
        if esc_state:
            # ESC was not part of an arrow sequence: treat it as standalone
            # (clear the line), then handle this key normally below
            esc_state = 0
            buf.clear()
            pos = 0
            full_reset()
            render()

        if ch == "\r":
            erase_panel()
            out.write("\r\n")
            out.flush()
            line = "".join(buf).strip()
            if line:
                if not history or history[-1] != line:
                    history.append(line)
                    _save_history_list(history)
            return line

        if ch == "\t":
            line = "".join(buf)
            if original is None:
                cands = _live_candidates(line)
                if not cands:
                    continue
                original = list(buf)
                cycle_cands = cands
                menu_idx = 0
            else:
                menu_idx += 1
                if menu_idx >= len(cycle_cands):
                    buf[:] = original
                    original = None
                    cycle_cands = None
                    menu_idx = 0
                    render()
                    continue
            start = token_start("".join(original))
            choice = cycle_cands[menu_idx]
            buf[start:] = list(choice)
            if line.startswith("/"):
                buf.append(" ")
            pos = len(buf)
            render()
            continue

        if ch == "\x08":
            if pos > 0:
                del buf[pos - 1]
                pos -= 1
            reset_tab_cycle()
            render()
            continue

        if ch == "\x03":
            # raw Ctrl+C character (hosts that deliver it unprocessed)
            if buf:
                buf.clear()
                pos = 0
                full_reset()
                render()
                continue
            out.write("\r\n")
            out.flush()
            raise KeyboardInterrupt

        if ch == "\x04":
            if buf:
                if pos < len(buf):
                    del buf[pos]
                reset_tab_cycle()
                render()
                continue
            raise EOFError

        if len(ch) == 1 and ch >= " ":
            buf.insert(pos, ch)
            pos += 1
            full_reset()
            render()
            continue
        # any other control key: ignore
    finally:
        _restore_stdin_mode(raw_state)


def repl():
    LIVE = _can_live_input()
    if LIVE:
        history = _load_history_list()
    else:
        history = None
        setup_readline()
    banner()
    if STATE.get("resumed") and STATE.get("messages"):
        print(f"  (resumed {len(STATE['messages'])} prior message(s); /reset to start fresh)\n")
    while True:
        try:
            if LIVE:
                try:
                    line = live_input("Kyle> ", history)
                except Exception as e:  # noqa: BLE001
                    # live path failed; fall back to plain input so the REPL keeps working
                    sys.stdout.write("\r\n[live input disabled this run: %r]\n" % (e,))
                    sys.stdout.flush()
                    line = input("Kyle> ")
            else:
                line = input("Kyle> ")
            line = line.strip()
        except (EOFError, KeyboardInterrupt):
            print()
            save_readline_history()
            break
        if not line:
            continue
        if line.startswith("/"):
            try:
                do_command(line)
            except SystemExit:
                save_session()
                save_readline_history()
                raise
            except Exception as e:  # noqa: BLE001
                print(f"  ! {e!r}")
            save_session()
            continue
        try:
            run_turn(line)
        except KeyboardInterrupt:
            print("\n(interrupted)")
        except Exception as e:  # noqa: BLE001
            print(f"  ! error: {e!r}")
        save_session()


# ============================ RPC mode (Harness Remote bridge) ============================
# Long-lived process that speaks the pi RPC wire protocol over stdin/stdout so the
# Harness Remote bridge (and thus the phone) can drive this harness. Same records as
# bridge/bionic-driver.mjs: one JSON object per line, nothing else on stdout.
#   stdin  : prompt / abort / extension_ui_response / set_thinking_level
#   stdout : agent_start, turn_start, message_update, tool_execution_*, agent_settled,
#            extension_ui_request, response, note
# Everything else (progress, logs) goes to stderr.
import threading
import time as _time

RPC = {
    "abort": threading.Event(),          # set -> stop the current run
    "ask": None,                          # {"event": threading.Event(), "result": {...}} while a question is open
    "q": __import__("queue").Queue(),     # prompt queue (one worker, FIFO)
    "shutdown": False,
}

def rpc_out(rec):
    try:
        sys.stdout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass

def rpc_note(text):
    rpc_out({"type": "note", "text": text})

def rpc_err(command, message):
    rpc_out({"type": "response", "success": False, "command": command, "error": message})

def map_effort(level):
    lvl = (level or "").strip().lower()
    if lvl in ("", "off", "none"): return None
    if lvl in ("minimal", "low"): return "low"
    if lvl == "medium": return "medium"
    return "high"   # high, xhigh, max

def ask_user(title, message, options, allow_all=False):
    """Emit an extension_ui_request and block until the bridge answers.
    Returns True to proceed, False to deny. With allow_all, a 'allow all'
    choice flips STATE['yolo'] for the rest of this process."""
    ask_id = "vibe-" + str(id(object()))
    ev = threading.Event()
    slot = {"event": ev, "result": {}}
    RPC["ask"] = slot
    rpc_out({"type": "extension_ui_request", "id": ask_id, "method": "select",
             "title": title, "message": message, "options": options, "placeholder": ""})
    # Block until the bridge sends extension_ui_response (or we are aborted).
    while not ev.wait(timeout=0.2):
        if RPC["abort"].is_set():
            break
    RPC["ask"] = None
    res = slot["result"]
    if RPC["abort"].is_set() and "value" not in res and "confirmed" not in res and not res.get("cancelled"):
        return False
    if res.get("cancelled"):
        return False
    val = str(res.get("value", res.get("confirmed", "")))
    if allow_all and val.lower().startswith("yes, and allow all"):
        STATE["yolo"] = True
    if val.lower().startswith("yes"):
        return True
    return False

def interruptible_shell(command, cwd, timeout_s):
    """Run a shell command, killing it if the run is aborted or the timeout hits."""
    p = subprocess.Popen(command, shell=True, cwd=cwd, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    bufs = {"out": [], "err": []}
    def drain(stream, key):
        try:
            for line in stream:
                bufs[key].append(line)
        except Exception:  # noqa: BLE001
            pass
    t1 = threading.Thread(target=drain, args=(p.stdout, "out"), daemon=True)
    t2 = threading.Thread(target=drain, args=(p.stderr, "err"), daemon=True)
    t1.start(); t2.start()
    start = _time.time()
    aborted = False
    while p.poll() is None:
        if RPC["abort"].is_set():
            aborted = True
            try: p.kill()
            except Exception: pass
            break
        if _time.time() - start > timeout_s:
            try: p.kill()
            except Exception: pass
            break
        _time.sleep(0.05)
    try:
        p.wait(timeout=3)
    except Exception:  # noqa: BLE001
        pass
    t1.join(timeout=2); t2.join(timeout=2)
    out = "".join(bufs["out"])[-20000:]
    err = "".join(bufs["err"])[-20000:]
    code = -1 if (aborted or p.returncode is None) else p.returncode
    if aborted:
        err = (err + "\n[aborted by user]").strip()
    return {"exit_code": code, "stdout": out, "stderr": err}

def execute_tool(name, args):
    """Run one tool with RPC abort support. Returns (out_text, is_error)."""
    if name == "run_shell":
        cwd = args.get("cwd") or STATE.get("cwd") or os.getcwd()
        timeout_s = int(args.get("timeout_s", 120) or 120)
        r = interruptible_shell(args.get("command", ""), cwd, timeout_s)
        return to_text(r), (r["exit_code"] != 0)
    fn = TOOL_IMPL.get(name)
    if fn is None:
        return to_text({"error": f"unknown tool {name}"}), True
    try:
        return to_text(fn(**args)), False
    except Exception as e:  # noqa: BLE001
        return to_text({"error": repr(e)}), True

def call_model_rpc(key, payload, emit):
    """Stream a Messages API call, emitting pi records via emit(). Returns
    (content_blocks, stop_reason, usage, aborted)."""
    payload = dict(payload)
    payload["stream"] = True
    req = urllib.request.Request(
        API_BASE + "/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json", "accept": "text/event-stream"},
        method="POST")
    blocks = {}
    order = []
    stop_reason = None
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_write": 0, "cache_read": 0}
    aborted = False
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            for raw in resp:
                if RPC["abort"].is_set():
                    aborted = True
                    try: resp.close()
                    except Exception: pass
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                try:
                    ev = json.loads(data)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type")
                if t == "message_start":
                    u = ev.get("message", {}).get("usage", {})
                    usage["input_tokens"] = u.get("input_tokens", 0)
                    usage["cache_write"] = u.get("cache_creation_input_tokens", 0)
                    usage["cache_read"] = u.get("cache_read_input_tokens", 0)
                elif t == "content_block_start":
                    idx = ev["index"]
                    block = dict(ev["content_block"])
                    blocks[idx] = block
                    order.append(idx)
                    if block.get("type") == "tool_use":
                        block["_input"] = ""
                        emit({"type": "message_update", "assistantMessageEvent": {
                            "type": "toolcall_start", "id": block.get("id"),
                            "contentIndex": idx, "toolName": block.get("name")}})
                    elif block.get("type") == "server_tool_use":
                        block["_input"] = ""
                elif t == "content_block_delta":
                    idx = ev["index"]
                    d = ev.get("delta", {})
                    dt = d.get("type")
                    if dt == "text_delta":
                        txt = d.get("text", "")
                        if txt:
                            emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": txt}})
                        blocks.setdefault(idx, {"type": "text", "text": ""})
                        blocks[idx]["text"] = blocks[idx].get("text", "") + txt
                    elif dt == "thinking_delta":
                        th = d.get("thinking", "")
                        if th:
                            emit({"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": th}})
                        blocks.setdefault(idx, {"type": "thinking", "thinking": ""})
                        blocks[idx]["thinking"] = blocks[idx].get("thinking", "") + th
                    elif dt == "signature_delta":
                        blocks[idx]["signature"] = blocks[idx].get("signature", "") + d.get("signature", "")
                    elif dt == "input_json_delta":
                        pj = d.get("partial_json", "")
                        if pj:
                            emit({"type": "message_update", "assistantMessageEvent": {"type": "toolcall_delta", "contentIndex": idx, "delta": pj}})
                        blocks[idx]["_input"] = blocks[idx].get("_input", "") + pj
                elif t == "message_delta":
                    stop_reason = ev.get("delta", {}).get("stop_reason")
                    u = ev.get("usage", {})
                    if u.get("output_tokens") is not None:
                        usage["output_tokens"] = u["output_tokens"]
                    if u.get("cache_creation_input_tokens") is not None:
                        usage["cache_write"] = u["cache_creation_input_tokens"]
                    if u.get("cache_read_input_tokens") is not None:
                        usage["cache_read"] = u["cache_read_input_tokens"]
                elif t == "error":
                    raise SystemExit("Model error: " + json.dumps(ev))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        if aborted:
            return content_finalize(blocks, order), stop_reason, usage, True
        raise SystemExit(f"request failed: {e!r}")
    return content_finalize(blocks, order), stop_reason, usage, aborted

def content_finalize(blocks, order):
    content = []
    for idx in order:
        b = blocks[idx]
        if b.get("type") in ("tool_use", "server_tool_use"):
            raw_in = b.pop("_input", "")
            if raw_in.strip():
                try:
                    b["input"] = json.loads(raw_in)
                except json.JSONDecodeError:
                    b["input"] = {}
                    b["_truncated"] = True
            else:
                b["input"] = {}
        content.append(b)
    return content

def rpc_user_content(text, images):
    """Build the user message content: image blocks (native) + text."""
    content = []
    for img in (images or []):
        if not (img and isinstance(img.get("data"), str)):
            continue
        mt = img.get("mimeType") or "image/jpeg"
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": mt, "data": img["data"]}})
    if text:
        content.append({"type": "text", "text": text})
    if not content:
        content.append({"type": "text", "text": "(empty message)"})
    return content

def run_one_rpc(messages, msg, cost):
    """Run the agent loop for a single prompt. Emits records. Returns None."""
    text = str(msg.get("message", ""))
    images = msg.get("images") or []
    key = resolve_api_key()
    model = STATE["model"]
    tools = main_tools()
    system = system_prompt()
    content = rpc_user_content(text, images)
    if isinstance(content, list):
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": text})
    steps = 0
    max_steps = STATE.get("max_steps", MAX_STEPS)
    while max_steps <= 0 or steps < max_steps:
        steps += 1
        if RPC["abort"].is_set():
            return
        payload = {"model": model, "max_tokens": STATE["max_tokens"],
                   "system": system, "tools": tools, "messages": messages}
        if STATE.get("effort"):
            payload["thinking"] = {"type": "adaptive"}
            payload["output_config"] = {"effort": STATE["effort"]}
        _apply_cache_control(payload)
        blocks, stop_reason, usage, aborted = call_model_rpc(key, payload, rpc_out)
        _add_cost(cost, usage)
        messages.append({"role": "assistant", "content": blocks})
        tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
        if aborted:
            return
        if not tool_uses:
            return
        results = []
        for tu in tool_uses:
            name = tu["name"]
            args = tu.get("input", {}) or {}
            if RPC["abort"].is_set():
                return
            if tu.get("_truncated"):
                results.append({"type": "tool_result", "tool_use_id": tu["id"],
                                "content": "[not executed] tool-call JSON was truncated by the output token cap; retry smaller",
                                "is_error": True})
                continue
            # Stream the tool start before we run it.
            rpc_out({"type": "tool_execution_start", "toolCallId": tu["id"],
                     "toolName": name, "args": args})
            t0 = _time.time()
            if needs_confirm(name, args) and not STATE["yolo"]:
                preview = json.dumps(args, ensure_ascii=False)
                if len(preview) > 400:
                    preview = preview[:400] + " ..."
                ok = ask_user("Allow " + name + "?", name + "\n" + preview,
                              ["Yes, run it", "Yes, and allow all this session", "No"],
                              allow_all=True)
                if not ok:
                    results.append({"type": "tool_result", "tool_use_id": tu["id"],
                                    "content": "[denied by user]", "is_error": True})
                    rpc_out({"type": "tool_execution_end", "toolCallId": tu["id"],
                             "toolName": name, "isError": True,
                             "durationMs": int((_time.time() - t0) * 1000),
                             "result": {"content": [{"type": "text", "text": "[denied by user]"}]}})
                    continue
            out_text, is_err = execute_tool(name, args)
            dur = int((_time.time() - t0) * 1000)
            rpc_out({"type": "tool_execution_end", "toolCallId": tu["id"], "toolName": name,
                     "isError": bool(is_err), "durationMs": dur,
                     "result": {"content": [{"type": "text", "text": out_text[:20000]}]}})
            results.append({"type": "tool_result", "tool_use_id": tu["id"],
                            "content": out_text, "is_error": bool(is_err)})
        messages.append({"role": "user", "content": results})

    # Step budget exhausted: salvage.
    if max_steps > 0 and steps >= max_steps:
        rpc_note("Step budget of " + str(max_steps) + " reached; producing a final write-up.")
        try:
            salvage = ("You have used all " + str(max_steps) + " agent steps. Produce your final "
                       "answer now: what you completed, what you found, and what remains.")
            payload = {"model": model, "max_tokens": STATE["max_tokens"], "system": system,
                       "messages": messages + [{"role": "user", "content": salvage}]}
            _apply_cache_control(payload)
            blocks, _sr, usage, _ab = call_model_rpc(key, payload, rpc_out)
            _add_cost(usage, usage) if False else _add_cost(cost, usage)
            messages.append({"role": "assistant", "content": blocks})
        except SystemExit as e:
            rpc_err("salvage", str(e))

def _session_file(session_id):
    base = os.path.expanduser("~/.harness-remote/vibe-sessions")
    safe = (session_id or "default").replace(os.sep, "_")
    return os.path.join(base, safe + ".json")

def load_rpc_session(session_id):
    p = _session_file(session_id)
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        msgs = data.get("messages", [])
        if data.get("model"):
            STATE["model"] = data["model"]
        STATE["effort"] = data.get("effort", STATE.get("effort"))
        return msgs
    except Exception:  # noqa: BLE001
        return []

def save_rpc_session(session_id, messages):
    p = _session_file(session_id)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"model": STATE["model"], "effort": STATE.get("effort"),
                       "messages": messages[-400:]}, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass

def rpc_worker(session_id):
    messages = load_rpc_session(session_id)
    cost = {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0}
    if messages:
        rpc_note("Resumed " + str(len(messages)) + " prior messages.")
    while not RPC["shutdown"]:
        try:
            item = RPC["q"].get(timeout=0.3)
        except Exception:  # noqa: BLE001 (queue.Empty)
            continue
        if item is None:
            break
        RPC["abort"].clear()
        rpc_out({"type": "agent_start"})
        rpc_out({"type": "turn_start"})
        try:
            run_one_rpc(messages, item, cost)
        except SystemExit as e:
            rpc_err("run", str(e))
        except Exception as e:  # noqa: BLE001
            rpc_err("run", repr(e))
        finally:
            save_rpc_session(session_id, messages)
        aborted = RPC["abort"].is_set()
        rpc_out({"type": "agent_settled", **({"aborted": True} if aborted else {})})

def rpc_stdin_reader(session_id):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            rpc_err("parse", "Bad JSON on stdin")
            continue
        t = msg.get("type")
        if t == "prompt":
            RPC["q"].put(msg)
        elif t == "abort":
            # If a question is open, deny it so the worker unblocks, then flag abort.
            slot = RPC["ask"]
            if slot and not slot["event"].is_set():
                slot["result"] = {"cancelled": True}
                slot["event"].set()
            RPC["abort"].set()
        elif t == "extension_ui_response":
            slot = RPC["ask"]
            if slot:
                res = slot["result"]
                if "cancelled" in msg:
                    res["cancelled"] = bool(msg["cancelled"])
                if "confirmed" in msg:
                    res["confirmed"] = bool(msg["confirmed"])
                if "value" in msg:
                    res["value"] = msg["value"]
                slot["event"].set()
        elif t == "set_thinking_level":
            STATE["effort"] = map_effort(msg.get("level"))
            rpc_note("effort -> " + str(STATE["effort"] or "off"))
        else:
            rpc_err(str(t), "vibe mode does not support \"" + str(t) + "\"")
    # stdin closed: shut down the worker.
    RPC["shutdown"] = True
    RPC["q"].put(None)

def rpc_main(session_id):
    STATE["quiet"] = True  # route any stray progress to stderr; stdout stays JSON
    # The bridge sets HARNESS_REMOTE_PERMISSIONS per session: 'auto' means the user
    # chose not to be asked, so skip confirmations (yolo). 'ask' is the safe default.
    if os.environ.get("HARNESS_REMOTE_PERMISSIONS") == "auto":
        STATE["yolo"] = True
    reader = threading.Thread(target=rpc_stdin_reader, args=(session_id,), daemon=True)
    worker = threading.Thread(target=rpc_worker, args=(session_id,), daemon=True)
    worker.start()
    reader.start()
    # Keep the process alive; the bridge keeps stdin open for the session's life.
    try:
        while True:
            _time.sleep(1)
            if RPC["shutdown"] and RPC["q"].empty():
                break
    except KeyboardInterrupt:
        pass
    RPC["shutdown"] = True
    RPC["q"].put(None)
    _time.sleep(0.5)


# ---------------- entrypoint ----------------
def parse_args(argv):
    opts = {"model": DEFAULT_MODEL, "yolo": False, "cwd": os.getcwd(),
            "max_tokens": 8192, "web_search": True, "fresh": False, "json": False,
            "workspace": None, "effort": None, "trace": False,
            "max_steps": MAX_STEPS, "trace_log": None, "cache": True,
            "rpc": False, "session_id": None}
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(__doc__.strip()); sys.exit(0)
        elif a in ("-y", "--yolo"):
            opts["yolo"] = True; i += 1
        elif a in ("-m", "--model"):
            opts["model"] = resolve_model(argv[i + 1]); i += 2
        elif a in ("-e", "--effort"):
            lvl = (argv[i + 1] or "").strip().lower()
            if lvl not in ("low", "medium", "high"):
                print("effort must be: low | medium | high"); sys.exit(2)
            opts["effort"] = lvl; i += 2
        elif a in ("-d", "--cwd"):
            opts["cwd"] = os.path.abspath(argv[i + 1]); i += 2
        elif a in ("-w", "--workspace"):
            opts["workspace"] = argv[i + 1]; i += 2
        elif a in ("-t", "--max-tokens"):
            opts["max_tokens"] = int(argv[i + 1]); i += 2
        elif a == "--no-search":
            opts["web_search"] = False; i += 1
        elif a == "--no-cache":
            opts["cache"] = False; i += 1
        elif a in ("--fresh", "-f"):
            opts["fresh"] = True; i += 1
        elif a == "--json":
            opts["json"] = True; i += 1
        elif a in ("--trace", "-T"):
            opts["trace"] = True; i += 1
        elif a in ("--max-steps", "-s"):
            v = argv[i + 1]
            opts["max_steps"] = 0 if v.lower() == "unlimited" else max(0, int(v)); i += 2
        elif a == "--trace-log":
            opts["trace_log"] = argv[i + 1]; i += 2
        elif a == "--rpc":
            opts["rpc"] = True; i += 1
        elif a == "--session-id":
            opts["session_id"] = argv[i + 1]; i += 2
        elif a.startswith("-"):
            print(f"unknown flag: {a}"); i += 1
        else:
            positional.append(a); i += 1
    return opts, " ".join(positional) if positional else None


def _force_utf8_stdio():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def main():
    _force_utf8_stdio()
    opts, oneshot = parse_args(sys.argv[1:])
    if oneshot and oneshot.lstrip().startswith("#"):
        # cmd.exe passes '#' literally (no shell comments); a pasted comment is not a task -> REPL.
        oneshot = None
    STATE.update(opts)
    STATE["messages"] = []
    STATE["cost"] = {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0}
    if opts.get("workspace"):
        wsn = opts["workspace"].strip()
        target = load_workspaces().get(wsn)
        if target is None:
            raise SystemExit(f"unknown workspace '{wsn}'. Add it with: /ws add {wsn} <path>")
        STATE["cwd"] = os.path.abspath(target)

    if opts.get("rpc"):
        # Long-lived RPC mode for the Harness Remote bridge: no REPL, no cwd
        # session load/save; the conversation persists under --session-id.
        rpc_main(opts.get("session_id"))
        return

    if STATE.get("trace"):
        global _TRACE_LOG_HANDLE
        log_path = STATE.get("trace_log") or os.path.join(SCRIPT_DIR, "vibe_trace.log")
        try:
            _TRACE_LOG_HANDLE = open(log_path, "a", encoding="utf-8")
            _TRACE_LOG_HANDLE.write(
                f"\n[run] {datetime.now().isoformat(timespec='seconds')} "
                f"model={STATE['model']} cwd={STATE['cwd']}\n")
            _TRACE_LOG_HANDLE.flush()
            if STATE.get("quiet"):
                sys.stderr.write(f"[trace log] {log_path}\n")
                sys.stderr.flush()
        except Exception:  # noqa: BLE001
            _TRACE_LOG_HANDLE = None

    if opts.get("json"):
        # Isolated one-shot: no session load/save, quiet, pure JSON on stdout.
        if not oneshot:
            raise SystemExit("--json needs a task. e.g.  py claude_cli.py --json -m sonnet \"summarize X\"")
        STATE["quiet"] = True
        answer = agent_loop(oneshot, system=system_prompt(), model=STATE["model"],
                            tools=main_tools(), messages=[], cost=STATE["cost"], quiet=True,
                            max_steps=STATE.get("max_steps", MAX_STEPS))
        result_line = json.dumps({"model": STATE["model"], "answer": answer,
                                  "input_tokens": STATE["cost"]["in"],
                                  "output_tokens": STATE["cost"]["out"],
                                  "cache_write_tokens": STATE["cost"].get("cache_write", 0),
                                  "cache_read_tokens": STATE["cost"].get("cache_read", 0)},
                                 ensure_ascii=False)
        print(result_line)
        try:
            # Also persist the result for detached/background callers that may not
            # capture stdout (e.g. a start-redirected process).
            with open(os.path.join(SCRIPT_DIR, "vibe_result.json"), "w", encoding="utf-8") as f:
                f.write(result_line + "\n")
        except Exception:  # noqa: BLE001
            pass
        return

    if not STATE.get("fresh"):
        p = load_session()
        if p is not None and STATE["messages"]:
            STATE["resumed"] = True
    if oneshot:
        try:
            run_turn(oneshot)
        finally:
            save_session()
            c = STATE["cost"]
            print(f"\n[session] in={c['in']} out={c['out']} tokens "
                  f"cache(write={c.get('cache_write', 0)} read={c.get('cache_read', 0)}) "
                  f"(auto-saved; use --fresh to start over)")
        return
    repl()


if __name__ == "__main__":
    main()
