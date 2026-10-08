// app/viewer.js — Three.js scene for the Metabolic Map.
// Procedural low-poly body (no anatomy meshes; spec §1.5), organ systems as toggleable
// groups, hand-written orbit control, raycast hover/click picking, scale ladder with
// 2.5D schematic stages, and a live overlay driven by simulation state.
// Units: metres, feet at y = 0, body faces +z. EVERYTHING HERE IS AN APPROXIMATION.

import * as THREE from 'three';
import { token } from './charts.js';

const V = (x, y, z) => new THREE.Vector3(x, y, z);
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;

// Organ base colours (anatomical convention; listed in DESIGN_DECISIONS.md).
const ORGAN_COLORS = {
  heart: '#b8323a', aorta: '#c43d3d', venaCava: '#3d5fb8', kidney: '#9c4a44', adrenal: '#d7a24a',
  ureter: '#d9c27a', bladder: '#e0bf5a', stomach: '#d88f7d', intestine: '#d9a088', colon: '#c98d6f',
  brain: '#e2a3ad', pituitary: '#9e6bd6', muscle: '#c4574f', skin: '#d8b49c',
};
// Kidney strain colour rule (DESIGN_DECISIONS O1 / TRACKER D-1). The strain index has no
// clinical thresholds, so these breakpoints are display choices only. Compare the three
// rules live in app/design-options.html. Change the rule HERE ONLY: the kidney colour, the
// HUD legend bar and the stat-tile swatch (ui.js) all follow it.
//   'thirds'       organ base → amber at 0.33 → red at 0.66 (V1 default)
//   'gradient'     organ base → red, continuous, no breakpoints
//   'conservative' organ base → amber at 0.5 → red at 0.8
export const STRAIN_COLOR_RULE = 'thirds';
const STRAIN_AMBER = '#f0a020', STRAIN_RED = '#d03b3b';
export const STRAIN_RULES = Object.freeze({
  thirds: [[0, ORGAN_COLORS.kidney], [0.33, STRAIN_AMBER], [0.66, STRAIN_RED], [1, STRAIN_RED]],
  gradient: [[0, ORGAN_COLORS.kidney], [1, STRAIN_RED]],
  conservative: [[0, ORGAN_COLORS.kidney], [0.5, STRAIN_AMBER], [0.8, STRAIN_RED], [1, STRAIN_RED]],
});
/** Colour stops [[strain, '#hex'], ...] for a rule (default: STRAIN_COLOR_RULE). */
export function strainStops(rule = STRAIN_COLOR_RULE) {
  const stops = STRAIN_RULES[rule];
  if (!stops) throw new Error(`unknown strain colour rule "${rule}". Known: ${Object.keys(STRAIN_RULES).join(', ')}`);
  return stops;
}
const EXPAND_TINT = '#4f9fdc'; // volume above baseline
const SHRINK_TINT = '#c07a2c'; // volume below baseline

export function strainColor(s, rule = STRAIN_COLOR_RULE) {
  const STRAIN_STOPS = strainStops(rule);
  const c = new THREE.Color();
  for (let i = 1; i < STRAIN_STOPS.length; i++) {
    const [a, ca] = STRAIN_STOPS[i - 1], [b, cb] = STRAIN_STOPS[i];
    if (s <= b || i === STRAIN_STOPS.length - 1) {
      const f = Math.min(1, Math.max(0, (s - a) / (b - a)));
      return c.set(ca).lerp(new THREE.Color(cb), f);
    }
  }
  return c.set(ORGAN_COLORS.kidney);
}

function mat(color, extra = {}) {
  return new THREE.MeshStandardMaterial({ color, roughness: 0.62, metalness: 0.02, flatShading: true, ...extra });
}

function limb(a, b, r, material, seg = 7) {
  const dir = new THREE.Vector3().subVectors(b, a);
  const len = dir.length();
  const geo = new THREE.CapsuleGeometry(r, Math.max(0.001, len), 3, seg);
  const m = new THREE.Mesh(geo, material);
  m.position.copy(a).addScaledVector(dir, 0.5);
  m.quaternion.setFromUnitVectors(V(0, 1, 0), dir.normalize());
  return m;
}

function tube(points, r, material, seg = 48, radial = 7) {
  const curve = new THREE.CatmullRomCurve3(points);
  return new THREE.Mesh(new THREE.TubeGeometry(curve, seg, r, radial, false), material);
}

// ---------------------------------------------------------------------------
// Minimal orbit control: drag = rotate, right/shift drag or two fingers = pan,
// wheel or pinch = zoom. Damped; supports animated fly-to.
// ---------------------------------------------------------------------------
class Orbit {
  constructor(camera, dom) {
    this.camera = camera; this.dom = dom;
    this.target = V(0, 1.0, 0); this.goalTarget = this.target.clone();
    this.theta = 0.35; this.phi = 1.4; this.radius = 3.0;
    this.goal = { theta: this.theta, phi: this.phi, radius: this.radius };
    this.pointers = new Map(); this.moved = 0; this.mode = null; this.lastPinch = 0;
    dom.addEventListener('pointerdown', (e) => this.down(e));
    dom.addEventListener('pointermove', (e) => this.move(e));
    dom.addEventListener('pointerup', (e) => this.up(e));
    dom.addEventListener('pointercancel', (e) => this.up(e));
    dom.addEventListener('wheel', (e) => { e.preventDefault(); this.zoom(Math.exp(e.deltaY * 0.0012)); }, { passive: false });
    dom.addEventListener('contextmenu', (e) => e.preventDefault());
    dom.addEventListener('keydown', (e) => this.key(e));
  }
  down(e) {
    this.dom.setPointerCapture?.(e.pointerId);
    this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    this.moved = 0;
    this.mode = this.pointers.size === 2 ? 'pinch' : (e.button === 2 || e.shiftKey ? 'pan' : 'rotate');
    if (this.mode === 'pinch') this.lastPinch = this.pinchDist();
  }
  pinchDist() { const [a, b] = [...this.pointers.values()]; return Math.hypot(a.x - b.x, a.y - b.y); }
  move(e) {
    const p = this.pointers.get(e.pointerId); if (!p) return;
    const dx = e.clientX - p.x, dy = e.clientY - p.y;
    p.x = e.clientX; p.y = e.clientY;
    this.moved += Math.abs(dx) + Math.abs(dy);
    if (this.mode === 'pinch' && this.pointers.size === 2) {
      const d = this.pinchDist(); if (this.lastPinch) this.zoom(this.lastPinch / d); this.lastPinch = d; return;
    }
    if (this.mode === 'rotate') {
      this.goal.theta -= dx * 0.008;
      this.goal.phi = Math.min(Math.PI - 0.15, Math.max(0.15, this.goal.phi - dy * 0.008));
    } else if (this.mode === 'pan') this.pan(dx, dy);
  }
  up(e) { this.pointers.delete(e.pointerId); if (this.pointers.size === 0) this.mode = null; }
  pan(dx, dy) {
    const s = this.goal.radius * 0.0012;
    const right = V(1, 0, 0).applyQuaternion(this.camera.quaternion);
    const up = V(0, 1, 0).applyQuaternion(this.camera.quaternion);
    this.goalTarget.addScaledVector(right, -dx * s).addScaledVector(up, dy * s);
  }
  zoom(f) { this.goal.radius = Math.min(8, Math.max(0.12, this.goal.radius * f)); }
  key(e) {
    const k = e.key; let used = true;
    if (k === 'ArrowLeft') this.goal.theta += 0.12; else if (k === 'ArrowRight') this.goal.theta -= 0.12;
    else if (k === 'ArrowUp') this.goal.phi = Math.max(0.15, this.goal.phi - 0.1);
    else if (k === 'ArrowDown') this.goal.phi = Math.min(Math.PI - 0.15, this.goal.phi + 0.1);
    else if (k === '+' || k === '=') this.zoom(0.85); else if (k === '-') this.zoom(1.18);
    else used = false;
    if (used) e.preventDefault();
  }
  flyTo(target, radius, theta = null, phi = null) {
    this.goalTarget.copy(target); this.goal.radius = radius;
    if (theta !== null) this.goal.theta = theta; if (phi !== null) this.goal.phi = phi;
  }
  update(dt) {
    const k = REDUCED ? 1 : 1 - Math.exp(-dt * 7);
    this.theta += (this.goal.theta - this.theta) * k;
    this.phi += (this.goal.phi - this.phi) * k;
    this.radius += (this.goal.radius - this.radius) * k;
    this.target.lerp(this.goalTarget, k);
    const sp = new THREE.Spherical(this.radius, this.phi, this.theta);
    this.camera.position.setFromSpherical(sp).add(this.target);
    this.camera.lookAt(this.target);
  }
}

// ---------------------------------------------------------------------------
// Schematic stage drawings (canvas → texture). All labelled "schematic — not to scale".
// ---------------------------------------------------------------------------
const W = 1024, H = 768;
function stagePalette() {
  return {
    bg: token('--surface') || '#ffffff', ink: token('--ink') || '#111', ink2: token('--ink-2') || '#555',
    muted: token('--muted') || '#888', line: token('--grid') || '#ddd', accent: token('--accent') || '#2a6be0',
    water: token('--s1') || '#2a78d6', na: token('--s2') || '#eb6834', font: token('--font-body') || 'sans-serif',
    display: token('--font-display') || 'sans-serif',
  };
}
function header(g, P, title, sub) {
  g.fillStyle = P.bg; g.fillRect(0, 0, W, H);
  g.strokeStyle = P.line; g.lineWidth = 4; g.strokeRect(2, 2, W - 4, H - 4);
  g.fillStyle = P.ink; g.font = `600 40px ${P.display}`; g.textBaseline = 'top'; g.textAlign = 'left';
  g.fillText(title, 40, 32);
  g.fillStyle = P.muted; g.font = `24px ${P.font}`; g.fillText(sub, 40, 82);
  g.fillStyle = P.muted; g.font = `600 20px ${P.font}`; g.textAlign = 'right';
  g.fillText('Schematic, not to scale', W - 40, 40);
  g.textAlign = 'left';
}
function label(g, P, text, x, y, opts = {}) {
  g.font = `${opts.weight || 500} ${opts.size || 24}px ${P.font}`; g.fillStyle = opts.color || P.ink2;
  g.textAlign = opts.align || 'left'; g.textBaseline = 'middle'; g.fillText(text, x, y);
}
function arrow(g, x1, y1, x2, y2, color, w = 4) {
  g.strokeStyle = color; g.fillStyle = color; g.lineWidth = w; g.lineCap = 'round';
  g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke();
  const a = Math.atan2(y2 - y1, x2 - x1), s = 12 + w;
  g.beginPath(); g.moveTo(x2, y2); g.lineTo(x2 - s * Math.cos(a - 0.45), y2 - s * Math.sin(a - 0.45));
  g.lineTo(x2 - s * Math.cos(a + 0.45), y2 - s * Math.sin(a + 0.45)); g.closePath(); g.fill();
}
const f0 = (x) => (Number.isFinite(x) ? Math.round(x).toLocaleString('en-US') : '–');
const f1 = (x) => (Number.isFinite(x) ? x.toFixed(1) : '–');
const f2 = (x) => (Number.isFinite(x) ? x.toFixed(2) : '–');

function drawNephron(g, S) {
  const P = stagePalette();
  header(g, P, 'Nephron', 'Kidney → tissue. Live values from the median simulation.');
  // cortex / medulla bands, medulla shaded by concentrating activity
  const act = S.adhAct ?? 0.4;
  g.fillStyle = P.line; g.globalAlpha = 0.35; g.fillRect(40, 130, W - 80, 170); g.globalAlpha = 1;
  const grad = g.createLinearGradient(0, 300, 0, H - 40);
  grad.addColorStop(0, 'rgba(235,104,52,0.05)'); grad.addColorStop(1, `rgba(235,104,52,${0.08 + 0.32 * act})`);
  g.fillStyle = grad; g.fillRect(40, 300, W - 80, H - 340);
  label(g, P, 'Cortex', 56, 150, { size: 20, color: P.muted });
  label(g, P, 'Medulla', 56, 430, { size: 20, color: P.muted });
  label(g, P, 'Gradient set by ADH', 56, 458, { size: 18, color: P.muted });
  // nephron path
  g.strokeStyle = P.ink2; g.lineWidth = 14; g.lineJoin = 'round'; g.lineCap = 'round';
  g.beginPath();
  g.moveTo(200, 210);
  g.bezierCurveTo(260, 160, 300, 260, 340, 200); g.bezierCurveTo(370, 160, 400, 250, 410, 240);
  g.lineTo(420, 660); g.quadraticCurveTo(450, 710, 480, 660); g.lineTo(490, 250);
  g.bezierCurveTo(520, 180, 560, 260, 600, 200); g.lineTo(700, 180);
  g.stroke();
  // collecting duct
  g.strokeStyle = P.water; g.lineWidth = 22;
  g.beginPath(); g.moveTo(700, 150); g.lineTo(700, H - 70); g.stroke();
  // glomerulus
  g.fillStyle = 'rgba(196,61,61,0.85)'; g.beginPath(); g.arc(170, 220, 36, 0, Math.PI * 2); g.fill();
  g.strokeStyle = P.ink2; g.lineWidth = 4; g.beginPath(); g.arc(170, 220, 50, -0.6, Math.PI * 1.75); g.stroke();
  // water leaving the collecting duct (more arrows = more AQP2 = more ADH)
  const n = Math.round(1 + act * 6);
  for (let i = 0; i < n; i++) { const yy = 360 + i * 48; arrow(g, 715, yy, 790, yy - 10, P.water, 4); }
  label(g, P, 'H₂O reabsorbed', 800, 360, { size: 20, color: P.ink2 });
  // labels with live numbers
  label(g, P, 'Glomerulus', 60, 300, { size: 20 });
  label(g, P, `GFR ${f0(S.gfr)} mL/min`, 60, 330, { size: 22, color: P.ink, weight: 600 });
  label(g, P, `${f0(S.gfrNorm ?? S.gfr)} per 1.73 m²`, 60, 358, { size: 20, color: P.ink2 });
  label(g, P, 'Proximal tubule', 250, 140, { size: 20 });
  label(g, P, 'Loop of Henle', 330, 700, { size: 20 });
  label(g, P, 'Distal tubule', 520, 140, { size: 20 });
  label(g, P, `FE Na ${f2(S.fena)} %`, 508, 290, { size: 22, color: P.ink, weight: 600 });
  label(g, P, 'Collecting duct', 718, 170, { size: 20 });
  label(g, P, `Urine ${f2(S.urine)} L/h`, 728, H - 110, { size: 22, color: P.ink, weight: 600 });
  label(g, P, `${f0(S.uosm)} mOsm/kg`, 728, H - 78, { size: 22, color: P.ink, weight: 600 });
}

function drawCell(g, S) {
  const P = stagePalette();
  header(g, P, 'Principal cell', 'Collecting duct. AQP2 in the apical membrane follows ADH.');
  const act = S.adhAct ?? 0.4;
  label(g, P, 'Tubular fluid (urine side)', 40, 124, { size: 20, color: P.muted });
  label(g, P, 'Interstitium and blood', 320, H - 40, { size: 20, color: P.muted });
  // cell body
  g.fillStyle = 'rgba(42,120,214,0.07)'; g.strokeStyle = P.ink2; g.lineWidth = 6;
  g.beginPath(); g.roundRect(120, 190, W - 240, H - 290, 40); g.fill(); g.stroke();
  // nucleus
  g.fillStyle = 'rgba(158,107,214,0.25)'; g.beginPath(); g.ellipse(W / 2 - 120, H / 2 + 40, 90, 60, 0, 0, Math.PI * 2); g.fill();
  label(g, P, 'Nucleus', W / 2 - 120, H / 2 + 40, { size: 20, align: 'center', color: P.ink2 });
  // AQP2: total 12, inserted share follows ADH action on urine concentration
  const total = 12, inserted = Math.round(1 + act * (total - 1));
  for (let i = 0; i < total; i++) {
    const inMem = i < inserted;
    const x = inMem ? 360 + i * 44 : 300 + ((i - inserted) % 6) * 70;
    const y = inMem ? 190 : 330 + Math.floor((i - inserted) / 6) * 60;
    g.fillStyle = P.water; g.globalAlpha = inMem ? 1 : 0.45;
    g.beginPath(); g.roundRect(x - 12, y - 18, 24, 36, 8); g.fill(); g.globalAlpha = 1;
    if (inMem) arrow(g, x, 140, x, 168, P.water, 3);
  }
  label(g, P, `AQP2 · ${inserted} of ${total} inserted`, 360, 245, { size: 22, color: P.ink, weight: 600 });
  label(g, P, 'AQP2 vesicles', 300, 300, { size: 20 });
  // ENaC
  g.fillStyle = P.na; g.beginPath(); g.roundRect(200, 172, 40, 36, 8); g.fill();
  arrow(g, 220, 140, 220, 168, P.na, 3); label(g, P, 'ENaC · Na⁺', 160, 245, { size: 20 });
  // basolateral: Na/K-ATPase, AQP3/4, V2R
  const by = H - 100;
  g.fillStyle = P.na; g.beginPath(); g.arc(260, by, 26, 0, Math.PI * 2); g.fill();
  label(g, P, 'Na⁺/K⁺-ATPase', 260, by - 48, { size: 20, align: 'center' });
  arrow(g, 250, by + 10, 250, by + 50, P.na, 3);
  g.fillStyle = P.water; g.beginPath(); g.roundRect(470, by - 18, 24, 36, 8); g.fill();
  g.beginPath(); g.roundRect(510, by - 18, 24, 36, 8); g.fill();
  label(g, P, 'AQP3/4', 502, by - 48, { size: 20, align: 'center' });
  g.fillStyle = P.accent; g.beginPath(); g.roundRect(740, by - 22, 34, 44, 8); g.fill();
  label(g, P, 'V2 receptor', 757, by - 52, { size: 20, align: 'center' });
  const adhDots = Math.max(1, Math.min(8, Math.round(S.adh * 1.5)));
  for (let i = 0; i < adhDots; i++) { g.fillStyle = '#9e6bd6'; g.beginPath(); g.arc(800 + (i % 4) * 26, by + 30 + Math.floor(i / 4) * 24, 9, 0, Math.PI * 2); g.fill(); }
  label(g, P, `ADH ${f1(S.adh)} pg/mL`, 790, by + 90 > H - 20 ? by + 80 : by + 90, { size: 22, color: P.ink, weight: 600 });
}

function drawPathway(g, S) {
  const P = stagePalette();
  header(g, P, 'ADH → V2R → cAMP → AQP2', 'Node shading shows modelled activation (ADH Hill curve).');
  const act = S.adhAct ?? 0.4;
  const nodes = [
    ['Plasma ADH', `${f1(S.adh)} pg/mL`], ['V2 receptor', 'basolateral'], ['Gs protein', ''], ['Adenylyl cyclase', ''],
    ['cAMP', ''], ['PKA', ''], ['AQP2 insertion', 'apical'], ['Water reabsorbed', ''], ['Urine osmolality', `${f0(S.uosm)} mOsm/kg`],
  ];
  const pos = [];
  const cols = 3, bw = 250, bh = 110, gx = 300, gy = 200, ox = 70, oy = 150;
  nodes.forEach((_, i) => {
    const r = Math.floor(i / cols), c0 = i % cols; const c = r % 2 === 0 ? c0 : cols - 1 - c0;
    pos.push([ox + c * gx, oy + r * gy]);
  });
  for (let i = 0; i < nodes.length - 1; i++) {
    const [x1, y1] = pos[i], [x2, y2] = pos[i + 1];
    if (y1 === y2) arrow(g, x1 + (x2 > x1 ? bw : 0), y1 + bh / 2, x2 + (x2 > x1 ? 0 : bw), y2 + bh / 2, P.ink2, 4);
    else arrow(g, x1 + bw / 2, y1 + bh, x2 + bw / 2, y2, P.ink2, 4);
  }
  nodes.forEach(([name, sub], i) => {
    const [x, y] = pos[i];
    g.fillStyle = `rgba(42,120,214,${0.1 + 0.6 * act})`;
    g.strokeStyle = P.accent; g.lineWidth = 3;
    g.beginPath(); g.roundRect(x, y, bw, bh, 18); g.fill(); g.stroke();
    label(g, P, name, x + bw / 2, y + (sub ? 40 : bh / 2), { size: 26, align: 'center', color: P.ink, weight: 600 });
    if (sub) label(g, P, sub, x + bw / 2, y + 76, { size: 22, align: 'center' });
  });
  label(g, P, `Activation ${Math.round(act * 100)} % of maximal concentrating effect`, 70, H - 40, { size: 22, color: P.ink2 });
}

function labelSprite(text, P, size = 0.03, weight = 600) {
  const c = document.createElement('canvas'); const g = c.getContext('2d');
  const font = `${weight} 64px ${P.font}`; g.font = font;
  const w = Math.ceil(g.measureText(text).width) + 24;
  c.width = w; c.height = 88;
  g.font = font; g.fillStyle = P.ink; g.textAlign = 'center'; g.textBaseline = 'middle';
  g.fillText(text, w / 2, 44);
  const tex = new THREE.CanvasTexture(c); tex.colorSpace = THREE.SRGBColorSpace;
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, transparent: true }));
  s.scale.set(size * (w / 88), size, 1); s.renderOrder = 10;
  return s;
}

// ---------------------------------------------------------------------------
export class Viewer {
  constructor(host, { onSelect, onHover } = {}) {
    this.host = host; this.onSelect = onSelect; this.onHover = onHover;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    this.canvas = this.renderer.domElement;
    this.canvas.className = 'viewer-canvas'; this.canvas.tabIndex = 0;
    this.canvas.setAttribute('aria-label', '3D body viewer. Drag to rotate, scroll to zoom, arrow keys also rotate.');
    host.prepend(this.canvas);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(35, 1, 0.01, 50);
    this.orbit = new Orbit(this.camera, this.canvas);
    this.meshes = {}; this.systems = {}; this.pickables = [];
    this.anchors = []; this.state = null; this.level = 'body'; this.selected = null;
    this.fps = 60; this.frames = 0; this.fpsT = performance.now();
    this.clock = new THREE.Clock();

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 1.4));
    const key = new THREE.DirectionalLight(0xffffff, 1.6); key.position.set(1.5, 3, 2.5); this.scene.add(key);
    const rim = new THREE.DirectionalLight(0x9fc4ff, 0.8); rim.position.set(-2, 1.5, -2.5); this.scene.add(rim);

    this.buildBody();
    this.buildStages();
    this.applyTheme();

    this.hoverEl = document.createElement('div'); this.hoverEl.className = 'viewer-hover'; this.hoverEl.hidden = true;
    host.append(this.hoverEl);
    this.raycaster = new THREE.Raycaster(); this.ndc = new THREE.Vector2();
    this.canvas.addEventListener('pointermove', (e) => this.pointerMove(e));
    this.canvas.addEventListener('pointerleave', () => this.setHover(null));
    this.canvas.addEventListener('pointerup', (e) => { if (this.orbit.moved < 6 && e.button === 0) this.click(e); });

    new ResizeObserver(() => this.resize()).observe(host);
    const retheme = () => { this.applyTheme(); this.redrawStage(true); };
    matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', retheme);
    new MutationObserver(retheme).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    this.resize();
    this.renderer.setAnimationLoop(() => this.frame());
  }

  applyTheme() {
    this.scene.background = new THREE.Color(token('--viewer-bg') || '#e9eef4');
  }

  register(name, obj, system, entity) {
    obj.traverse((o) => { if (o.isMesh) { o.userData.part = name; o.userData.entity = entity; this.pickables.push(o); } });
    this.meshes[name] = obj;
    if (system) {
      if (!this.systems[system]) { this.systems[system] = new THREE.Group(); this.systems[system].name = system; this.scene.add(this.systems[system]); }
      this.systems[system].add(obj);
    }
  }

  buildBody() {
    const C = ORGAN_COLORS;
    // --- skin (semi-transparent shell) ---
    const skinMat = mat(C.skin, { transparent: true, opacity: 0.2, depthWrite: false, side: THREE.DoubleSide });
    this.skinMat = skinMat;
    const skin = new THREE.Group();
    const prof = [[0.0, 0.8], [0.13, 0.81], [0.17, 0.9], [0.16, 1.0], [0.14, 1.1], [0.15, 1.22], [0.175, 1.36], [0.165, 1.44], [0.07, 1.49], [0.0, 1.5]]
      .map(([r, y]) => new THREE.Vector2(r, y));
    const torso = new THREE.Mesh(new THREE.LatheGeometry(prof, 14), skinMat); torso.scale.set(1, 1, 0.62); skin.add(torso);
    skin.add(limb(V(0, 1.47, 0), V(0, 1.57, 0), 0.05, skinMat));
    const head = new THREE.Mesh(new THREE.IcosahedronGeometry(0.105, 2), skinMat); head.position.set(0, 1.66, 0); head.scale.set(0.9, 1.1, 1); skin.add(head);
    const L = (s) => ({
      sh: V(0.2 * s, 1.42, 0), el: V(0.25 * s, 1.15, 0), wr: V(0.28 * s, 0.9, 0.03),
      hip: V(0.09 * s, 0.86, 0), kn: V(0.1 * s, 0.47, 0.01), an: V(0.1 * s, 0.08, 0),
    });
    const muscleMat = mat(C.muscle, { transparent: true, opacity: 0.32, depthWrite: false });
    this.muscleMat = muscleMat;
    const muscle = new THREE.Group();
    for (const s of [1, -1]) {
      const p = L(s);
      skin.add(limb(p.sh, p.el, 0.048, skinMat), limb(p.el, p.wr, 0.04, skinMat), limb(p.hip, p.kn, 0.078, skinMat), limb(p.kn, p.an, 0.057, skinMat));
      const hand = new THREE.Mesh(new THREE.IcosahedronGeometry(0.045, 1), skinMat); hand.position.copy(p.wr).add(V(0.01 * s, -0.06, 0)); hand.scale.set(0.6, 1.2, 0.4); skin.add(hand);
      const foot = new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.05, 0.22), skinMat); foot.position.set(0.1 * s, 0.03, 0.06); skin.add(foot);
      muscle.add(limb(p.sh, p.el, 0.036, muscleMat), limb(p.el, p.wr, 0.029, muscleMat), limb(p.hip, p.kn, 0.062, muscleMat), limb(p.kn, p.an, 0.044, muscleMat));
    }
    // trunk muscles (pectoral / paraspinal hints)
    for (const s of [1, -1]) muscle.add(limb(V(0.08 * s, 1.36, 0.06), V(0.12 * s, 1.3, 0.05), 0.04, muscleMat));
    skin.renderOrder = 3; skin.traverse((o) => { o.renderOrder = 3; });
    muscle.traverse((o) => { o.renderOrder = 2; });
    this.register('skin', skin, 'skin', 'organ:skin');
    this.register('muscle', muscle, 'muscle', 'tissue:muscle');

    // --- cardiovascular ---
    const heart = new THREE.Mesh(new THREE.IcosahedronGeometry(0.055, 2), mat(C.heart));
    heart.scale.set(1, 1.15, 0.85); heart.rotation.z = -0.45; heart.position.set(0.025, 1.3, 0.035);
    this.register('heart', heart, 'cardiovascular', 'organ:heart');
    const aortaPts = [V(0.02, 1.33, 0.02), V(0.03, 1.41, 0.0), V(0.0, 1.43, -0.04), V(-0.012, 1.37, -0.065), V(-0.006, 1.15, -0.072), V(0, 0.95, -0.065), V(0, 0.88, -0.045)];
    const aorta = new THREE.Group();
    aorta.add(tube(aortaPts, 0.012, mat(C.aorta)));
    for (const s of [1, -1]) aorta.add(tube([V(0, 0.885, -0.045), V(0.05 * s, 0.84, -0.02), V(0.085 * s, 0.76, 0.0)], 0.008, mat(C.aorta), 16));
    // renal arteries
    for (const s of [1, -1]) aorta.add(tube([V(0, 1.05, -0.07), V(0.04 * s, 1.05, -0.065), V(0.065 * s, 1.05, -0.06)], 0.005, mat(C.aorta), 8));
    this.register('aorta', aorta, 'cardiovascular', 'organ:aorta');
    const vc = new THREE.Group();
    vc.add(tube([V(-0.035, 1.43, -0.02), V(-0.03, 1.33, 0.01), V(-0.034, 1.16, -0.055), V(-0.03, 0.93, -0.05), V(-0.02, 0.88, -0.035)], 0.013, mat(C.venaCava)));
    for (const s of [1, -1]) vc.add(tube([V(-0.02, 0.88, -0.035), V(0.04 * s - 0.01, 0.83, -0.01), V(0.08 * s, 0.75, 0.01)], 0.008, mat(C.venaCava), 16));
    this.register('venaCava', vc, 'cardiovascular', 'organ:vena-cava');

    // --- renal / urinary ---
    this.kidneyMat = mat(C.kidney, { emissive: 0x000000 });
    const kidneys = new THREE.Group();
    for (const s of [1, -1]) {
      const k = new THREE.Mesh(new THREE.IcosahedronGeometry(1, 2), this.kidneyMat);
      k.scale.set(0.042, 0.072, 0.032); k.position.set(0.085 * s, 1.05, -0.06); k.rotation.z = 0.18 * s;
      kidneys.add(k);
    }
    this.register('kidney', kidneys, 'renal', 'organ:kidney');
    const ureters = new THREE.Group();
    for (const s of [1, -1]) ureters.add(tube([V(0.07 * s, 1.02, -0.055), V(0.06 * s, 0.94, -0.04), V(0.035 * s, 0.875, 0.02)], 0.004, mat(C.ureter), 20, 5));
    this.register('ureter', ureters, 'renal', 'organ:ureters');
    this.bladderMat = mat(C.bladder, { transparent: true, opacity: 0.9 });
    const bladder = new THREE.Mesh(new THREE.IcosahedronGeometry(0.035, 2), this.bladderMat);
    bladder.position.set(0, 0.86, 0.04);
    this.register('bladder', bladder, 'renal', 'organ:bladder');

    // --- endocrine ---
    const adrenals = new THREE.Group();
    for (const s of [1, -1]) { const a = new THREE.Mesh(new THREE.ConeGeometry(0.022, 0.03, 5), mat(C.adrenal)); a.position.set(0.08 * s, 1.135, -0.06); adrenals.add(a); }
    this.register('adrenal', adrenals, 'endocrine', 'organ:adrenal');
    this.pitMat = mat(C.pituitary, { emissive: new THREE.Color(C.pituitary), emissiveIntensity: 0.4 });
    const pit = new THREE.Mesh(new THREE.IcosahedronGeometry(0.012, 1), this.pitMat);
    pit.position.set(0, 1.605, 0.025);
    this.register('pituitary', pit, 'endocrine', 'organ:pituitary');

    // --- nervous ---
    const brain = new THREE.Mesh(new THREE.IcosahedronGeometry(0.085, 2), mat(C.brain, { transparent: true, opacity: 0.75 }));
    brain.scale.set(0.92, 0.72, 1.08); brain.position.set(0, 1.685, -0.005);
    this.register('brain', brain, 'nervous', 'organ:brain');

    // --- GI ---
    const stomach = tube([V(0.02, 1.25, 0.02), V(0.06, 1.2, 0.05), V(0.095, 1.14, 0.055), V(0.065, 1.09, 0.065), V(0.0, 1.095, 0.065)], 0.03, mat(C.stomach), 32, 8);
    this.register('stomach', stomach, 'gi', 'organ:stomach');
    const gut = new THREE.Group();
    const knot = new THREE.Mesh(new THREE.TorusKnotGeometry(0.048, 0.011, 120, 6, 3, 7), mat(C.intestine));
    knot.position.set(0, 0.97, 0.06); knot.scale.set(1.1, 0.8, 0.55); gut.add(knot);
    gut.add(tube([V(-0.1, 0.9, 0.05), V(-0.11, 1.05, 0.05), V(0.0, 1.07, 0.07), V(0.11, 1.05, 0.05), V(0.1, 0.9, 0.05), V(0.03, 0.86, 0.02)], 0.017, mat(C.colon), 48, 7));
    this.register('intestine', gut, 'gi', 'organ:intestine');
    this.kidneyPos = V(0.085, 1.05, -0.06);
  }

  buildStages() {
    // Sub-organ schematic stages float beside the right kidney (viewer's left), facing +z.
    this.stagePos = V(-0.55, 1.08, 0.12);
    this.stageCanvas = document.createElement('canvas'); this.stageCanvas.width = W; this.stageCanvas.height = H;
    this.stageTex = new THREE.CanvasTexture(this.stageCanvas); this.stageTex.colorSpace = THREE.SRGBColorSpace;
    this.stageTex.anisotropy = 4;
    const plane = new THREE.Mesh(new THREE.PlaneGeometry(0.48, 0.36), new THREE.MeshBasicMaterial({ map: this.stageTex, side: THREE.DoubleSide, transparent: true }));
    plane.position.copy(this.stagePos); plane.visible = false; plane.renderOrder = 5;
    this.stagePlane = plane; this.scene.add(plane);
    // leader line from kidney to the stage
    const lg = new THREE.BufferGeometry().setFromPoints([V(-0.085, 1.05, -0.06), V(-0.31, 1.08, 0.12)]);
    this.leader = new THREE.Line(lg, new THREE.LineBasicMaterial({ color: 0x8391a5 })); this.leader.visible = false; this.scene.add(this.leader);
    // molecule: AVP as a residue-level ball-and-stick (cyclic 1–6 via disulfide, tail 7–9)
    const mol = new THREE.Group(); mol.position.copy(this.stagePos); mol.visible = false;
    const seq = ['C', 'Y', 'F', 'Q', 'N', 'C', 'P', 'R', 'G'];
    const cols = { C: '#e0bf5a', Y: '#2a78d6', F: '#2a78d6', Q: '#1baf7a', N: '#1baf7a', P: '#898781', R: '#4a3aa7', G: '#898781' };
    const pts = [];
    for (let i = 0; i < 6; i++) { const a = Math.PI / 2 + (i * Math.PI * 2) / 6; pts.push(V(Math.cos(a) * 0.07, Math.sin(a) * 0.07, Math.sin(i) * 0.012)); }
    pts.push(V(-0.03, -0.12, 0.015), V(0.02, -0.17, -0.01), V(0.0, -0.225, 0.012));
    // re-centre
    const ctr = pts.reduce((a, p) => a.add(p), V(0, 0, 0)).multiplyScalar(1 / pts.length); pts.forEach((p) => p.sub(ctr));
    const ringC = pts.slice(0, 6).reduce((a, p) => a.add(p), V(0, 0, 0)).multiplyScalar(1 / 6);
    const P = stagePalette();
    pts.forEach((p, i) => {
      const s = new THREE.Mesh(new THREE.IcosahedronGeometry(0.02, 2), mat(cols[seq[i]], { flatShading: false }));
      s.position.copy(p); mol.add(s);
      // Q4 sits above the tail and P7 starts it, so steer those two labels sideways.
      const out = i === 3 ? V(1, -0.5, 0).normalize() : i === 6 ? V(-1, 0, 0)
        : i < 6 ? p.clone().sub(ringC).setZ(0).normalize() : V(1, 0.15, 0).normalize();
      const lab = labelSprite(`${seq[i]}${i + 1}`, P, 0.022); lab.position.copy(p).addScaledVector(out, 0.042).add(V(0, 0, 0.02)); mol.add(lab);
    });
    const bond = (a, b, color, r = 0.005) => {
      const m = limb(a, b, r, mat(color, { flatShading: false }), 6); mol.add(m);
    };
    for (let i = 0; i < 5; i++) bond(pts[i], pts[i + 1], '#9aa4b2');
    bond(pts[5], pts[6], '#9aa4b2'); bond(pts[6], pts[7], '#9aa4b2'); bond(pts[7], pts[8], '#9aa4b2');
    bond(pts[0], pts[5], '#e0bf5a', 0.007); // disulfide C1–C6
    const ss = labelSprite('S–S', P, 0.018, 500); ss.position.copy(pts[0]).lerp(pts[5], 0.5).add(V(0.035, 0.01, 0.02)); mol.add(ss);
    const top = pts[0].y + 0.07;
    const title = labelSprite('Vasopressin (AVP)', P, 0.03); title.position.set(0, top + 0.05, 0); mol.add(title);
    const note = labelSprite('Residue-level schematic, not atomic', P, 0.017, 500); note.position.set(0, top + 0.017, 0); mol.add(note);
    this.molecule = mol; this.scene.add(mol);
    this.stageDrawn = 0;
  }

  /** Show/hide an organ system group. */
  setSystemVisible(sys, on) { if (this.systems[sys]) this.systems[sys].visible = on; }

  /** Move to a rung of the scale ladder. `part` is the viewer mesh name for organ scale. */
  setLevel(level, part = null) {
    this.level = level;
    const schem = ['tissue', 'cell', 'pathway'].includes(level);
    this.stagePlane.visible = schem; this.leader.visible = schem || level === 'molecule';
    this.molecule.visible = level === 'molecule';
    const dimBody = level !== 'body' && level !== 'organ';
    this.skinMat.opacity = dimBody ? 0.06 : 0.2;
    this.muscleMat.opacity = dimBody ? 0.1 : 0.32;
    if (level === 'body') this.orbit.flyTo(V(0, 1.0, 0), 3.0, 0.35, 1.4);
    else if (level === 'organ') {
      const obj = this.meshes[part || 'kidney'];
      const box = new THREE.Box3().setFromObject(obj); const sph = box.getBoundingSphere(new THREE.Sphere());
      const big = ['skin', 'muscle'].includes(part);
      this.orbit.flyTo(sph.center, big ? 2.6 : Math.max(0.35, sph.radius * 5.5), part === 'kidney' ? Math.PI * 0.85 : 0.35, 1.45);
    } else if (level === 'molecule') this.orbit.flyTo(this.stagePos, this.fitRadius(0.42, 0.52), 0, Math.PI / 2);
    else this.orbit.flyTo(this.stagePos, this.fitRadius(0.48, 0.36), 0, Math.PI / 2);
    if (schem) this.redrawStage(true);
  }

  /** Camera distance that fits a w × h (m) plane with a 10% margin. */
  fitRadius(w, h) {
    const t = Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2));
    return 1.1 * Math.max(h / 2 / t, w / 2 / (t * this.camera.aspect));
  }

  select(part) {
    if (this.selected) this.highlight(this.selected, 0);
    this.selected = part;
    if (part) this.highlight(part, 0.35);
  }

  highlight(part, amt) {
    const obj = this.meshes[part]; if (!obj) return;
    const col = new THREE.Color(token('--accent') || '#2a6be0');
    obj.traverse((o) => {
      if (!o.isMesh || !o.material.emissive) return;
      if (o.userData.baseEmissive === undefined) { o.userData.baseEmissive = o.material.emissive.clone(); o.userData.baseEI = o.material.emissiveIntensity; }
      if (part === 'pituitary') return; // pituitary emissive is driven by ADH
      if (amt > 0) { o.material.emissive.copy(col); o.material.emissiveIntensity = amt; }
      else { o.material.emissive.copy(o.userData.baseEmissive); o.material.emissiveIntensity = o.userData.baseEI; }
    });
  }

  pick(e) {
    const r = this.canvas.getBoundingClientRect();
    this.ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    this.raycaster.setFromCamera(this.ndc, this.camera);
    const vis = this.pickables.filter((m) => { let o = m; while (o) { if (!o.visible) return false; o = o.parent; } return true; });
    const hits = this.raycaster.intersectObjects(vis, false);
    // prefer anything inside the skin/muscle shells
    const inner = hits.find((h) => !['skin', 'muscle'].includes(h.object.userData.part));
    return inner || hits.find((h) => h.object.userData.part === 'muscle') || hits[0] || null;
  }

  pointerMove(e) {
    if (this.orbit.mode) { this.setHover(null); return; }
    const h = this.pick(e);
    this.setHover(h ? h.object.userData.part : null, e);
  }

  setHover(part, e) {
    if (this.hovered && this.hovered !== this.selected) this.highlight(this.hovered, 0);
    this.hovered = part;
    if (part && part !== this.selected) this.highlight(part, 0.22);
    if (!part) { this.hoverEl.hidden = true; this.canvas.style.cursor = ''; this.onHover?.(null); return; }
    this.canvas.style.cursor = 'pointer';
    const name = this.onHover?.(part) || part;
    this.hoverEl.textContent = name; this.hoverEl.hidden = false;
    const r = this.host.getBoundingClientRect();
    this.hoverEl.style.left = `${Math.min(e.clientX - r.left + 14, r.width - this.hoverEl.offsetWidth - 8)}px`;
    this.hoverEl.style.top = `${e.clientY - r.top + 14}px`;
  }

  click(e) {
    const h = this.pick(e);
    if (h) this.onSelect?.(h.object.userData.part, h.object.userData.entity);
  }

  /** Programmatic click at a mesh's projected screen position (used by tests). */
  screenPointOf(part) {
    const obj = this.meshes[part]; if (!obj) return null;
    const p = new THREE.Box3().setFromObject(obj).getCenter(new THREE.Vector3()).project(this.camera);
    const r = this.canvas.getBoundingClientRect();
    return { x: r.left + ((p.x + 1) / 2) * r.width, y: r.top + ((1 - p.y) / 2) * r.height };
  }

  /** Keep an HTML element pinned to a mesh. */
  addAnchor(el, part, offset = V(0, 0, 0)) { this.anchors.push({ el, part, offset }); this.host.append(el); }

  /**
   * Live overlay. s = { strain, bladderL, dEcf, dIcf (fractions), adh, adhAct, map, uosm, gfr, fena, urine }
   */
  setState(s) {
    this.state = s;
    this.kidneyMat.color.copy(strainColor(s.strain));
    if (this.selected !== 'kidney' && this.hovered !== 'kidney') {
      this.kidneyMat.emissive.copy(strainColor(s.strain)).multiplyScalar(Math.min(0.5, s.strain * 0.6));
      this.kidneyMat.emissiveIntensity = 1;
    }
    const tint = (base, d) => {
      const c = new THREE.Color(base); const f = Math.min(1, Math.abs(d) / 0.04);
      return c.lerp(new THREE.Color(d >= 0 ? EXPAND_TINT : SHRINK_TINT), f * 0.85);
    };
    this.skinMat.color.copy(tint(ORGAN_COLORS.skin, s.dEcf));
    this.muscleMat.color.copy(tint(ORGAN_COLORS.muscle, s.dIcf));
    const bl = this.meshes.bladder; const k = Math.cbrt(Math.max(0.03, s.bladderL) / 0.15);
    bl.scale.setScalar(Math.max(0.55, Math.min(1.9, k)));
    if (['tissue', 'cell', 'pathway'].includes(this.level)) this.redrawStage(false);
  }

  redrawStage(force) {
    if (!this.stagePlane.visible || !this.state) { if (force && this.stagePlane.visible) this.state = this.state || {}; else return; }
    const now = performance.now();
    if (!force && now - this.stageDrawn < 200) return;
    this.stageDrawn = now;
    const g = this.stageCanvas.getContext('2d');
    const S = this.state || {};
    if (this.level === 'tissue') drawNephron(g, S);
    else if (this.level === 'cell') drawCell(g, S);
    else if (this.level === 'pathway') drawPathway(g, S);
    this.stageTex.needsUpdate = true;
  }

  resize() {
    const w = this.host.clientWidth, h = this.host.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.canvas.style.width = '100%'; this.canvas.style.height = '100%';
    this.camera.aspect = w / h;
    // keep the whole body in frame on narrow screens
    this.camera.fov = w / h < 0.8 ? 50 : 35;
    this.camera.updateProjectionMatrix();
  }

  frame() {
    const dt = Math.min(0.1, this.clock.getDelta());
    const t = this.clock.elapsedTime;
    this.orbit.update(dt);
    const s = this.state;
    // heartbeat (≈70 bpm) and ADH-driven pituitary pulse
    const heart = this.meshes.heart;
    const beat = REDUCED ? 1 : 1 + 0.06 * Math.max(0, Math.sin(t * 2 * Math.PI * 1.17)) ** 8;
    heart.scale.set(1 * beat, 1.15 * beat, 0.85 * beat);
    if (s) {
      const freq = 0.4 + Math.min(3, s.adh) * 0.5;
      const pulse = REDUCED ? 0.5 : 0.5 + 0.5 * Math.sin(t * 2 * Math.PI * freq);
      this.pitMat.emissiveIntensity = 0.25 + Math.min(1, s.adh / 6) * (0.6 + 0.9 * pulse);
      this.meshes.pituitary.scale.setScalar(1 + Math.min(1, s.adh / 6) * 0.5 * pulse);
    }
    if (this.molecule.visible && !REDUCED) this.molecule.rotation.y = Math.sin(t * 0.4) * 0.6;
    this.renderer.render(this.scene, this.camera);
    // anchors
    const w = this.host.clientWidth, h = this.host.clientHeight;
    for (const a of this.anchors) {
      const obj = this.meshes[a.part];
      let visible = !!obj && this.level !== 'tissue' && this.level !== 'cell' && this.level !== 'pathway' && this.level !== 'molecule';
      let o = obj; while (visible && o) { if (!o.visible) visible = false; o = o.parent; }
      if (visible) {
        const p = obj.getWorldPosition(new THREE.Vector3()).add(a.offset).project(this.camera);
        visible = p.z < 1 && Math.abs(p.x) < 1.1 && Math.abs(p.y) < 1.1;
        if (visible) a.el.style.transform = `translate(${((p.x + 1) / 2) * w}px, ${((1 - p.y) / 2) * h}px) translate(-50%, -100%)`;
      }
      a.el.hidden = !visible;
    }
    this.frames++;
    const now = performance.now();
    if (now - this.fpsT > 500) { this.fps = (this.frames * 1000) / (now - this.fpsT); this.frames = 0; this.fpsT = now; this.onFps?.(this.fps); }
  }
}
