# Measurements

One file per question that could only be settled by asking the real thing.

Everything in this folder is **append-only**. A probe run adds a dated entry at
the bottom; it never edits or deletes an entry above it. A later measurement
that contradicts an earlier one supersedes it in place — the earlier reading
stays, because the fact that we once believed it is part of the record
(`docs/01_METHODOLOGY.md`).

This exists because of a failure this project has already had. `600 REST
reads/hour` was an assumption that no document sourced and every document
repeated, until one live response falsified it (BUG-20260909-003, and the real
figure is 120). The cure for that shape of failure is not more careful writing;
it is a cheap measurement, recorded where the next reader will find it, with the
date and the raw evidence attached.

| File | Question | Written by | Cost per run |
|---|---|---|---|
| `2026-09-11_events_page_size.md` | Does the events endpoint really return 200 items for `limit=200`? (REQ-D-13, E-U1) | `tools/probe_events_page.py` via `probe-events-page.command` | 1 REST read |
| `2026-09-11_two_sockets.md` | May one API key hold two simultaneous stream connections, and how much does a single socket drop? (E-U4, gates PR-10) | `tools/probe_two_sockets.py` via `probe-two-sockets.command` | 0 REST reads; two extra connections |

A file here that is still absent means the question is still open, and any
document that answers it is stating an assumption. Say so rather than quoting
the number.
