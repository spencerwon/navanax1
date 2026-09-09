#!/usr/bin/env python3
"""Generate the macOS launchd LaunchAgent property lists for Navanax.

    python3 tools/launchd.py labels
    python3 tools/launchd.py render com.navanax.recorder --root . --python python3
    python3 tools/launchd.py render com.navanax.recorder --root . --out FILE

Why a Python helper rather than a heredoc in the .command file: a property list
is XML, and a launchd job whose plist is malformed does not fail loudly -- it
simply never runs, and `launchctl print` reports "could not find service".
A recorder that silently never started looks exactly like a quiet market, which
is the one confusion this project cannot afford (docs/07 §4). `plistlib` cannot
emit invalid XML, and tests/selftest.py parses every plist this module renders
before any of them is installed.

WHAT EACH KEY MEANS (for a reader who has not met launchd before)

  Label                  the job's name. `launchctl` addresses it by this.
  ProgramArguments       argv. No shell is involved: no quoting, no PATH search
                         for the first element unless it is a bare name, no
                         globbing. That is a feature -- it removes a class of
                         quoting bug -- but it means the Python interpreter is
                         found via EnvironmentVariables PATH, not via a login
                         shell, because launchd agents do NOT read .zprofile.
  WorkingDirectory       the process's cwd. The project directory.
  RunAtLoad              start it once, immediately, when the job is loaded
                         (which happens at install and again at every login).
  KeepAlive              restart it whenever it exits, for ANY reason: crash,
                         clean exit, `kill`. This is what makes the recorder
                         unattended.
  ThrottleInterval       launchd will not restart a job more often than this
                         many seconds. 10 s. Without it, a job that fails
                         instantly is respawned in a hot loop.
  ExitTimeOut            how long launchd waits after SIGTERM before SIGKILL.
                         The recorder flushes its final frame on SIGTERM
                         (cli._install_shutdown_handlers), so this must be
                         generous enough for that flush -- the default is 20 s
                         and we ask for 30.
  StartCalendarInterval  a cron-like wall-clock schedule, in LOCAL time. If the
                         Mac is asleep at the appointed minute, launchd runs the
                         job when it next wakes; it does not wake the Mac.
  StandardOutPath /      where stdout / stderr go. launchd does NOT create these
  StandardErrorPath      directories -- the install script does.
"""
from __future__ import annotations

import argparse
import plistlib
import sys
from pathlib import Path

# launchd agents get a minimal environment. This is the PATH the jobs run with.
# /Library/Frameworks/... is where a python.org installer puts python3 on macOS;
# /opt/homebrew/bin is Apple-silicon Homebrew; /usr/local/bin is Intel Homebrew.
DEFAULT_PATH = (
    "/Library/Frameworks/Python.framework/Versions/3.14/bin:"
    "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
)

RECORDER = "com.navanax.recorder"
DASHBOARD = "com.navanax.dashboard"
TRAITS = "com.navanax.traits"
KEEPAWAKE = "com.navanax.keepawake"

# The three the autostart pair installs. keepawake is deliberately NOT here:
# it is opt-in, because it changes the machine's sleep behaviour (see
# docs/04_ENVIRONMENTS.md §8).
AUTOSTART_LABELS = (RECORDER, DASHBOARD, TRAITS)
ALL_LABELS = (*AUTOSTART_LABELS, KEEPAWAKE)

LOG_NAMES = {
    RECORDER: "recorder.log",
    DASHBOARD: "dashboard.log",
    TRAITS: "traits.log",
    KEEPAWAKE: "keepawake.log",
}

# Daily traits refresh. Local time -- StartCalendarInterval is not UTC.
TRAITS_HOUR = 3
TRAITS_MINUTE = 30

THROTTLE_SECONDS = 10


def log_path(root: Path, label: str) -> Path:
    return root / "data" / "logs" / LOG_NAMES[label]


def job(label: str, root: Path | str, python: str = "python3",
        port: int = 8765, path: str = DEFAULT_PATH) -> dict:
    """Build the plist dictionary for one label.

    `root` is made absolute: launchd resolves nothing relative, and a
    WorkingDirectory that does not exist makes the job fail to spawn with
    errno 2, which reads like a missing interpreter and is not.
    """
    if label not in ALL_LABELS:
        raise ValueError(f"unknown label {label!r}; known: {', '.join(ALL_LABELS)}")
    root = Path(root).expanduser().resolve()
    logs = log_path(root, label)

    env = {
        "PATH": path,
        # Without this, stdout to a FILE is block-buffered: the log stays empty
        # for minutes and autostart-status.command shows nothing, which reads as
        # "it is not running".
        "PYTHONUNBUFFERED": "1",
        # Belt and braces. `python3 -m navanax.cli` normally resolves through the
        # editable install start.command performs. If that install is ever
        # broken by a Python upgrade, this still finds the package rather than
        # leaving a job that restarts every 10 s forever with ModuleNotFoundError.
        "PYTHONPATH": str(root / "src"),
    }

    plist: dict = {
        "Label": label,
        "WorkingDirectory": str(root),
        "EnvironmentVariables": env,
        "StandardOutPath": str(logs),
        "StandardErrorPath": str(logs),
        "RunAtLoad": True,
    }

    if label == RECORDER:
        plist["ProgramArguments"] = [python, "-m", "navanax.cli", "ingest", "--supervised"]
        plist["KeepAlive"] = True
        plist["ThrottleInterval"] = THROTTLE_SECONDS
        plist["ExitTimeOut"] = 30
    elif label == DASHBOARD:
        plist["ProgramArguments"] = [
            python, "-m", "navanax.cli", "dashboard", "--no-browser", "--port", str(port),
        ]
        plist["KeepAlive"] = True
        plist["ThrottleInterval"] = THROTTLE_SECONDS
        plist["ExitTimeOut"] = 30
    elif label == TRAITS:
        # No KeepAlive: this job is SUPPOSED to finish. KeepAlive on a job that
        # exits normally would restart it forever, and `traits` spends metered
        # REST budget -- it would drain the 120/hour bucket permanently.
        plist["ProgramArguments"] = [python, "-m", "navanax.cli", "traits"]
        plist["StartCalendarInterval"] = {"Hour": TRAITS_HOUR, "Minute": TRAITS_MINUTE}
        plist["ThrottleInterval"] = THROTTLE_SECONDS
        # NOT RunAtLoad (tech-lead, PR-1 review): the first traits run spends up
        # to ~97 metered reads, and the Operator must be told BEFORE budget is
        # spent, not after. The installer says so and names traits.command for
        # a run-it-now; the schedule takes care of every later day.
        plist["RunAtLoad"] = False
    elif label == KEEPAWAKE:
        # -i: prevent IDLE sleep. -s: only while on AC power (so a closed lid on
        # battery still sleeps and the battery is not flattened overnight).
        # No project code runs here; caffeinate is an Apple binary.
        plist["ProgramArguments"] = ["/usr/bin/caffeinate", "-i", "-s"]
        plist["KeepAlive"] = True
        plist["ThrottleInterval"] = THROTTLE_SECONDS
        plist["EnvironmentVariables"] = {"PATH": path}  # caffeinate needs no Python

    return plist


def render(label: str, root: Path | str, python: str = "python3",
           port: int = 8765, path: str = DEFAULT_PATH) -> bytes:
    """The plist as XML bytes. plistlib cannot produce malformed XML."""
    return plistlib.dumps(job(label, root, python, port, path), sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="launchd.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("labels", help="print the labels the autostart pair installs")
    sub.add_parser("all-labels", help="print every label including the opt-in keep-awake")

    r = sub.add_parser("render", help="write one plist")
    r.add_argument("label")
    r.add_argument("--root", default=".", help="project directory")
    r.add_argument("--python", default="python3", help="interpreter to run the CLI with")
    r.add_argument("--port", type=int, default=8765)
    r.add_argument("--path", default=DEFAULT_PATH)
    r.add_argument("--out", default=None, help="write here instead of stdout")

    args = ap.parse_args(argv)

    if args.cmd == "labels":
        print("\n".join(AUTOSTART_LABELS))
        return 0
    if args.cmd == "all-labels":
        print("\n".join(ALL_LABELS))
        return 0

    try:
        data = render(args.label, args.root, args.python, args.port, args.path)
    except ValueError as exc:
        print(f"launchd.py: {exc}", file=sys.stderr)
        return 2
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        # Write via a temp file and replace, so an interrupted write can never
        # leave a half-written plist that launchd would refuse to parse.
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(out)
        print(str(out))
    else:
        sys.stdout.buffer.write(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
