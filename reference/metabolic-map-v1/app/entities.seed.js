// app/entities.seed.js — V1 seed entity list for the left rail.
// Temporary: the Literature Curator's kb/entities.json replaces this when present
// (see loadEntities() in ui.js). Shape follows spec §6 (subset of fields).
// Scales: 0 organism, 1 organ system, 2 organ, 3 tissue, 4 cell, 5 organelle,
// 6 pathway/reaction, 7 molecule.
// `mesh` names the viewer object for an entity (viewer.js). `system` groups it in
// the rail and drives the system toggles. Summaries are textbook-level and cite
// ev:guyton-hall-2021 (resolved from engine/evidence.engine.json) until the
// Curator supplies per-entity evidence.

const GH = ['ev:guyton-hall-2021'];

export const SYSTEMS = [
  { id: 'cardiovascular', name: 'Cardiovascular' },
  { id: 'renal', name: 'Renal and urinary' },
  { id: 'endocrine', name: 'Endocrine' },
  { id: 'nervous', name: 'Nervous' },
  { id: 'gi', name: 'Digestive' },
  { id: 'muscle', name: 'Skeletal muscle' },
  { id: 'skin', name: 'Skin' },
];

export const ENTITIES = [
  { id: 'organism:body', type: 'organ', name: 'Whole body', scale: 0, system: null, mesh: null,
    summary: '70 kg reference adult. Total body water about 42 L: about 14 L extracellular (plasma plus interstitial fluid) and 28 L intracellular.',
    quantities: ['V_ecf', 'V_icf'], evidence: GH },

  // Cardiovascular
  { id: 'organ:heart', type: 'organ', name: 'Heart', scale: 2, system: 'cardiovascular', mesh: 'heart', parent: 'organism:body',
    summary: 'Pumps about 5 L/min at rest. In this model mean arterial pressure follows ECF volume; the badge on the heart shows the simulated MAP.',
    quantities: ['MAP'], evidence: GH },
  { id: 'organ:aorta', type: 'organ', name: 'Aorta', scale: 2, system: 'cardiovascular', mesh: 'aorta', parent: 'organism:body',
    summary: 'Main artery leaving the left ventricle. Its pressure drives glomerular filtration in the kidneys.',
    quantities: ['MAP'], evidence: GH },
  { id: 'organ:vena-cava', type: 'organ', name: 'Venae cavae', scale: 2, system: 'cardiovascular', mesh: 'venaCava', parent: 'organism:body',
    summary: 'Large veins returning blood to the right atrium. Atrial stretch releases ANP when ECF volume expands.',
    quantities: ['V_ecf'], evidence: GH },

  // Renal / urinary
  { id: 'organ:kidney', type: 'organ', name: 'Kidneys', scale: 2, system: 'renal', mesh: 'kidney', parent: 'organism:body',
    summary: 'Filter about 180 L of plasma a day (GFR about 125 mL/min) and return over 99% of filtered water and sodium. They set urine volume and concentration under ADH, aldosterone and ANP.',
    quantities: ['GFR', 'urine_flow', 'U_osm', 'strain_index'], evidence: GH },
  { id: 'tissue:nephron', type: 'tissue', name: 'Nephron', scale: 3, system: 'renal', mesh: 'schem:tissue', parent: 'organ:kidney',
    summary: 'Functional unit of the kidney, about one million per kidney: glomerulus, proximal tubule, loop of Henle, distal tubule and collecting duct.',
    quantities: ['GFR', 'FE_Na', 'U_osm'], evidence: GH },
  { id: 'cell:principal', type: 'cell', name: 'Principal cell (collecting duct)', scale: 4, system: 'renal', mesh: 'schem:cell', parent: 'tissue:nephron',
    summary: 'Collecting-duct cell that reabsorbs sodium through ENaC and water through AQP2 channels. ADH inserts AQP2 into the apical membrane.',
    quantities: ['ADH', 'U_osm'], evidence: GH },
  { id: 'pathway:adh-aqp2', type: 'pathway', name: 'ADH → V2R → cAMP → AQP2', scale: 6, system: 'renal', mesh: 'schem:pathway', parent: 'cell:principal',
    summary: 'ADH binds the V2 receptor, Gs activates adenylyl cyclase, cAMP activates PKA, and PKA drives AQP2 vesicles to the apical membrane so water can be reabsorbed.',
    quantities: ['ADH', 'U_osm'], evidence: GH },
  { id: 'molecule:avp', type: 'hormone', name: 'Vasopressin (ADH)', scale: 7, system: 'renal', mesh: 'schem:molecule', parent: 'pathway:adh-aqp2',
    summary: 'Nine-residue peptide hormone (CYFQNCPRG) with a disulfide bond between the two cysteines. Released from the posterior pituitary as plasma osmolality rises.',
    quantities: ['ADH'], evidence: GH },
  { id: 'organ:ureters', type: 'organ', name: 'Ureters', scale: 2, system: 'renal', mesh: 'ureter', parent: 'organism:body',
    summary: 'Carry urine from each kidney to the bladder.',
    quantities: ['urine_flow'], evidence: GH },
  { id: 'organ:bladder', type: 'organ', name: 'Bladder', scale: 2, system: 'renal', mesh: 'bladder', parent: 'organism:body',
    summary: 'Stores urine. The viewer grows it with urine made since the last void and empties it at 0.4 L. That void rule is a display choice, not part of the model.',
    quantities: ['urine_flow'], evidence: GH },

  // Endocrine
  { id: 'organ:pituitary', type: 'organ', name: 'Hypothalamus and pituitary', scale: 2, system: 'endocrine', mesh: 'pituitary', parent: 'organism:body',
    summary: 'Hypothalamic osmoreceptors sense plasma osmolality; the posterior pituitary releases ADH. The marker pulses faster as simulated ADH rises.',
    quantities: ['ADH', 'Thirst'], evidence: GH },
  { id: 'organ:adrenal', type: 'organ', name: 'Adrenal glands', scale: 2, system: 'endocrine', mesh: 'adrenal', parent: 'organism:body',
    summary: 'Sit on top of each kidney. The cortex secretes aldosterone, which raises sodium reabsorption when ECF volume falls.',
    quantities: ['Aldo'], evidence: GH },

  // Nervous
  { id: 'organ:brain', type: 'organ', name: 'Brain', scale: 2, system: 'nervous', mesh: 'brain', parent: 'organism:body',
    summary: 'Generates thirst in response to rising osmolality or falling volume. Thirst is reported but does not trigger drinking in V1.',
    quantities: ['Thirst'], evidence: GH },

  // GI
  { id: 'organ:stomach', type: 'organ', name: 'Stomach', scale: 2, system: 'gi', mesh: 'stomach', parent: 'organism:body',
    summary: 'Receives drinks and food. In the model, ingested water and salt sit in a single gut pool before absorption.',
    quantities: ['V_gut_water', 'Na_gut'], evidence: GH },
  { id: 'organ:intestine', type: 'organ', name: 'Intestines', scale: 2, system: 'gi', mesh: 'intestine', parent: 'organism:body',
    summary: 'Absorb most ingested water and sodium. Plain water is absorbed with a half-time of minutes.',
    quantities: ['V_gut_water', 'Na_gut'], evidence: GH },

  // Muscle and skin
  { id: 'tissue:muscle', type: 'tissue', name: 'Skeletal muscle', scale: 3, system: 'muscle', mesh: 'muscle', parent: 'organism:body',
    summary: 'Holds most intracellular fluid. Shown here as a stand-in for the ICF compartment; it tints as ICF volume changes.',
    quantities: ['V_icf'], evidence: GH },
  { id: 'organ:skin', type: 'organ', name: 'Skin and interstitium', scale: 2, system: 'skin', mesh: 'skin', parent: 'organism:body',
    summary: 'Stands in for the extracellular compartment; it tints as ECF volume changes. Skin sodium storage is not modelled in V1.',
    quantities: ['V_ecf'], evidence: GH },
];

/** Scale ladder rungs (spec §7). */
export const LADDER = [
  { id: 'body', name: 'Body', scale: 0 },
  { id: 'organ', name: 'Organ', scale: 2 },
  { id: 'tissue', name: 'Tissue', scale: 3 },
  { id: 'cell', name: 'Cell', scale: 4 },
  { id: 'pathway', name: 'Pathway', scale: 6 },
  { id: 'molecule', name: 'Molecule', scale: 7 },
];
