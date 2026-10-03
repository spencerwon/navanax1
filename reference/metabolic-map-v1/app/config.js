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
