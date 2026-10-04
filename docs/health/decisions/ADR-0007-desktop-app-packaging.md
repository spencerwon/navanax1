# ADR-0007 — The viewer as a desktop app: a plain .app bundle and a standard-library launcher

**Status:** Proposed · **Date:** 2026-10-03 · **Deciders:** orchestrator session; Operator review pending
**Supersedes:** — · **Superseded by:** —
**Module:** `desktop-app` (`config/health/modules.yaml`, `docs/health/07`), owner platform-engineer

## Context

The Operator asked: "make this a desktop app with a clickable icon that is a black
background with a stick figure on it". "This" is the Metabolic Map viewer vendored at
`reference/metabolic-map-v1/`, a static page (`index.html`, `app/*.js`, `engine/*.js`,
`kb/*.json`) that must stay runnable unchanged (HREQ-P-09). Its module scripts and
workers need an HTTP origin in some browsers, so "open the file" is not enough. The
Operator uses two Macs with python.org Python 3.14 (no Homebrew on one) and already
launches everything by double-clicking a `*.command` file at the repository root.
Constraints: nothing to install on the default path (HREQ-N-02), no listener beyond
localhost (HREQ-N-05), the disclaimer and the validation status on every surface
(HREQ-S-01), and a module that lands switched off and can be removed by its recipe
(HREQ-X-01..X-03, `04 §6.5`).

## Decision

1. **A standard-library launcher, `python -m health.app`** (`src/health/app.py`). It
   serves `reference/metabolic-map-v1/` byte for byte from a threaded `http.server` bound
   to 127.0.0.1 only (a non-loopback address is refused in code), on a port the operating
   system picks; every response says `Cache-Control: no-store` and carries an explicit
   media type for `.js`, `.mjs`, `.json`, `.svg` and `.html`; a request whose Host is not
   127.0.0.1 or localhost on that port is refused, so a page elsewhere cannot reach it by
   rebinding a name. It prints the address, then the disclaimer and the validation status
   as its last two lines, and again after it stops. It opens the page in the default
   browser, or — only when the optional `pywebview` package is importable — in a native
   window titled "Metabolic Map", and it stops cleanly on Ctrl-C, SIGTERM, SIGHUP (the
   Terminal window closing), the native window closing, or the Stop button of a small
   macOS dialog (`--dialogs`, which the .app passes because it has no terminal).
2. **The flag governs the surface.** The launcher reads `desktop-app` and `reference-v1`
   through `health.registry` (the pattern `health.cli status` uses for `engine-v1`) and,
   unless both are enabled, refuses to start, printing why and the enable recipe; an
   unreadable registry refuses too. The module lands with `enabled: false`; the second,
   one-line pull request (`enable/desktop-app`) that turns it on is the Operator's to
   approve. Until then the page is still opened directly, as before.
3. **A plain .app bundle with a script executable** (`desktop/MetabolicMap.app`):
   `Contents/Info.plist` (name "Metabolic Map", identifier `org.navanax.metabolic-map`,
   executable `MetabolicMap`, icon `MetabolicMap`, `APPL`, macOS 10.13 or later,
   high-resolution capable, the disclaimer in Get Info), `Contents/MacOS/MetabolicMap`, a
   bash script in the house style of the `.command` files, and
   `Contents/Resources/MetabolicMap.icns`. The script finds the checkout four levels up,
   adds the python.org and Homebrew locations to the bare PATH a double-clicked app gets,
   skips `/usr/bin/python3` when it would only offer to install the Command Line Tools,
   and runs `python3 -m health.app --dialogs` with `PYTHONPATH=src`, logging to
   `~/Library/Logs/MetabolicMap.log`. With no Python 3.10+ it shows a dialog saying so;
   moved out of the checkout it shows a dialog saying where it must stay.
   `MetabolicMap.command` at the root is the same launcher for the house convention, in a
   Terminal window. `METABOLIC_MAP_PYTHON` picks the interpreter for both (for example a
   virtual environment with pywebview).
4. **The icon** is `desktop/icon.svg`: a 1024 × 1024 canvas holding the macOS icon
   shape — an 824 px black (`#000000`) rounded square, corner radius 185 px (22.5 %),
   centred with Apple's 100 px margin — and a white stick figure: a round head about a
   sixth of the figure's height, a straight torso, two arms from the shoulders and two
   legs from the hip, as round-capped strokes, centred, nothing else. Strokes are 76 px so
   the figure, head apart from body, survives at 32 and 16 px. `tools/health_icon.py build`
   renders it at 16–1024 px with headless Chromium (Playwright; needed only to re-render)
   and writes the `.icns` with a standard-library writer: header, `TOC `, then PNG
   elements `icp4` 16, `icp5` 32, `icp6` 64, `ic07` 128, `ic08` 256, `ic09` 512, `ic10`
   1024, `ic11` 32 (16@2x), `ic12` 64 (32@2x), `ic13` 256 (128@2x), `ic14` 512 (256@2x).
   `read` parses an `.icns` back into its elements (the self-test uses it); `build --pngs
   DIR` packs PNGs rendered any other way; on a Mac, `iconset --iconutil` writes the
   `.iconset` Apple's `iconutil -c icns` reads and rebuilds the file with Apple's tool. The
   PNG and the `.icns` (81 KB) are committed, so nobody needs a renderer to use the app.

## Alternatives rejected

- **Electron.** About 200 MB per app, a Node toolchain and a build to produce it, and a
  bundled Chromium to keep patched — for a page any installed browser already runs.
- **Tauri.** Small output, but a Rust toolchain and a compile step on the Operator's Macs.
- **py2app / PyInstaller.** A build step and a frozen interpreter inside the bundle: a
  second Python to keep current, a bundle to rebuild after every change, and a binary in
  git that nobody can read.
- **A native window as the default.** pywebview is the lightest way to one (WKWebView on
  macOS, nothing bundled), but it is a dependency; it stays optional and is used only when
  it is already importable.

## Consequences

- Nothing to install and nothing to build: the bundle is three small text files, a script
  and an icon, all readable in a diff. The page is the vendored viewer, unchanged; the 3D
  body still loads three.js and the fonts from their CDNs, as the published artifact does.
- **The bundle is unsigned.** Gatekeeper checks the quarantine attribute, which macOS sets
  on files *downloaded* by a browser, mail or AirDrop — not on files a `git clone` or
  `git pull` writes — so a checkout double-clicks without a warning. If the attribute ever
  appears (the repository downloaded as a zip, for example), clear it once:
  `xattr -dr com.apple.quarantine desktop/MetabolicMap.app`, or right-click the app,
  choose Open, and confirm.
- The bundle runs the checkout it sits in, so it must stay at `desktop/MetabolicMap.app`;
  it goes into the Dock by dragging it there (or as an alias in Applications), and says so
  if it is moved.
- A script executable is not a Cocoa application, so the Dock's Quit may not reach it;
  the Stop dialog, closing the native window, or Ctrl-C in `MetabolicMap.command` is how
  it is stopped (Activity Monitor otherwise).
- HREQ-S-01: the terminal, the dialogs and Get Info show both sentences. The page itself
  still renders only the disclaimer's first sentence (BUG-20261003-104, open, fixed in the
  reference at its next version bump); this module does not edit the reference.
- The design-lead reviews the icon rendered (`desktop/icon-1024.png`) with the Operator,
  as for every user-facing change.

## How to undo

Before the enable pull request: revert this module's merge, or run the removal recipe of
`desktop-app` in `docs/health/07` (flag off, `buglog.py --mark-removed desktop-app`, delete
`src/health/app.py`, `desktop/`, `MetabolicMap.command`, `tools/health_icon.py` and
`tests/health_app_selftest.py`, delete its `HEALTH_SUITES` row and its two CI steps, the
04 §7 row and the README lines and paragraphs, remove the entry, regenerate 07, gates
green). After it: revert the enable pull request to turn the surface off and keep the code.
A different packaging is a new ADR that supersedes this one.
