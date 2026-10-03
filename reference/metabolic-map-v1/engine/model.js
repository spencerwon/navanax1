// engine/model.js — body-fluid / electrolyte / renal ODE model (V1).
//
// EDUCATIONAL MODEL — NOT MEDICAL ADVICE. Not clinically validated.
//
// Units: time h, volume L, amount mmol (mOsm for osmoles), concentration mmol/L,
// pressure mmHg, osmolality mOsm/kg (1 kg water ≈ 1 L assumed), ADH pg/mL.
//
// Every numbered block below ("M0" ... "M10", each number used once) has a matching
// section in docs/health/01_METHODOLOGY.md §2.3 (the V1 engine/README.md is not
// vendored) and the same number in src/health/engine/model.py, so the code can be
// audited line by line. Every parameter `p.xxx` is defined, with citation, range and
// evidence grade, in params.json.
//
// Pure ES module, no imports beyond the parameter table: runs in Node and browsers.

import PARAM_TABLE from './params.data.js';

/** Ordered state vector (spec §3.1). */
export const STATE_KEYS = Object.freeze([
  'V_ecf',          // L    extracellular fluid volume
  'V_icf',          // L    intracellular fluid volume
  'Na_ecf',         // mmol total ECF sodium
  'K_icf',          // mmol total ICF potassium
  'osm_icf_solute', // mOsm intracellular osmotically active solute
  'ADH',            // pg/mL plasma vasopressin
  'V_gut_water',    // L    unabsorbed water in gut
  'Na_gut',         // mmol unabsorbed Na in gut
  'Thirst',         // 0..1 thirst drive (output signal only in V1)
  'Aldo',           // rel. aldosterone activity, 1 = baseline
  'ANP',            // rel. ANP, 1 = baseline
  'MAP',            // mmHg mean arterial pressure
  'R_auto',         // rel. slow whole-body autoregulation factor on MAP, 1 = baseline (D-2; Guyton)
]);
export const IDX = Object.freeze(Object.fromEntries(STATE_KEYS.map((k, i) => [k, i])));

/** Cumulative-flux ledger integrated alongside the state (for mass-balance audits). */
export const LEDGER_KEYS = Object.freeze([
  'water_in',  // L    ingested + metabolic water
  'water_out', // L    urine + insensible + fecal + sweat
  'na_in',     // mmol ingested Na
  'na_out',    // mmol urine + sweat Na
  'k_in',      // mmol ingested K
  'k_out',     // mmol urine K
]);

/** Derived (non-integrated) quantities returned by derived(). */
export const DERIVED_KEYS = Object.freeze([
  'Na_plasma',        // mmol/L
  'osm_plasma',       // mOsm/kg (calculated: 2[Na] + glucose/18 + BUN/2.8)
  'osm_icf',          // mOsm/kg
  'TBW',              // L total body water (ECF + ICF)
  'GFR',              // mL/min, absolute (alias of GFR_mlmin, kept for compatibility)
  'GFR_mlmin',        // mL/min, absolute: what this model body filters (D-3)
  'GFR_norm',         // mL/min per 1.73 m² body surface area (clinical convention; D-3)
  'FL_Na',            // mmol/h filtered Na load
  'FE_Na',            // % fractional excretion of Na
  'Na_excr',          // mmol/h urinary Na excretion
  'K_excr',           // mmol/h urinary K excretion
  'U_osm',            // mOsm/kg urine osmolality
  'urine_flow',       // L/h
  'urine_flow_mLmin', // mL/min
  'C_H2O',            // L/h free-water clearance (urine_flow − osmolar clearance)
  'ADH_target',       // pg/mL instantaneous secretion target
  'strain_transport', // strain index components (dimensionless loads ≥ 0)
  'strain_excretion',
  'strain_glomerular',
  'strain_concentrating',
  'strain_index',     // 0..1 composite KIDNEY STRAIN INDEX (an index, NOT a clinical measure)
]);

/**
 * The person every V1 number describes (decision D-3, approved by Spencer 2026-10-01).
 * Volumes, intakes and GFR_0 in params.json are textbook values for this person.
 * bsa_m2 = 1.73 m² is the conventional body surface area used to normalise GFR
 * ("per 1.73 m²"), so for this person absolute and normalised GFR are equal.
 * V2 will scale volumes and GFR with body size; until then this is fixed.
 */
export const REFERENCE_PERSON = Object.freeze({
  name: 'reference adult (70 kg)',
  mass_kg: 70,
  bsa_m2: 1.73,
  ageGroup: 'adult',
});
/** Body surface area that "normalised" GFR is expressed per (clinical convention). */
export const GFR_NORM_BSA_M2 = 1.73;

const LN2 = Math.LN2;
const H_PER_DAY = 24;

/** Deep copy of the parameter VALUES from params.json (spec §3 defaultParams). */
export function defaultParams() {
  const out = {};
  for (const [k, v] of Object.entries(PARAM_TABLE)) out[k] = v.value;
  return out;
}

/** Full parameter table (values + units + ranges + evidence), deep-copied. */
export function paramTable() {
  return JSON.parse(JSON.stringify(PARAM_TABLE));
}

// ---------------------------------------------------------------------------
// M0. Derived constants and the analytic reference steady state.
//
// The reference steady state is the state at which, with baseline inputs,
// every derivative in rhs() is exactly zero. It is obtained in closed form:
//   1. urine flow must equal net water intake            -> V_ur_ss
//   2. urine solute must equal solute intake (+ urea)     -> S_ss
//   3. urine osmolality U_ss = S_ss / V_ur_ss              (M7)
//   4. invert the ADH -> U_osm Hill curve                  -> ADH_ss
//   5. invert the osmolality -> ADH line (M4)              -> osm_ss -> [Na]_ss
//   6. choose FE0 so Na excretion equals Na intake at [Na]_ss, V_ecf_0, MAP_0.
// Note: osm_icf_solute − 2·K_icf is an exact invariant of the model (non-K ICF
// solute is neither produced nor excreted in V1), so the steady state is unique
// only once V_icf_0 fixes that invariant. That is why it is set analytically.
// ---------------------------------------------------------------------------
const CACHE = new WeakMap();

export function constants(p) {
  // Cache per params object; the snapshot check makes in-place edits of `p` safe.
  const hit = CACHE.get(p);
  if (hit && sameSnapshot(hit._snap, p)) return hit;

  const c_other = p.glucose_mgdl / 18 + p.bun_mgdl / 2.8;            // mOsm/kg, non-Na plasma solute
  const waterIn_h = p.waterIn_base_Ld / H_PER_DAY;                    // L/h
  const naIn_h = p.naIn_base_mmold / H_PER_DAY;                       // mmol/h
  const kIn_h = p.kIn_base_mmold / H_PER_DAY;                         // mmol/h
  const metab_h = p.water_metabolic_Ld / H_PER_DAY;                   // L/h
  const insens_h = p.insensible_Ld / H_PER_DAY;                       // L/h
  const fecal_h = p.fecal_water_Ld / H_PER_DAY;                       // L/h
  const urea_h = p.urea_excr_mosmd / H_PER_DAY;                       // mOsm/h
  const GFR0_Lh = p.GFR_0 * 60 / 1000;                                // mL/min -> L/h

  const V_ur_ss = waterIn_h + metab_h - insens_h - fecal_h;           // L/h
  const S_ss = 2 * naIn_h + 2 * kIn_h + urea_h;                       // mOsm/h
  const U_ss = S_ss / V_ur_ss;                                        // mOsm/kg
  const h = (U_ss - p.U_osm_min) / (p.U_osm_max - p.U_osm_min);       // fraction of concentrating range

  const feasible = V_ur_ss > 0 && h > 0 && h < 1;
  const ADH_ss = feasible ? p.adh_ec50 * Math.pow(h / (1 - h), 1 / p.adh_hill) : NaN;
  const osm_ss = p.adh_threshold + ADH_ss / p.adh_slope;
  const Na_ss = (osm_ss - c_other) / 2;
  const FL_ss = GFR0_Lh * Na_ss;
  const FE0 = naIn_h / FL_ss;                                         // baseline fractional excretion

  // Water-shift conductance from the equilibration time constant (M3):
  // linearised rate = Lp · (osm/V_icf + (osm − c_other)/V_ecf) = 1/tau.
  const Lp = 1 / (p.osm_eq_tau_h * (osm_ss / p.V_icf_0 + (osm_ss - c_other) / p.V_ecf_0));

  const C = {
    feasible, c_other, waterIn_h, naIn_h, kIn_h, metab_h, insens_h, fecal_h, urea_h, GFR0_Lh,
    V_ur_ss, S_ss, U_ss, ADH_ss, osm_ss, Na_ss, FL_ss, FE0, Lp,
    Treab_ss: FL_ss - naIn_h,
    k_gut_w: LN2 / p.gut_water_thalf_h,
    k_gut_na: LN2 / p.gut_na_thalf_h,
    k_adh: LN2 / p.adh_thalf_h,
    k_anp: LN2 / p.anp_thalf_h,
  };
  // Fastest time constant in the system (h) — used by the solver for step control.
  C.tau_min = Math.min(p.osm_eq_tau_h, 1 / C.k_gut_w, 1 / C.k_gut_na, 1 / C.k_adh, 1 / C.k_anp,
    p.thirst_tau_h, p.aldo_tau_h, p.map_tau_h);
  C._snap = snapshot(p);
  CACHE.set(p, C);
  return C;
}

function snapshot(p) {
  const keys = Object.keys(p);
  return { keys, vals: keys.map((k) => p[k]) };
}
function sameSnapshot(s, p) {
  const { keys, vals } = s;
  for (let i = 0; i < keys.length; i++) if (p[keys[i]] !== vals[i]) return false;
  return true;
}

/** Baseline inputs (continuous average diet) for given params. */
export function baselineInputs(p) {
  return {
    waterIn_Lh: p.waterIn_base_Ld / H_PER_DAY,
    naIn_mmolh: p.naIn_base_mmold / H_PER_DAY,
    kIn_mmolh: p.kIn_base_mmold / H_PER_DAY,
    sweat_Lh: 0,
    exercise: 0, // accepted for API compatibility; unused in V1
  };
}

/** Reference steady state (spec §3 initialState). Throws if params are physically infeasible. */
export function initialState(p) {
  const C = constants(p);
  if (!C.feasible) {
    throw new Error(`initialState: infeasible params (required urine osmolality ${C.U_ss.toFixed(0)} ` +
      `outside (${p.U_osm_min}, ${p.U_osm_max}) or non-positive urine flow ${C.V_ur_ss})`);
  }
  const y = new Float64Array(STATE_KEYS.length);
  y[IDX.V_ecf] = p.V_ecf_0;
  y[IDX.V_icf] = p.V_icf_0;
  y[IDX.Na_ecf] = C.Na_ss * p.V_ecf_0;
  y[IDX.K_icf] = p.K_icf_0;
  y[IDX.osm_icf_solute] = C.osm_ss * p.V_icf_0;
  y[IDX.ADH] = C.ADH_ss;
  y[IDX.V_gut_water] = C.waterIn_h / C.k_gut_w;   // gut content in balance with continuous intake
  y[IDX.Na_gut] = C.naIn_h / C.k_gut_na;
  y[IDX.Thirst] = clamp01(p.thirst_slope * (C.osm_ss - p.thirst_threshold));
  y[IDX.Aldo] = 1;
  y[IDX.ANP] = 1;
  y[IDX.MAP] = p.MAP_0;
  y[IDX.R_auto] = 1;
  return y;
}

// ---------------------------------------------------------------------------
// fluxes(): every physiological flux, computed once and shared by rhs(),
// derived() and the mass-balance ledger. inputs may be null (derived only).
// ---------------------------------------------------------------------------
export function fluxes(y, p, inputs, C = constants(p)) {
  const V_ecf = y[0], V_icf = y[1], Na_ecf = y[2], K_icf = y[3], S_icf = y[4], ADH = y[5];
  const V_gut = y[6], Na_gut = y[7], Aldo = y[9], ANP = y[10], MAP = y[11], R_auto = y[12];

  const vr = V_ecf / p.V_ecf_0;                 // relative ECF volume
  const Na_c = Na_ecf / V_ecf;                  // plasma [Na], mmol/L
  const osm_e = 2 * Na_c + C.c_other;           // plasma osmolality, mOsm/kg
  const osm_i = S_icf / V_icf;                  // ICF osmolality

  // M2. Gut absorption (first-order).
  const J_gut_w = C.k_gut_w * V_gut;            // L/h
  const J_gut_na = C.k_gut_na * Na_gut;         // mmol/h

  // M3. Osmotic water shift ECF -> ICF (positive = into cells).
  const J_shift = C.Lp * (osm_i - osm_e);       // L/h

  // M4. ADH secretion target: osmotic line whose threshold moves with volume
  //     (hypovolemia lowers threshold, expansion raises it).
  const adh_thr = p.adh_threshold + p.adh_vol_shift * 100 * (vr - 1);
  const ADH_target = Math.max(0, p.adh_slope * (osm_e - adh_thr));

  // M5. Thirst target (osmotic + hypovolemic), 0..1.
  const Thirst_target = clamp01(p.thirst_slope * (osm_e - p.thirst_threshold) +
    p.thirst_vol_gain * 100 * Math.max(0, 1 - vr));

  // M6. Volume-driven hormone and pressure targets.
  const Aldo_target = Math.pow(vr, -p.aldo_vol_exp);
  const ANP_target = Math.pow(vr, p.anp_vol_exp);
  // MAP: the long-term volume elasticity map_vol_exp is split into a fast part (cardiac output,
  // within hours) and a slow part delivered through R_auto (Guyton whole-body autoregulation:
  // over days to weeks, raised flow is converted into raised peripheral resistance).
  // At steady state R_auto = vr^(map_auto_frac·map_vol_exp), so MAP_ss = MAP_0·vr^map_vol_exp exactly
  // as before D-2; only the time course changes.
  const MAP_target = p.MAP_0 * Math.pow(vr, (1 - p.map_auto_frac) * p.map_vol_exp) * R_auto;
  const R_auto_target = Math.pow(vr, p.map_auto_frac * p.map_vol_exp);

  // M7. Kidney.
  const GFR = C.GFR0_Lh * Math.pow(MAP / p.MAP_0, p.gfr_map_exp) * Math.pow(vr, p.gfr_vol_exp); // L/h
  const FL = GFR * Na_c;                                                   // filtered Na, mmol/h
  const FE = Math.min(1, C.FE0
    * Math.pow(Aldo, -p.aldo_effect_exp)                                   // aldosterone: retains Na
    * Math.pow(ANP, p.anp_effect_exp)                                      // ANP: natriuretic
    * Math.exp(p.pn_gain * (MAP - p.MAP_0))                                // pressure natriuresis
    * Math.exp(p.na_osm_gain * (Na_c - C.Na_ss)));                         // osmotic natriuresis
  const Na_ur = FL * FE;                                                   // mmol/h
  const K_ur = C.kIn_h * Math.exp(p.k_excr_gain * (K_icf / p.K_icf_0 - 1)); // mmol/h
  const solute_ur = 2 * Na_ur + 2 * K_ur + C.urea_h;                       // mOsm/h
  const An = Math.pow(Math.max(ADH, 0), p.adh_hill);
  const U_osm = p.U_osm_min + (p.U_osm_max - p.U_osm_min) * An / (An + Math.pow(p.adh_ec50, p.adh_hill));
  const V_ur = solute_ur / U_osm;                                          // L/h
  const C_osm = solute_ur / osm_e;                                         // osmolar clearance, L/h

  // M8. Inputs and non-renal losses.
  const inp = inputs || ZERO_INPUTS;
  const sweat = inp.sweat_Lh || 0;
  const sweat_na = sweat * p.sweat_na_mmolL;

  return {
    vr, Na_c, osm_e, osm_i, J_gut_w, J_gut_na, J_shift, adh_thr, ADH_target, Thirst_target,
    Aldo_target, ANP_target, MAP_target, R_auto_target, GFR, FL, FE, Na_ur, K_ur, solute_ur, U_osm, V_ur, C_osm,
    waterIn: inp.waterIn_Lh || 0, naIn: inp.naIn_mmolh || 0, kIn: inp.kIn_mmolh || 0,
    sweat, sweat_na, metab: C.metab_h, insens: C.insens_h, fecal: C.fecal_h,
  };
}
const ZERO_INPUTS = Object.freeze({ waterIn_Lh: 0, naIn_mmolh: 0, kIn_mmolh: 0, sweat_Lh: 0, exercise: 0 });

/**
 * dy/dt (spec §3 rhs). t is unused (the model is autonomous; time-dependence
 * enters only through `inputs`) but kept in the signature per spec.
 * If `ledgerOut` (length 6) is supplied, the ledger derivatives are written into it.
 * `C` (from constants(p)) may be passed by hot loops to skip the cache lookup.
 */
export function rhs(t, y, p, inputs, out, ledgerOut, C = constants(p)) {
  const f = fluxes(y, p, inputs, C);
  const dy = out || new Float64Array(STATE_KEYS.length);
  // M1. Water and solute balances.
  dy[0] = f.J_gut_w + f.metab - f.J_shift - f.V_ur - f.insens - f.fecal - f.sweat;  // V_ecf
  dy[1] = f.J_shift;                                                               // V_icf
  dy[2] = f.J_gut_na - f.Na_ur - f.sweat_na;                                       // Na_ecf
  dy[3] = f.kIn - f.K_ur;                                                          // K_icf (absorbed K enters cells directly)
  dy[4] = 2 * (f.kIn - f.K_ur);                                                    // osm_icf_solute (K + anion)
  // M4-M6. First-order hormone / drive / pressure dynamics.
  dy[5] = C.k_adh * (f.ADH_target - y[5]);                                         // ADH (secretion = clearance·target)
  dy[6] = f.waterIn - f.J_gut_w;                                                   // V_gut_water
  dy[7] = f.naIn - f.J_gut_na;                                                     // Na_gut
  dy[8] = (f.Thirst_target - y[8]) / p.thirst_tau_h;                               // Thirst
  dy[9] = (f.Aldo_target - y[9]) / p.aldo_tau_h;                                   // Aldo
  dy[10] = C.k_anp * (f.ANP_target - y[10]);                                       // ANP
  dy[11] = (f.MAP_target - y[11]) / p.map_tau_h;                                   // MAP
  dy[12] = (f.R_auto_target - y[12]) / p.map_auto_tau_h;                           // R_auto (slow, days-weeks)
  // M9. Outputs that do not feed back: the cumulative-flux ledger (integrated beside the
  //     state for the mass-balance audit, LEDGER_KEYS) here, and derived() below, which
  //     assembles DERIVED_KEYS from fluxes() and the strain index (M10).
  if (ledgerOut) {
    ledgerOut[0] = f.waterIn + f.metab;
    ledgerOut[1] = f.V_ur + f.insens + f.fecal + f.sweat;
    ledgerOut[2] = f.naIn;
    ledgerOut[3] = f.Na_ur + f.sweat_na;
    ledgerOut[4] = f.kIn;
    ledgerOut[5] = f.K_ur;
  }
  return dy;
}

// ---------------------------------------------------------------------------
// M10. KIDNEY STRAIN INDEX
//
// THIS IS AN INDEX, NOT A CLINICAL MEASURE. It has no units, no validated
// thresholds and no diagnostic meaning. It summarises, on a 0..1 scale, how far
// four mechanistically motivated renal workloads are pushed ABOVE the model's
// own baseline:
//   transport     — tubular Na reabsorption (≈ renal O2 consumption; Brezis & Rosen 1995,
//                   Sejersted 1982 [dog])
//   excretion     — Na excretory burden relative to baseline excretion
//   glomerular    — glomerular pressure / hyperfiltration proxy (Brenner 1982):
//                   w_p·(MAP − MAP_0)/scale_pressure + w_f·(GFR/GFR_0 − 1)/scale_filtration,
//                   w_p = w_f = 0.5 (a mean) and scale_filtration = 0.1 by default
//                   (strain_w_glomerular_pressure, strain_w_glomerular_filtration,
//                   strain_scale_filtration: params.json rows since 1.1.0, BUG-20261003-095)
//   concentrating — fraction of the remaining urine-concentrating range in use
//                   (medullary transport demand; Brezis & Rosen 1995)
// raw   = Σ w_i · load_i        (weights and scales: params.json, all E-assumption)
// index = raw / (1 + raw)       (0 at baseline; 0.5 when the weighted load = 1 unit)
// ---------------------------------------------------------------------------
function strain(f, p, C, MAP) {
  const transport = Math.max(0, (f.FL - f.Na_ur) / C.Treab_ss - 1) / p.strain_scale_transport;
  const excretion = Math.max(0, f.Na_ur / C.naIn_h - 1) / p.strain_scale_excretion;
  const glomerular = p.strain_w_glomerular_pressure * Math.max(0, (MAP - p.MAP_0) / p.strain_scale_pressure) +
    p.strain_w_glomerular_filtration * Math.max(0, (f.GFR / C.GFR0_Lh - 1) / p.strain_scale_filtration);
  const concentrating = Math.max(0, (f.U_osm - C.U_ss) / (p.U_osm_max - C.U_ss));
  const raw = p.strain_w_transport * transport + p.strain_w_excretion * excretion +
    p.strain_w_glomerular * glomerular + p.strain_w_concentrating * concentrating;
  return { transport, excretion, glomerular, concentrating, index: raw / (1 + raw) };
}

/** Derived quantities for one state (spec §3 derived; block M9). Returns an object keyed by DERIVED_KEYS. */
export function derived(y, p, C = constants(p)) {
  const f = fluxes(y, p, null, C);
  const s = strain(f, p, C, y[11]);
  return {
    Na_plasma: f.Na_c,
    osm_plasma: f.osm_e,
    osm_icf: f.osm_i,
    TBW: y[0] + y[1],
    GFR: f.GFR * 1000 / 60,
    GFR_mlmin: f.GFR * 1000 / 60,
    GFR_norm: (f.GFR * 1000 / 60) * GFR_NORM_BSA_M2 / REFERENCE_PERSON.bsa_m2,
    FL_Na: f.FL,
    FE_Na: 100 * f.FE,
    Na_excr: f.Na_ur,
    K_excr: f.K_ur,
    U_osm: f.U_osm,
    urine_flow: f.V_ur,
    urine_flow_mLmin: f.V_ur * 1000 / 60,
    C_H2O: f.V_ur - f.C_osm,
    ADH_target: f.ADH_target,
    strain_transport: s.transport,
    strain_excretion: s.excretion,
    strain_glomerular: s.glomerular,
    strain_concentrating: s.concentrating,
    strain_index: s.index,
  };
}

function clamp01(x) { return x < 0 ? 0 : x > 1 ? 1 : x; }
