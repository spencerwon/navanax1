// app/config.js — deployment switches for the app (no behaviour lives here).
// EDUCATIONAL MODEL — NOT MEDICAL ADVICE.

/**
 * Is the curated knowledge base (kb/entities.json, kb/evidence.json) shipped next to app/?
 * true  → the app loads kb/ (it ships with this repo).
 * false → the app never requests kb/ files (no 404 in the console) and uses the V1 seed
 *         entities (app/entities.seed.js) and engine/evidence.engine.json instead.
 * Set false only for a copy served without the kb/ folder. (BUG-0002, TRACKER T-103)
 */
export const KB_AVAILABLE = true;

/**
 * The mandated display labels (HREQ-S-03, HREQ-S-04, HREQ-P-12), byte-equal to `display` in
 * config/health/base.yaml. The app is static and cannot read that file, so this is the one
 * copy it carries; tools/health_app_smoke.py holds this copy, and every place the page
 * renders it, equal to the file. The chronic trajectory label is the engine's own
 * (SCENARIOS.chronic_high_salt_30d.label), so it is not repeated here.
 */
export const LABELS = Object.freeze({
  index_label: 'an index, not a clinical measure',
  threshold_label: 'classification threshold, not a physiological parameter',
  custom_run_label: 'custom intervention on the 70 kg reference adult — no registered expectations',
});
