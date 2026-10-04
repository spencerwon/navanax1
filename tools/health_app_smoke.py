"""Headless-Chromium smoke check of the vendored V1 app and its safety texts.

`python3 tools/health_app_smoke.py [--no-skips] [--page PATH] [--three FILE] [--screenshots DIR]`

HREQ-P-09: the reference app stays runnable from `reference/metabolic-map-v1/index.html`.
This serves `reference/` from a stdlib HTTP server on 127.0.0.1 (an ephemeral port, so the
page sits in a subdirectory, as it does when the repository is served), loads the page in
headless Chromium driven by the Playwright that is installed for Node, runs every scenario
from the dropdown and both the Chronic and Custom tabs, and checks:

  - no console error, page error, failed request or HTTP error from the page's own origin
    (a third-party resource -- Google Fonts, three.js from cdnjs -- that cannot be reached
    from this machine is printed, loudly, and does not fail the check; `--three FILE` serves
    a local copy of three.module.min.js so the 3D viewer runs offline);
  - every scenario renders: every chart holds a finite median and band, and the HUD names
    the scenario;
  - the three safety texts, and the labels, byte-equal to config/health/base.yaml:
      1. "Educational model — not medical advice. Not clinically validated." (model.disclaimer
         and model.validation_status) on every surface that shows a number: the HUD, the run
         panel, every chart card, the dose panel, the evidence drawer, the knowledge-base card,
         the footer and the narrow-screen strip (HREQ-S-01, BUG-20261003-104);
      2. display.threshold_label beside 135 and 145 mmol/L on the plasma sodium chart, in the
         neutral tone (HREQ-S-03, BUG-20261003-101);
      3. under the chronic-salt charts, "the He 2013 band is a calibration target for
         aldo_vol_exp, map_vol_exp, pn_gain" and the known-divergence row's text, read from the
         records in engine/scenarios.js (HREQ-S-07, BUG-20261003-103), and the salt sweep's
         unverified row on the dose panel (BUG-20261003-197);
    plus display.index_label on every surface that shows the kidney strain index (HREQ-S-04,
    BUG-20261003-100 in the vendored copy), display.trajectory_label on the Chronic tab and
    display.custom_run_label on the Custom tab (BUG-20261003-196), and the curated knowledge
    base loaded from the subdirectory (BUG-20261003-195).

Three static checks run with or without a browser: app/config.js carries the labels
byte-equal to base.yaml, the fallback STRAIN_P list in app/ui.js names every strain_*
parameter row (BUG-20261003-194), and app/index.html is still the top-level page with the
publish skeleton removed (the two copies carry the same fixes).

Exit status: 0 every check passed; 1 a check failed; 3 the browser checks were SKIPPED
(no Chromium, Node or Playwright here; printed loudly, never a silent pass). With
`--no-skips` a skip is a failure.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "reference" / "metabolic-map-v1"
BASE_YAML = ROOT / "config" / "health" / "base.yaml"
EXIT_SKIPPED = 3
CHROMIUM_CANDIDATES = ("/opt/pw-browsers/chromium",)       # env HEALTH_SMOKE_CHROMIUM first
NODE_CANDIDATES = ("/opt/node22/bin/node",)                 # then `node` on PATH
NODE_MODULES_CANDIDATES = ("/opt/node-tools/node_modules",)  # env HEALTH_SMOKE_NODE_MODULES first
CONFIG_KEYS = ("model.disclaimer", "model.validation_status", "display.index_label",
               "display.threshold_label", "display.trajectory_label", "display.custom_run_label",
               "display.extrapolation_label")
TIMEOUT_S = 900

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n      {detail}" if detail and not ok else ""))
    return bool(ok)


# ---------------------------------------------------------------------------
# Expected strings: config/health/base.yaml is the oracle (stdlib reader for its flat
# two-level layout; every value checked here is a double-quoted scalar).
def config_strings(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    section = None
    for line in text.splitlines():
        top = re.match(r"^([a-z_]+):\s*(#.*)?$", line)
        if top:
            section = top.group(1)
            continue
        kv = re.match(r'^  ([a-z_]+):\s*"([^"\\]*)"\s*(#.*)?$', line)
        if kv and section:
            out[f"{section}.{kv.group(1)}"] = kv.group(2)
    return out


def derive_app_index(top: str) -> str:
    """app/index.html is the top-level page without the publish skeleton (its first line and
    the closing `</body></html>`), loading ./ui.js instead of ./app/ui.js."""
    body = "\n".join(top.split("\n")[1:])
    tail = "\n\n</body></html>"
    if body.endswith(tail):
        body = body[: -len(tail) + 1]
    return body.replace('<script type="module" src="./app/ui.js"></script>',
                        '<script type="module" src="./ui.js"></script>')


def static_checks(cfg: dict[str, str]) -> None:
    config_js = (REF / "app" / "config.js").read_text(encoding="utf-8")
    block = re.search(r"export const LABELS = Object\.freeze\(\{(.*?)\}\);", config_js, re.S)
    labels = dict(re.findall(r"(\w+): '([^']*)'", block.group(1))) if block else {}
    wrong = {k: v for k, v in labels.items() if v != cfg.get(f"display.{k}")}
    check("static: app/config.js LABELS carries index_label, threshold_label and custom_run_label, "
          "each byte-equal to display.* in config/health/base.yaml",
          {"index_label", "threshold_label", "custom_run_label"} <= labels.keys() and not wrong,
          f"labels {labels}; differing from base.yaml: {wrong}")

    ui_js = (REF / "app" / "ui.js").read_text(encoding="utf-8")
    strain_p = re.search(r"const STRAIN_P = \[(.*?)\];", ui_js, re.S)
    listed = set(re.findall(r"'([^']+)'", strain_p.group(1))) if strain_p else set()
    rows = json.loads((REF / "engine" / "params.json").read_text(encoding="utf-8"))
    strain_rows = {k for k in rows if k.startswith("strain_")}
    check("static: the fallback evidence list STRAIN_P (app/ui.js) names every strain_* parameter "
          f"row of engine/params.json ({len(strain_rows)}) (BUG-20261003-194)",
          strain_p is not None and strain_rows <= listed,
          f"missing from STRAIN_P: {sorted(strain_rows - listed)}")

    top = (REF / "index.html").read_text(encoding="utf-8")
    app = (REF / "app" / "index.html").read_text(encoding="utf-8")
    check("static: app/index.html is the top-level index.html without the publish skeleton "
          "(the two copies carry the same page)", derive_app_index(top) == app,
          "regenerate app/index.html from index.html")


# ---------------------------------------------------------------------------
# The browser part: prerequisites, a local server, the Playwright driver.
def _executable(path: str | None) -> str | None:
    return path if path and os.path.isfile(path) and os.access(path, os.X_OK) else None


def prerequisites() -> tuple[dict[str, str], str | None]:
    """(paths, None) when Chromium, Node and Playwright are all here; else ({}, reason)."""
    chromium = next(filter(None, (_executable(p) for p in (os.environ.get("HEALTH_SMOKE_CHROMIUM"),
                                                            *CHROMIUM_CANDIDATES))), None)
    node = next(filter(None, (_executable(p) for p in (*NODE_CANDIDATES, shutil.which("node")))), None)
    modules = None
    cands = [os.environ.get("HEALTH_SMOKE_NODE_MODULES"), *NODE_MODULES_CANDIDATES]
    if node:
        cands.append(str(Path(node).resolve().parents[1] / "lib" / "node_modules"))
    for cand in filter(None, cands):
        if (Path(cand) / "playwright" / "package.json").is_file():
            modules = cand
            break
    missing = [name for name, v in (("Chromium", chromium), ("Node", node),
                                    ("Playwright for Node", modules)) if not v]
    if missing:
        return {}, f"no {', no '.join(missing)} on this machine"
    return {"chromium": chromium, "node": node, "nodeModules": modules}, None


class _Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".js": "text/javascript", ".mjs": "text/javascript", ".json": "application/json"}

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        # The browser's own favicon probe: the published page's icon link sits after the publish
        # skeleton's <body>, so Chromium also asks for /favicon.ico. No page code requests it.
        if self.path.split("?")[0] == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        super().do_GET()


DRIVER = r"""
import { createRequire } from 'node:module';
const cfg = JSON.parse(process.env.SMOKE_CONFIG);
const { chromium } = createRequire(cfg.nodeModules + '/')('playwright');
const out = { problems: [], surfaces: {}, scenarios: [], shots: [] };
const browser = await chromium.launch({ executablePath: cfg.chromium, headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, colorScheme: 'light' });
  const origin = new URL(cfg.url).origin;
  const local = (u) => { try { return !u || new URL(u).origin === origin; } catch { return true; } };
  page.on('console', (m) => { if (m.type() === 'error') out.problems.push({ kind: 'console.error', text: m.text(), url: m.location().url || '', local: local(m.location().url) }); });
  page.on('pageerror', (e) => out.problems.push({ kind: 'pageerror', text: String(e.message), url: '', local: true }));
  page.on('requestfailed', (r) => out.problems.push({ kind: 'requestfailed', text: r.failure()?.errorText || '', url: r.url(), local: local(r.url()) }));
  page.on('response', (r) => { if (r.status() >= 400) out.problems.push({ kind: `http ${r.status()}`, text: '', url: r.url(), local: local(r.url()) }); });
  if (cfg.three) await page.route('**/three.module.min.js', (r) => r.fulfill({ path: cfg.three, contentType: 'text/javascript' }));

  const text = (sel) => page.evaluate((s) => document.querySelector(s)?.innerText ?? null, sel);
  const raw = (sel) => page.evaluate((s) => document.querySelector(s)?.textContent ?? null, sel);
  const shot = async (name, sel) => {
    if (!cfg.shots) return;
    const path = `${cfg.shots}/${name}.png`;
    if (sel) await page.locator(sel).first().screenshot({ path }); else await page.screenshot({ path });
    out.shots.push(path);
  };
  const ready = () => page.waitForFunction(() => window.__mm?.S.res && window.__mm.S.res !== window.__smokePrev &&
    /^Monte Carlo n =/.test(document.getElementById('mcNote').textContent), null, { timeout: cfg.timeoutMs });
  const run = async (action) => { await page.evaluate(() => { window.__smokePrev = window.__mm?.S.res ?? null; }); await action(); await ready(); };
  const cards = () => page.evaluate(() => Object.fromEntries(Object.entries(window.__mm.S.charts)
    .map(([id, c]) => [id, c.chart.host.closest('.chart-card').innerText])));
  const chartsOk = () => page.evaluate(() => Object.entries(window.__mm.S.charts).filter(([, c]) => {
    const d = c.chart.data;
    return !(d && d.t.length > 1 && d.series.every((s) => ['q05', 'q50', 'q95'].every((q) => Array.from(s[q]).every(Number.isFinite))));
  }).map(([id]) => id));
  const PANEL = 'section.panel[aria-label="Choose an intervention"]';

  await page.goto(cfg.url, { waitUntil: 'load' });
  await ready();
  await page.waitForFunction(() => /doses ×|failed/.test(document.getElementById('doseNote').textContent), null, { timeout: cfg.timeoutMs });
  await page.evaluate(() => { for (const [id, c] of Object.entries(window.__mm.S.charts)) c.chart.host.closest('.chart-card').dataset.smoke = id; });
  out.records = await page.evaluate(async () => {
    const src = document.querySelector('script[type="module"][src$="ui.js"]').src;
    const eng = await import(new URL('../engine/index.js', src).href);
    const pick = (id) => eng.SCENARIOS[id].validation.expects.map((x) => ({ id: x.id, kind: x.kind, role: x.role ?? null,
      calibrates: x.calibrates ?? null, metric: x.metric, target: x.target }));
    return { titles: Object.fromEntries(Object.entries(eng.SCENARIOS).map(([k, v]) => [k, v.title])),
      chronic: pick('chronic_high_salt_30d'), sweep: pick('salt_load_sweep') };
  });
  out.surfaces = {
    hud: await text('#view .hud.tr'), gauge: await text('#view .gauge'), runPanel: await text(PANEL),
    stats: await text('#stats'), dosePanel: await text('#dosePanel'), doseHeading: await text('#dosePanel h2'),
    footer: await text('#foot'), topStrip: await raw('.top-disclaimer'), charts: await cards(),
  };
  out.refLines = await page.evaluate(() => (window.__mm.S.charts.na.chart.opts.refLines || []).map((r) => ({ ...r })));
  out.kb = await page.evaluate(() => ({ entities: window.__mm.S.entitySource, evidence: window.__mm.S.evidence.size }));
  out.viewer = await page.evaluate(() => (window.__mm.S.viewer ? 'running'
    : document.querySelector('.webgl-msg') ? 'fallback message (WebGL or three.js unavailable)' : 'not started'));
  await shot('page-default'); await shot('hud', '#view'); await shot('run-panel', PANEL);
  await shot('chart-plasma-sodium', '[data-smoke="na"]'); await shot('chart-strain-index', '[data-smoke="strain"]');
  await shot('stats', '#stats'); await shot('dose-panel', '#dosePanel'); await shot('footer', '#foot');

  await page.evaluate(() => window.__mm.selectEntity('organ:kidney'));
  out.surfaces.kbKidney = await text('#kb'); await shot('kb-kidney', '#kb');
  await page.click('[data-smoke="na"] .ev-chip');
  await page.waitForSelector('#drawer:not([hidden])');
  out.surfaces.drawer = await text('#drawer'); await shot('evidence-drawer', '#drawer');
  await page.click('#drawerClose');

  const ids = await page.evaluate(() => [...document.getElementById('scenarioSel').options].map((o) => o.value));
  for (const id of ids) {
    const t0 = Date.now();
    await run(() => page.evaluate((v) => { const s = document.getElementById('scenarioSel'); s.value = v; s.dispatchEvent(new Event('change')); }, id));
    const c = await cards();
    out.scenarios.push({ id, seconds: (Date.now() - t0) / 1000, hud: await raw('#hudScen'), bad: await chartsOk(), map: c.map, vol: c.vol });
  }
  await run(() => page.click('#tab-chronic'));
  const cc = await cards();
  out.chronic = { chip: await raw('#mode-chronic .chip'), map: cc.map, vol: cc.vol, bad: await chartsOk(), hud: await raw('#hudScen') };
  await shot('chronic-panel', PANEL); await shot('chronic-chart-map', '[data-smoke="map"]'); await shot('chronic-chart-volume', '[data-smoke="vol"]');
  await run(() => page.click('#tab-custom'));
  out.custom = { chip: await raw('#mode-custom .chip'), bad: await chartsOk(), hud: await raw('#hudScen') };
  await shot('custom-panel', PANEL);
  out.bodyText = await page.evaluate(() => document.body.innerText);
  if (cfg.shots) { await page.setViewportSize({ width: 390, height: 844 }); await page.waitForTimeout(300); await shot('page-narrow'); }
} catch (err) {
  out.problems.push({ kind: 'driver', text: String(err && err.stack || err), url: '', local: true });
} finally {
  await browser.close();
}
process.stdout.write('\n' + JSON.stringify(out) + '\n');
"""


def drive(paths: dict[str, str], page: str, three: str | None, shots: str | None) -> dict:
    handler = functools.partial(_Handler, directory=str(REF.parent))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/{REF.name}/{page}"
        cfg = {**paths, "url": url, "three": three, "shots": shots, "timeoutMs": 180_000}
        with tempfile.TemporaryDirectory() as tmp:
            driver = Path(tmp) / "driver.mjs"
            driver.write_text(DRIVER, encoding="utf-8")
            p = subprocess.run([paths["node"], str(driver)], capture_output=True, text=True,
                               timeout=TIMEOUT_S, env={**os.environ, "SMOKE_CONFIG": json.dumps(cfg)})
        last = (p.stdout.strip().splitlines() or [""])[-1]
        try:
            return {**json.loads(last), "url": url}
        except json.JSONDecodeError:
            return {"url": url, "problems": [{"kind": "driver", "local": True, "url": "",
                                              "text": f"exit {p.returncode}: {(p.stderr or p.stdout)[-2000:]}"}]}
    finally:
        server.shutdown()
        server.server_close()


def browser_checks(cfg: dict[str, str], obs: dict) -> None:
    full = f"{cfg['model.disclaimer']} {cfg['model.validation_status']}"
    idx, thr = cfg["display.index_label"], cfg["display.threshold_label"]
    s = obs.get("surfaces") or {}
    charts = s.get("charts") or {}
    own = [p for p in obs.get("problems", []) if p.get("local")]
    external = [p for p in obs.get("problems", []) if not p.get("local")]
    check(f"page: {obs.get('url')} loads and runs with no console error, page error, failed "
          "request or HTTP error from its own origin (HREQ-P-09)", not own,
          "; ".join(f"{p['kind']}: {p['text']} {p['url']}".strip() for p in own)[:3000])

    # 1. the disclaimer, both sentences, on every surface that shows a number
    surfaces = {"HUD": s.get("hud"), "run panel": s.get("runPanel"), "dose panel": s.get("dosePanel"),
                "evidence drawer": s.get("drawer"), "knowledge-base card (kidney)": s.get("kbKidney"),
                "footer": s.get("footer"), **{f"chart card {k}": v for k, v in charts.items()}}
    missing = [k for k, v in surfaces.items() if not v or full not in v]
    check(f"disclaimer: \"{full}\" on the HUD, the run panel, all {len(charts)} chart cards, the dose "
          "panel, the evidence drawer, the knowledge-base card and the footer (HREQ-S-01, "
          "BUG-20261003-104)", len(charts) == 8 and not missing, f"missing on: {missing}")
    check("disclaimer: the narrow-screen strip reads both sentences", s.get("topStrip") == full,
          f"strip {s.get('topStrip')!r}")

    # 2. thresholds: neutral, labelled, beside their value
    ref = obs.get("refLines") or []
    check(f"thresholds: the plasma sodium chart draws 135 and 145 with \"{thr}\" beside each value, "
          "with no alarm tone (HREQ-S-03, BUG-20261003-101)",
          sorted(r.get("value") for r in ref) == [135, 145]
          and all(r.get("label") == f"{r.get('value')} — {thr}" and "tone" not in r for r in ref)
          and thr in (charts.get("na") or ""),
          f"refLines {ref}; card {charts.get('na')!r}")
    check("thresholds: no surface calls 135 mmol/L a \"hyponatremia threshold\"",
          "hyponatremia threshold" not in (obs.get("bodyText") or "") + json.dumps(ref),
          "found on the page")

    # index label on every surface that shows the strain index
    idx_surfaces = {"HUD gauge": s.get("gauge"), "dose panel heading": s.get("doseHeading"),
                    "strain chart": charts.get("strain"), "stat tiles": s.get("stats"),
                    "knowledge-base card (kidney)": s.get("kbKidney")}
    missing = [k for k, v in idx_surfaces.items() if not v or idx not in v]
    check(f"index: \"{idx}\" on the HUD gauge, the dose panel heading, the strain chart, the stat "
          "tile and the kidney card (HREQ-S-04; BUG-20261003-100 in the vendored copy)", not missing,
          f"missing on: {missing}")

    # 3. calibration and divergence status under the chronic charts, from the records
    rec = obs.get("records") or {}
    chronic = obs.get("chronic") or {}
    cal = [r for r in rec.get("chronic", []) if r.get("role") == "calibration"]
    div = [r for r in rec.get("chronic", []) if r.get("kind") == "known-divergence"]
    want = "the He 2013 band is a calibration target for aldo_vol_exp, map_vol_exp, pn_gain"
    check(f"chronic: the MAP chart reads \"{want}\" (HREQ-S-07, BUG-20261003-103)",
          want in (chronic.get("map") or ""), f"MAP card {chronic.get('map')!r}")
    check(f"chronic: every calibration record ({len(cal)}) names its calibrated parameters under the "
          "MAP chart, and the known-divergence record's text sits under the volume chart",
          bool(cal) and bool(div)
          and all(f"calibration target for {', '.join(sorted(r['calibrates']))}" in (chronic.get("map") or "") for r in cal)
          and all(f"Known divergence. {r['metric']}: {r['target']}" in (chronic.get("vol") or "") for r in div),
          f"records {cal + div}; MAP {chronic.get('map')!r}; volume {chronic.get('vol')!r}")
    unv = [r for r in rec.get("sweep", []) if r.get("kind") == "unverified"]
    check("dose panel: the salt sweep's unverified record is shown on the panel it qualifies "
          "(HREQ-S-07, BUG-20261003-197)",
          bool(unv) and all(f"Unverified. {r['metric']}: {r['target']}" in (s.get("dosePanel") or "") for r in unv),
          f"records {unv}; panel {s.get('dosePanel')!r}")

    # labels on the Chronic and Custom tabs
    check(f"chronic: the tab's label is display.trajectory_label, byte-equal (HREQ-P-13, "
          f"BUG-20261003-196): {cfg['display.trajectory_label']!r}",
          chronic.get("chip") == cfg["display.trajectory_label"], f"chip {chronic.get('chip')!r}")
    custom = obs.get("custom") or {}
    check(f"custom: the tab's label is display.custom_run_label, byte-equal (HREQ-P-12, "
          f"BUG-20261003-196): {cfg['display.custom_run_label']!r}",
          custom.get("chip") == cfg["display.custom_run_label"], f"chip {custom.get('chip')!r}")

    # every scenario renders
    titles = rec.get("titles") or {}
    runs = obs.get("scenarios") or []
    bad = [f"{r['id']} (charts {r['bad']}, HUD {r['hud']!r})" for r in runs
           if r.get("bad") or r.get("hud") != titles.get(r["id"])]
    check(f"scenarios: all {len(titles)} registered scenarios render from the dropdown, every chart "
          "with a finite median and band, the HUD naming the scenario",
          bool(titles) and sorted(r["id"] for r in runs) == sorted(titles) and not bad,
          f"ran {[r['id'] for r in runs]}; bad {bad}")
    check("scenarios: the Chronic and Custom tabs run and render",
          bool(chronic) and not chronic.get("bad") and bool(custom) and not custom.get("bad")
          and chronic.get("hud") == titles.get("chronic_high_salt_30d"),
          f"chronic {chronic.get('bad')}, custom {custom.get('bad')}")
    leaked = [r["id"] for r in runs if r["id"] != "chronic_high_salt_30d"
              and ("Calibration, not validation" in (r.get("map") or "") or "Known divergence" in (r.get("vol") or ""))]
    check("chronic: the calibration and divergence notes appear only while the chronic scenario is shown",
          not leaked, f"notes shown under {leaked}")

    kb = obs.get("kb") or {}
    check("knowledge base: served from a subdirectory, the page loads kb/entities.json and the "
          "evidence records (BUG-20261003-195)",
          kb.get("entities") == "kb/entities.json" and (kb.get("evidence") or 0) > 0, f"kb {kb}")

    print(f"      viewer: {obs.get('viewer')}; scenario runs: "
          + ", ".join(f"{r['id']} {r['seconds']:.1f} s" for r in runs))
    for p in external:
        print(f"WARN  external resource, not the app (not failing): {p['kind']} {p['text']} {p['url']}".rstrip())
    for path in obs.get("shots") or []:
        print(f"      screenshot {path}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-skips", action="store_true", help="a skipped browser part is a failure")
    ap.add_argument("--page", default="index.html", help="page under reference/metabolic-map-v1/")
    ap.add_argument("--three", help="a local three.module.min.js served in place of the CDN copy")
    ap.add_argument("--screenshots", help="directory for light-theme screenshots of each surface")
    a = ap.parse_args(argv)
    print("health app smoke (HREQ-P-09; HREQ-S-01, S-03, S-04, S-07; HREQ-P-12, P-13)")
    cfg = config_strings(BASE_YAML.read_text(encoding="utf-8"))
    absent = [k for k in CONFIG_KEYS if k not in cfg]
    if not check("config: config/health/base.yaml holds the disclaimer, the validation status and "
                 "the five display labels", not absent, f"absent: {absent}"):
        return 1
    static_checks(cfg)
    paths, why = prerequisites()
    skipped = None
    if why:
        skipped = why
        print(f"SKIPPED  browser checks: {why} -- HREQ-P-09 and the rendered safety texts were NOT checked")
    else:
        if a.screenshots:
            Path(a.screenshots).mkdir(parents=True, exist_ok=True)
        three = str(Path(a.three).resolve()) if a.three else None
        shots = str(Path(a.screenshots).resolve()) if a.screenshots else None
        browser_checks(cfg, drive(paths, a.page, three, shots))
    failed = sum(1 for ok, _, _ in RESULTS if not ok)
    print(f"{len(RESULTS)} checks, {len(RESULTS) - failed} passed, {failed} failed"
          + (f"; browser checks SKIPPED ({skipped})" if skipped else ""))
    if failed or (skipped and a.no_skips):
        return 1
    return EXIT_SKIPPED if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
