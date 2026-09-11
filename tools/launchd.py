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
                         many seconds. 10 s for the recorder and the traits job;
                         30 s for the DASHBOARD, because the thing that makes it
                         exit instantly is a port conflict with a second
                         dashboard, and a hot loop of those is what corrupted the
                         analytical store on 2026-09-10 (BUG-20260910-067).
                         Without it, a job that fails instantly is respawned in a
                         hot loop.
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
import importlib
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
RECORDER_B = "com.navanax.recorder-b"
DASHBOARD = "com.navanax.dashboard"
TRAITS = "com.navanax.traits"
KEEPAWAKE = "com.navanax.keepawake"

# The three the autostart pair installs. keepawake is deliberately NOT here:
# it is opt-in, because it changes the machine's sleep behaviour (see
# docs/04_ENVIRONMENTS.md §8). RECORDER_B is not here either: it exists only
# when `stream.redundant.enabled` is true in config/base.yaml -- see
# `autostart_labels()`, which READS that flag rather than assuming it.
AUTOSTART_LABELS = (RECORDER, DASHBOARD, TRAITS)
ALL_LABELS = (*AUTOSTART_LABELS, RECORDER_B, KEEPAWAKE)

LOG_NAMES = {
    RECORDER: "recorder.log",
    RECORDER_B: "recorder-b.log",
    DASHBOARD: "dashboard.log",
    TRAITS: "traits.log",
    KEEPAWAKE: "keepawake.log",
}


def redundant_enabled(root: Path | str) -> bool:
    """Is `stream.redundant.enabled` true for THIS project directory?

    Read through `navanax.cli._config` and `navanax.redundancy.settings` -- the
    same two functions the recorder itself reads the flag with, environment
    overlay included. A second copy of the parsing here would be a second thing
    to keep in step, and the failure mode of drift is a launchd job for a
    connection that refuses to start (or, worse, no job for a connection the
    Operator believes is running).

    Returns False if the config cannot be read at all. Rendering no B job is the
    safe direction: a missing job is visible in `launchctl list`, whereas a job
    installed for a disabled connection restarts every 10 s forever and records
    nothing.
    """
    try:
        proj = Path(root).expanduser().resolve()
        sys.path.insert(0, str(proj / "src"))
        # Deliberately late: tools/ is not a package, and importing the project
        # at module scope would make `--help` depend on it.
        cli = importlib.import_module("navanax.cli")
        red = importlib.import_module("navanax.redundancy")
        cfg, _ = cli._config(proj)
        return red.settings(cfg).enabled
    except Exception as exc:                       # noqa: BLE001 - any failure -> no B job
        print(f"launchd.py: could not read stream.redundant.enabled from {root} "
              f"({type(exc).__name__}: {exc}); assuming OFF and rendering no "
              f"{RECORDER_B} job", file=sys.stderr)
        return False


#: Labels that are OPT-IN and must never be treated as stale. `keepawake` changes
#: the machine's sleep behaviour and is installed by its own launcher; an
#: autostart install that quietly removed it would undo a deliberate choice.
OPT_IN_LABELS = ("com.navanax.keepawake",)

#: The prefix every job of ours carries. Anything else in `launchctl list` belongs
#: to somebody else and is never touched.
LABEL_PREFIX = "com.navanax."


def stale_labels(root: Path | str, loaded: object) -> tuple[str, ...]:
    """Loaded jobs of ours that this project no longer wants. Sorted, deduplicated.

    The case this exists for: the Operator turns `stream.redundant.enabled` back
    OFF. `com.navanax.recorder-b` is still installed and still loaded, so launchd
    keeps starting it, `ingest --redundant` refuses with EXIT_CONFIG because the
    flag is off, and `KeepAlive` restarts it ten seconds later -- forever. Nothing
    is damaged (the refusal happens before anything is recorded) and nothing says
    so either, except a log file nobody is reading.

    ONLY labels this generator KNOWS are returned. A `com.navanax.*` job this
    version has never heard of is reported by `unknown_labels` and left alone: it
    is far more likely to be a job from a NEWER version of this project than
    rubbish, and booting out something we cannot name is how an upgrade breaks a
    downgrade. Opt-in labels are never stale.
    """
    wanted = set(autostart_labels(root)) | set(OPT_IN_LABELS)
    known = set(ALL_LABELS)
    return tuple(sorted({label for label in (loaded or ())
                         if label in known and label not in wanted}))


def unknown_labels(loaded: object) -> tuple[str, ...]:
    """Loaded `com.navanax.*` jobs this generator cannot name. Reported, never removed."""
    known = set(ALL_LABELS)
    return tuple(sorted({label for label in (loaded or ())
                         if label.startswith(LABEL_PREFIX) and label not in known}))


def autostart_labels(root: Path | str) -> tuple[str, ...]:
    """The labels the autostart pair should install for this project.

    The redundant recorder is in this list ONLY when the flag is on. That is the
    whole of PR-10's launchd half: with the flag off there is one recorder job,
    exactly as before, and the second one does not exist on the machine at all.
    """
    if redundant_enabled(root):
        return (RECORDER, RECORDER_B, DASHBOARD, TRAITS)
    return AUTOSTART_LABELS

# Daily traits refresh. Local time -- StartCalendarInterval is not UTC.
TRAITS_HOUR = 3
TRAITS_MINUTE = 30

THROTTLE_SECONDS = 10

# The dashboard's own throttle, and why it is not 10 (BUG-20260910-067).
#
# The failure mode a throttle has to survive here is a PORT CONFLICT: a second
# dashboard is already listening on 8765, so every start of this one fails to
# bind and exits 2. At ThrottleInterval 10 that is 360 starts an hour -- and on
# 2026-09-10 it was 177 of them, each of which opened the 2.8 GB analytical
# store and folded new frames into it before dying, because the store was opened
# BEFORE the bind. Two writers alternating on one SQLite store, with processes
# killed mid-write, left it "database disk image is malformed".
#
# `dashboard.serve()` now binds first, so a doomed retry writes nothing at all;
# that is the fix. 30 s is the second layer: it makes the loop slow enough to
# read in dashboard.log rather than a wall of banners, and it bounds the cost of
# any future start-up work that is not free. Not longer, because a genuinely
# crashed dashboard should come back promptly -- the page is how the Operator
# sees the record at all.
DASHBOARD_THROTTLE_SECONDS = 30


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
    elif label == RECORDER_B:
        # PR-10, connection B. Identical supervision to A -- it is a recorder and
        # the same argument applies: an hour it did not record cannot be bought
        # back. What differs is `--redundant`, which makes the CLI refuse unless
        # the flag is on AND B has its own key, and points it at B's landing root
        # and B's own `.ingest.lock`. Two writers on one root lose manifest
        # records silently; two roots with one writer each do not.
        plist["ProgramArguments"] = [python, "-m", "navanax.cli", "ingest",
                                     "--redundant", "--supervised"]
        plist["KeepAlive"] = True
        plist["ThrottleInterval"] = THROTTLE_SECONDS
        plist["ExitTimeOut"] = 30
    elif label == DASHBOARD:
        plist["ProgramArguments"] = [
            python, "-m", "navanax.cli", "dashboard", "--no-browser", "--port", str(port),
        ]
        plist["KeepAlive"] = True
        plist["ThrottleInterval"] = DASHBOARD_THROTTLE_SECONDS
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

    lb = sub.add_parser("labels", help="print the labels the autostart pair installs")
    lb.add_argument("--root", default=".", help="project directory (the redundant "
                                                "recorder is listed only when its flag is on)")
    sub.add_parser("all-labels", help="print every label the generator knows, including "
                                      "the opt-in keep-awake and the redundant recorder")

    st = sub.add_parser("stale", help="of the loaded labels given as arguments, print the ones "
                                      "this project no longer wants (one per line)")
    st.add_argument("--root", default=".", help="project directory")
    st.add_argument("--unknown", action="store_true",
                    help="instead print the loaded com.navanax.* labels this version cannot "
                         "name -- report them, never boot them out")
    st.add_argument("loaded", nargs="*", help="labels currently loaded in launchd")

    r = sub.add_parser("render", help="write one plist")
    r.add_argument("label")
    r.add_argument("--root", default=".", help="project directory")
    r.add_argument("--python", default="python3", help="interpreter to run the CLI with")
    r.add_argument("--port", type=int, default=8765)
    r.add_argument("--path", default=DEFAULT_PATH)
    r.add_argument("--out", default=None, help="write here instead of stdout")

    args = ap.parse_args(argv)

    if args.cmd == "labels":
        print("\n".join(autostart_labels(args.root)))
        return 0
    if args.cmd == "all-labels":
        print("\n".join(ALL_LABELS))
        return 0
    if args.cmd == "stale":
        out = (unknown_labels(args.loaded) if args.unknown
               else stale_labels(args.root, args.loaded))
        if out:
            print("\n".join(out))
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
