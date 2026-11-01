/**
 * tests/engine_invariants.js
 * --------------------------
 * Regression test for discrete-event simulation engine invariants.
 *
 * Replays simulation events through AnimationController.advanceTo() across
 * sessions x scenarios x seeds x lane configurations and asserts at every timestamp:
 *   1. Every vehicle in state 'serving' occupies exactly one lane (karcisLanes,
 *      rfidLanes, or dynLaneVeh).
 *   2. No lane holds a vehicle that is not 'serving'.
 *
 * Guards against the loop not advancing past t=3700.
 * Exits with a non-zero code on failure and prints the first failing case.
 *
 * Usage:
 *   node tests/engine_invariants.js
 *   node tests/engine_invariants.js --simulate-bug  (demonstrates failure on old typePriority sort)
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const simulateBug = process.argv.includes('--simulate-bug');

// Locate HTML source (prefer animation.html, fallback to animation_template.html + anim_data.json)
const repoRoot = path.resolve(__dirname, '..');
const htmlPath = path.join(repoRoot, 'animation.html');
const templatePath = path.join(repoRoot, 'animation_template.html');
const jsonPath = path.join(repoRoot, 'anim_data.json');

let htmlContent = '';
if (fs.existsSync(htmlPath)) {
    htmlContent = fs.readFileSync(htmlPath, 'utf-8');
} else if (fs.existsSync(templatePath) && fs.existsSync(jsonPath)) {
    const template = fs.readFileSync(templatePath, 'utf-8');
    const animJson = fs.readFileSync(jsonPath, 'utf-8');
    htmlContent = template.replace('/*__ANIM_DATA__*/null', animJson).replace('/*__LOGO_BASE64__*/', '');
} else {
    console.error('ERROR: Neither animation.html nor animation_template.html found.');
    process.exit(1);
}

const scriptMatch = htmlContent.match(/<script>([\s\S]*?)<\/script>/);
if (!scriptMatch) {
    console.error('ERROR: No <script> tag found in HTML.');
    process.exit(1);
}

// Minimal DOM & Web API sandbox for Node environment
const sandbox = {
    console,
    Math,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    requestAnimationFrame: () => 0,
    cancelAnimationFrame: () => {},
    document: {
        addEventListener: () => {},
        getElementById: () => ({
            style: {},
            classList: { add() {}, remove() {}, contains() { return false; } },
            addEventListener: () => {},
            value: '0',
            textContent: ''
        }),
        querySelectorAll: () => []
    },
    window: {
        location: { search: '' },
        addEventListener: () => {}
    }
};
sandbox.globalThis = sandbox;

vm.createContext(sandbox);

// Append exports hook
const harnessCode = scriptMatch[1] + `
;globalThis.__ENGINE_TEST_EXPORTS__ = {
    ANIM_DATA,
    runSimulation,
    AnimationController,
    getScenarioConfig,
    runReplications,
    summarize,
    tCritical
};
`;

vm.runInContext(harnessCode, sandbox);

const {
    ANIM_DATA,
    runSimulation,
    AnimationController,
    getScenarioConfig
} = sandbox.__ENGINE_TEST_EXPORTS__;

if (!ANIM_DATA || !runSimulation || !AnimationController) {
    console.error('ERROR: Failed to export simulation engine symbols.');
    process.exit(1);
}

// Test matrix
const SESSIONS = ANIM_DATA.sessions; // Test all available sessions
const SCENARIOS = ['as_is', 'shift_50', 'full_rfid', 'full_karcis'];
const SEEDS = [42, 101, 777];
const LANE_CONFIGS = [
    { kLanes: 2, rLanes: 1, dynamicLanes: true, label: 'K2_R1_dynON' },
    { kLanes: 2, rLanes: 1, dynamicLanes: false, label: 'K2_R1_dynOFF' },
    { kLanes: 1, rLanes: 1, dynamicLanes: true, label: 'K1_R1_dynON' },
    { kLanes: 3, rLanes: 2, dynamicLanes: false, label: 'K3_R2_dynOFF' }
];

const stubCanvas = {
    getContext: () => ({})
};

console.log('============================================================');
console.log('  ENGINE INVARIANTS REGRESSION TEST');
console.log('============================================================');
if (simulateBug) {
    console.log('  [MODE: SIMULATE BUG with old typePriority event-log sort]');
}

let totalConfigurations = 0;
let totalTimestampsChecked = 0;

for (const session of SESSIONS) {
    for (const scenario of SCENARIOS) {
        for (const laneCfg of LANE_CONFIGS) {
            for (const seed of SEEDS) {
                totalConfigurations++;

                const cfg = getScenarioConfig(session, scenario, laneCfg.kLanes, laneCfg.rLanes);
                const sim = runSimulation({
                    lamK: cfg.lamK,
                    lamR: cfg.lamR,
                    kLanes: cfg.kLanes,
                    rLanes: cfg.rLanes,
                    serviceTimes: ANIM_DATA.service_times,
                    simDuration: 3600,
                    dynamicLanes: laneCfg.dynamicLanes,
                    dynThreshold: 20,
                    dynRevert: 5,
                    dynCooldown: 60,
                    seed
                });

                if (simulateBug) {
                    // Reintroduce the old typePriority sort that broke causal tie-breaking
                    const typePriority = { arrive: 1, startService: 2, endService: 3, laneShift: 4, laneRevert: 5 };
                    sim.events.sort((a, b) => (a.time - b.time) || ((typePriority[a.type] || 99) - (typePriority[b.type] || 99)));
                }

                const ctrl = new AnimationController(stubCanvas, 'a');
                ctrl.load(sim, cfg.kLanes, cfg.rLanes);

                // Replay events through unique timestamps
                const eventTimes = Array.from(new Set(sim.events.map(e => e.time))).sort((a, b) => a - b);

                for (let idx = 0; idx < eventTimes.length; idx++) {
                    const t = eventTimes[idx];

                    // Guard against loop not advancing past 3700 (advanceTo caps at 3700)
                    if (t > 3700) break;

                    ctrl.advanceTo(t);
                    totalTimestampsChecked++;

                    // Collect vehicles in lanes
                    const occupiedLanes = [];
                    for (let l = 0; l < ctrl.karcisLanes.length; l++) {
                        const vehId = ctrl.karcisLanes[l];
                        if (vehId) occupiedLanes.push({ laneType: 'karcis', laneIndex: l, vehId });
                    }
                    for (let l = 0; l < ctrl.rfidLanes.length; l++) {
                        const vehId = ctrl.rfidLanes[l];
                        if (vehId) occupiedLanes.push({ laneType: 'rfid', laneIndex: l, vehId });
                    }
                    if (ctrl.dynLaneVeh) {
                        occupiedLanes.push({ laneType: 'dyn', laneIndex: 0, vehId: ctrl.dynLaneVeh });
                    }

                    // Collect vehicles in 'serving' state
                    const servingVehIds = [];
                    for (const vehId in ctrl.vehicles) {
                        const v = ctrl.vehicles[vehId];
                        if (v.state === 'serving') {
                            servingVehIds.push(vehId);
                        }
                    }

                    // Invariant 1: No lane holds a vehicle that is not 'serving'
                    for (const item of occupiedLanes) {
                        const v = ctrl.vehicles[item.vehId];
                        if (!v || v.state !== 'serving') {
                            console.error('\nFAIL: Invariant violation (lane holds non-serving vehicle)!');
                            console.error(`  Session       : ${session.label}`);
                            console.error(`  Scenario      : ${scenario}`);
                            console.error(`  Lanes         : ${laneCfg.label} (K=${cfg.kLanes}, R=${cfg.rLanes})`);
                            console.error(`  Seed          : ${seed}`);
                            console.error(`  Sim Time (t)  : ${t.toFixed(4)}s`);
                            console.error(`  Lane          : ${item.laneType}[${item.laneIndex}]`);
                            console.error(`  Vehicle ID    : ${item.vehId}`);
                            console.error(`  Vehicle State : ${v ? v.state : 'null'}`);
                            process.exit(1);
                        }
                    }

                    // Invariant 2: Every vehicle in state 'serving' occupies exactly one lane
                    for (const vehId of servingVehIds) {
                        const count = occupiedLanes.filter(l => l.vehId === vehId).length;
                        if (count !== 1) {
                            console.error('\nFAIL: Invariant violation (serving vehicle not in exactly 1 lane)!');
                            console.error(`  Session       : ${session.label}`);
                            console.error(`  Scenario      : ${scenario}`);
                            console.error(`  Lanes         : ${laneCfg.label} (K=${cfg.kLanes}, R=${cfg.rLanes})`);
                            console.error(`  Seed          : ${seed}`);
                            console.error(`  Sim Time (t)  : ${t.toFixed(4)}s`);
                            console.error(`  Vehicle ID    : ${vehId} in state 'serving' occupies ${count} lanes.`);
                            process.exit(1);
                        }
                    }

                    // Invariant 3: Total counts must match
                    if (occupiedLanes.length !== servingVehIds.length) {
                        console.error('\nFAIL: Invariant violation (serving vehicle count != occupied lanes count)!');
                        console.error(`  Session       : ${session.label}`);
                        console.error(`  Scenario      : ${scenario}`);
                        console.error(`  Lanes         : ${laneCfg.label}`);
                        console.error(`  Seed          : ${seed}`);
                        console.error(`  Sim Time (t)  : ${t.toFixed(4)}s`);
                        console.error(`  Serving count : ${servingVehIds.length}, Occupied count: ${occupiedLanes.length}`);
                        process.exit(1);
                    }
                }
            }
        }
    }
}

console.log(`  Configurations tested : ${totalConfigurations}`);
console.log(`  Timestamps verified   : ${totalTimestampsChecked.toLocaleString()}`);
console.log('  Invariants verified   :');
console.log('    [OK] Every vehicle in state "serving" occupies exactly one lane');
console.log('    [OK] No lane holds a vehicle that is not "serving"');
console.log('    [OK] Loop bounded and guarded at t=3700');
console.log('============================================================');
console.log('  STATUS: ALL INVARIANTS PASSED (exit code 0)');
console.log('============================================================');
process.exit(0);
