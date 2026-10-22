"""Regression test: Git Bash userland tools (grep/sed/head) must work even when the parent
PATH lacks Git's bin dirs (as under the Harness Bridge), and cmd.exe PATH must stay untouched.
Stdlib only, no API calls. Run: py -3 test_shell_env.py"""
import os
import subprocess
import sys

import claude_cli

MINIMAL_PATH = r"C:\Windows\System32;C:\Windows"


def git_root(bash):
    root = os.path.dirname(os.path.dirname(bash))
    if os.path.basename(root).lower() == "usr":
        root = os.path.dirname(root)
    return root


def check(cond, detail):
    if not cond:
        raise AssertionError(detail)


def run_checks():
    bash = claude_cli._git_bash()
    usr_bin = os.path.join(git_root(bash), "usr", "bin")
    check(os.path.isdir(usr_bin), "Git usr\\bin not found: " + usr_bin)
    minimal = MINIMAL_PATH.split(os.pathsep)

    # A: bash env puts Git's usr\bin first and keeps the original entries after it.
    parts = claude_cli._shell_env(bash=True)["PATH"].split(os.pathsep)
    check(parts[0].lower() == usr_bin.lower(), "A: first PATH entry is %r, want %r" % (parts[0], usr_bin))
    check(parts[-len(minimal):] == minimal, "A: original entries not kept in order: %r" % parts)

    # B: with the broken parent PATH, grep/sed/head resolve inside bash.
    r = subprocess.run([bash, "-c", "command -v grep && sed --version | head -1"],
                       capture_output=True, text=True, env=claude_cli._shell_env(bash=True))
    check(r.returncode == 0 and "grep" in r.stdout,
          "B: rc=%s stdout=%r stderr=%r" % (r.returncode, r.stdout, r.stderr[:300]))
    print("B output: " + " | ".join(r.stdout.strip().splitlines()))

    # C: non-bash env leaves PATH alone (no Git dirs shadowing FIND/SORT for cmd.exe).
    parts = claude_cli._shell_env()["PATH"].split(os.pathsep)
    check(parts[0] == minimal[0], "C: first PATH entry is %r, want %r" % (parts[0], minimal[0]))
    check(not any("program files\\git" in p.lower() for p in parts), "C: Git dir in PATH: %r" % parts)


def main():
    if claude_cli._git_bash() is None:
        print("SKIP: Git Bash not installed")
        return 0
    saved = os.environ.get("PATH")
    os.environ["PATH"] = MINIMAL_PATH
    try:
        run_checks()
    except AssertionError as e:
        print("FAIL: %s" % e)
        return 1
    finally:
        if saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
