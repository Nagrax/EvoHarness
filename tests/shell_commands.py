"""Equivalent real shell inputs for Windows and POSIX status regressions."""
import os
import shlex
import subprocess
import sys


def python_command(script: str) -> str:
    argv = [sys.executable, "-c", script]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
