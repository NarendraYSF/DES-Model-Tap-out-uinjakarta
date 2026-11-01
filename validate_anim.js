/**
 * validate_anim.js
 * ----------------
 * Node.js validation script for the DES animation engine.
 *
 * Part 1: With Dynamic Lanes OFF, verifies JS engine against Python v3
 *         (results_v3/sim_results_v3.csv) across 6 sessions. (12 checks)
 * Part 2: With Dynamic Lanes ON, verifies JS engine against Python v4
 *         (results_v4/sim_results_v4.csv) across 6 sessions. (12 checks)
 *
 * Acceptance criteria:
 *   JS mean wait is within Python's 95% CI, OR within 0.5s absolute
 *   tolerance for near-zero waits, OR within 20% relative tolerance.
 *
 * Usage:
 *   node validate_anim.js
 */

const fs = require('fs');
const path = require('path');

// ============================================================================
// 1. PRNG
// ============================================================================

function mulberry32(seed) {
    return function() {
        seed |= 0;
        seed = seed + 0x6D2B79F5 | 0;
        let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

// ============================================================================
// 2. MINHEAP
// ============================================================================

class MinHeap {
    constructor() { this.heap = []; }
    get size() { return this.heap.length; }

    push(item) {
        this.heap.push(item);
        this._bubbleUp(this.heap.length - 1);
    }

    pop() {
        if (this.heap.length === 0) return undefined;
        const top = this.heap[0];
        const last = this.heap.pop();
        if (this.heap.length > 0) {
            this.heap[0] = last;
            this._sinkDown(0);
        }
        return top;
    }

    _bubbleUp(i) {
        while (i > 0) {
            const parent = (i - 1) >> 1;
            if (this.heap[i].time < this.heap[parent].time) {
                [this.heap[i], this.heap[parent]] = [this.heap[parent], this.heap[i]];
                i = parent;
            } else break;
        }
    }

    _sinkDown(i) {
        const n = this.heap.length;
        while (true) {
            let smallest = i;
            const left = 2 * i + 1;
            const right = 2 * i + 2;
            if (left < n && this.heap[left].time < this.heap[smallest].time) smallest = left;
            if (right < n && this.heap[right].time < this.heap[smallest].time) smallest = right;
            if (smallest !== i) {
                [this.heap[i], this.heap[smallest]] = [this.heap[smallest], this.heap[i]];
                i = smallest;
            } else break;
        }
    }
}

// ============================================================================
// 3. DES ENGINE (headless - supporting static and dynamic lanes)
// ============================================================================

function runSimulationHeadless(config) {
    const { lamK, lamR, kLanes, rLanes, serviceTimes, seed, simDuration,
            dynamicLanes = false, dynThreshold = 20, dynRevert = 5, dynCooldown = 60 } = config;
    const duration = simDuration || 3600;

    const rngArrK = mulberry32(seed * 4 + 0);
    const rngSvcK = mulberry32(seed * 4 + 1);
    const rngArrR = mulberry32(seed * 4 + 2);
    const rngSvcR = mulberry32(seed * 4 + 3);

    const stK = serviceTimes.karcis;
    const stR = serviceTimes.rfid;

    const useDyn = dynamicLanes && rLanes > 0 && kLanes > 0;
    const permRLanes = useDyn ? rLanes - 1 : rLanes;

    const karcisLanesArr = new Array(kLanes).fill(null);
    const rfidLanesArr = new Array(permRLanes).fill(null);
    const karcisQueue = [];
    const rfidQueue = [];

    // Dynamic lane state
    let dynRole = 'R';
    let dynLaneVeh = null;
    let pendingShift = false;
    let pendingRevert = false;
    let lastSwitchTime = -dynCooldown;
    let totalTimeK = 0.0;
    let totalTimeR = 0.0;
    let roleStartTime = 0.0;
    let switchCount = 0;

    // Track wait times and service times for stats
    const kWaits = [];
    const rWaits = [];
    let kServed = 0, rServed = 0;
    let kTotalService = 0, rTotalService = 0;

    // Queue snapshots for avg/max queue
    const kQueueSnaps = [];
    const rQueueSnaps = [];

    const pq = new MinHeap();

    // Vehicle service time storage (id -> serviceTime)
    const vehicleSvc = {};
    const vehicleArrival = {};

    let nextIdK = 0, nextIdR = 0;

    if (lamK > 0 && kLanes > 0) {
        const iat = -Math.log(1 - rngArrK()) / lamK;
        pq.push({ type: 0, time: iat }); // 0 = ARRIVAL_K
    }
    if (lamR > 0 && rLanes > 0) {
        const iat = -Math.log(1 - rngArrR()) / lamR;
        pq.push({ type: 1, time: iat }); // 1 = ARRIVAL_R
    }
    pq.push({ type: 3, time: 1.0 }); // 3 = MONITOR

    function findFreeLane(lanes) {
        for (let i = 0; i < lanes.length; i++) {
            if (lanes[i] === null) return i;
        }
        return -1;
    }

    function checkShiftTrigger(t) {
        if (!useDyn) return;
        if (dynRole === 'R' && !pendingShift) {
            if (karcisQueue.length > dynThreshold) {
                if (dynLaneVeh === null) {
                    doShift(t);
                } else {
                    pendingShift = true;
                }
            }
        }
    }

    function doShift(t) {
        totalTimeR += Math.max(0, t - roleStartTime);
        roleStartTime = t;
        dynRole = 'K';
        pendingShift = false;
        lastSwitchTime = t;
        switchCount++;
        if (karcisQueue.length > 0) {
            const nextVehId = karcisQueue.shift();
            dynLaneVeh = nextVehId;
            const nextSvc = vehicleSvc[nextVehId];
            pq.push({ type: 2, time: t + nextSvc, id: nextVehId, gate: 0, lane: 999 });
        }
    }

    function checkRevertTrigger(t) {
        if (!useDyn) return;
        if (dynRole === 'K' && !pendingRevert) {
            if (karcisQueue.length <= dynRevert && (t - lastSwitchTime >= dynCooldown)) {
                if (dynLaneVeh === null) {
                    doRevert(t);
                } else {
                    pendingRevert = true;
                }
            }
        }
    }

    function doRevert(t) {
        totalTimeK += Math.max(0, t - roleStartTime);
        roleStartTime = t;
        dynRole = 'R';
        pendingRevert = false;
        lastSwitchTime = t;
        switchCount++;
        if (rfidQueue.length > 0) {
            const nextVehId = rfidQueue.shift();
            dynLaneVeh = nextVehId;
            const nextSvc = vehicleSvc[nextVehId];
            pq.push({ type: 2, time: t + nextSvc, id: nextVehId, gate: 1, lane: 999 });
        }
    }

    while (pq.size > 0) {
        const evt = pq.pop();
        const t = evt.time;

        if (evt.type === 3) { // MONITOR
            kQueueSnaps.push(karcisQueue.length);
            rQueueSnaps.push(rfidQueue.length);
            if (useDyn) {
                checkShiftTrigger(t);
                checkRevertTrigger(t);
            }
            if (t + 1.0 <= duration + 60) {
                pq.push({ type: 3, time: t + 1.0 });
            }
            continue;
        }

        if (evt.type === 0) { // ARRIVAL_K
            if (t > duration) continue;
            const vehId = nextIdK++;
            const svcTime = stK[Math.floor(rngSvcK() * stK.length)];
            vehicleSvc[vehId] = svcTime;
            vehicleArrival[vehId] = t;

            const freeLane = findFreeLane(karcisLanesArr);
            if (freeLane >= 0) {
                karcisLanesArr[freeLane] = vehId;
                vehicleArrival[vehId] = t;
                pq.push({ type: 2, time: t + svcTime, id: vehId, gate: 0, lane: freeLane });
            } else if (useDyn && dynRole === 'K' && dynLaneVeh === null) {
                dynLaneVeh = vehId;
                vehicleArrival[vehId] = t;
                pq.push({ type: 2, time: t + svcTime, id: vehId, gate: 0, lane: 999 });
            } else {
                karcisQueue.push(vehId);
                checkShiftTrigger(t);
            }

            const iat = -Math.log(1 - rngArrK()) / lamK;
            pq.push({ type: 0, time: t + iat });
            continue;
        }

        if (evt.type === 1) { // ARRIVAL_R
            if (t > duration) continue;
            const vehId = 10000000 + nextIdR++;
            const svcTime = stR[Math.floor(rngSvcR() * stR.length)];
            vehicleSvc[vehId] = svcTime;
            vehicleArrival[vehId] = t;

            const freeLane = findFreeLane(rfidLanesArr);
            if (freeLane >= 0) {
                rfidLanesArr[freeLane] = vehId;
                vehicleArrival[vehId] = t;
                pq.push({ type: 2, time: t + svcTime, id: vehId, gate: 1, lane: freeLane });
            } else if (useDyn && dynRole === 'R' && dynLaneVeh === null) {
                dynLaneVeh = vehId;
                vehicleArrival[vehId] = t;
                pq.push({ type: 2, time: t + svcTime, id: vehId, gate: 1, lane: 999 });
            } else {
                rfidQueue.push(vehId);
            }

            const iat = -Math.log(1 - rngArrR()) / lamR;
            pq.push({ type: 1, time: t + iat });
            continue;
        }

        if (evt.type === 2) { // SERVICE_END
            const vehId = evt.id;
            const gate = evt.gate;
            const lane = evt.lane;
            const svcTime = vehicleSvc[vehId];

            if (t <= duration) {
                const waitTime = vehicleArrival[vehId] !== undefined
                    ? (t - svcTime) - vehicleArrival[vehId]
                    : 0;
                if (gate === 0) {
                    kWaits.push(Math.max(0, waitTime));
                    kServed++;
                    kTotalService += svcTime;
                } else {
                    rWaits.push(Math.max(0, waitTime));
                    rServed++;
                    rTotalService += svcTime;
                }
            }

            if (lane === 999) {
                // Dynamic lane finished service
                dynLaneVeh = null;
                if (gate === 0) {
                    if (pendingRevert) {
                        if (karcisQueue.length <= dynRevert) {
                            doRevert(t);
                        } else {
                            pendingRevert = false;
                            if (karcisQueue.length > 0) {
                                const nextVehId = karcisQueue.shift();
                                dynLaneVeh = nextVehId;
                                const nextSvc = vehicleSvc[nextVehId];
                                pq.push({ type: 2, time: t + nextSvc, id: nextVehId, gate: 0, lane: 999 });
                            }
                        }
                    } else {
                        checkRevertTrigger(t);
                        if (dynRole === 'K' && dynLaneVeh === null && karcisQueue.length > 0) {
                            const nextVehId = karcisQueue.shift();
                            dynLaneVeh = nextVehId;
                            const nextSvc = vehicleSvc[nextVehId];
                            pq.push({ type: 2, time: t + nextSvc, id: nextVehId, gate: 0, lane: 999 });
                        }
                    }
                } else {
                    if (pendingShift) {
                        if (karcisQueue.length > dynRevert) {
                            doShift(t);
                        } else {
                            pendingShift = false;
                            if (rfidQueue.length > 0) {
                                const nextVehId = rfidQueue.shift();
                                dynLaneVeh = nextVehId;
                                const nextSvc = vehicleSvc[nextVehId];
                                pq.push({ type: 2, time: t + nextSvc, id: nextVehId, gate: 1, lane: 999 });
                            }
                        }
                    } else {
                        checkShiftTrigger(t);
                        if (dynRole === 'R' && dynLaneVeh === null && rfidQueue.length > 0) {
                            const nextVehId = rfidQueue.shift();
                            dynLaneVeh = nextVehId;
                            const nextSvc = vehicleSvc[nextVehId];
                            pq.push({ type: 2, time: t + nextSvc, id: nextVehId, gate: 1, lane: 999 });
                        }
                    }
                }
            } else {
                const lanes = gate === 0 ? karcisLanesArr : rfidLanesArr;
                const queue = gate === 0 ? karcisQueue : rfidQueue;
                lanes[lane] = null;

                if (queue.length > 0) {
                    const nextVehId = queue.shift();
                    lanes[lane] = nextVehId;
                    const nextSvc = vehicleSvc[nextVehId];
                    pq.push({ type: 2, time: t + nextSvc, id: nextVehId, gate: gate, lane: lane });
                }

                if (gate === 0 && useDyn) {
                    checkRevertTrigger(t);
                }
            }
            continue;
        }
    }

    if (useDyn) {
        if (dynRole === 'R') {
            totalTimeR += Math.max(0, duration - roleStartTime);
        } else {
            totalTimeK += Math.max(0, duration - roleStartTime);
        }
    }

    const effKCap = useDyn ? (kLanes * duration + totalTimeK) : (kLanes * duration);
    const effRCap = useDyn ? Math.max(0, (rLanes - 1) * duration + totalTimeR) : (rLanes * duration);

    function calcGateStats(waits, qSnaps, effCap, served, totalService) {
        if (waits.length === 0) {
            return { meanWait: 0, maxWait: 0, p95Wait: 0, avgQueue: 0, maxQueue: 0,
                     utilisation: 0, throughput: served, backlog: 0 };
        }
        const sorted = waits.slice().sort((a, b) => a - b);
        const mean = waits.reduce((s, v) => s + v, 0) / waits.length;
        const max = sorted[sorted.length - 1];
        const p95idx = Math.min(Math.floor(sorted.length * 0.95), sorted.length - 1);
        const p95 = sorted[p95idx];

        let avgQ = 0, maxQ = 0;
        if (qSnaps.length > 0) {
            let sum = 0;
            for (let i = 0; i < qSnaps.length; i++) {
                sum += qSnaps[i];
                if (qSnaps[i] > maxQ) maxQ = qSnaps[i];
            }
            avgQ = sum / qSnaps.length;
        }

        const util = effCap > 0 ? (totalService / effCap) : 0;

        return { meanWait: mean, maxWait: max, p95Wait: p95, avgQueue: avgQ, maxQueue: maxQ,
                 utilisation: util, throughput: served, backlog: 0 };
    }

    return {
        karcis: { ...calcGateStats(kWaits, kQueueSnaps, effKCap, kServed, kTotalService),
                  backlog: karcisQueue.length, switchCount, totalTimeK, totalTimeR, effCap: effKCap },
        rfid:   { ...calcGateStats(rWaits, rQueueSnaps, effRCap, rServed, rTotalService),
                  backlog: rfidQueue.length, switchCount, totalTimeK, totalTimeR, effCap: effRCap }
    };
}

// ============================================================================
// 4. SCENARIO CONFIG
// ============================================================================

function getScenarioConfig(session, scenario, baseLanesK, baseLanesR) {
    const lamK0 = session.karcis.lambda;
    const lamR0 = session.rfid.lambda;
    const totalLam = lamK0 + lamR0;
    const totalLanes = baseLanesK + baseLanesR;

    switch (scenario) {
        case 'as_is':       return { lamK: lamK0,       lamR: lamR0,               kLanes: baseLanesK, rLanes: baseLanesR };
        case 'shift_25':    return { lamK: lamK0 * 0.75, lamR: lamR0 + lamK0 * 0.25, kLanes: baseLanesK, rLanes: baseLanesR };
        case 'shift_50':    return { lamK: lamK0 * 0.50, lamR: lamR0 + lamK0 * 0.50, kLanes: baseLanesK, rLanes: baseLanesR };
        case 'shift_75':    return { lamK: lamK0 * 0.25, lamR: lamR0 + lamK0 * 0.75, kLanes: baseLanesK, rLanes: baseLanesR };
        case 'shift_100':   return { lamK: 0,            lamR: lamR0 + lamK0,         kLanes: baseLanesK, rLanes: baseLanesR };
        case 'full_rfid':   return { lamK: 0,            lamR: totalLam,              kLanes: 0,          rLanes: totalLanes };
        case 'full_karcis':  return { lamK: totalLam,     lamR: 0,                     kLanes: totalLanes, rLanes: 0 };
        default:            return { lamK: lamK0,       lamR: lamR0,               kLanes: baseLanesK, rLanes: baseLanesR };
    }
}

// ============================================================================
// 5. VALIDATION RUNNER
// ============================================================================

function runValidationSuite(title, csvFilePath, animData, dynamicLanes) {
    const csvRaw = fs.readFileSync(csvFilePath, 'utf-8').replace(/\r/g, '');
    const csvLines = csvRaw.split('\n').filter(l => l.trim());
    const header = csvLines[0].split(',');

    const colIdx = {};
    header.forEach((h, i) => colIdx[h.trim()] = i);

    // Filter to as_is rows only
    const asIsRows = csvLines.slice(1)
        .map(l => l.split(','))
        .filter(r => r[colIdx['scenario']].trim() === 'as_is');

    const N_REPS = 200;
    const BASE_SEED = 42;
    const ABS_TOL = 0.5;  // Absolute tolerance for near-zero waits
    const REL_TOL = 0.20; // 20% relative tolerance

    console.log('============================================================');
    console.log(`  ${title}`);
    console.log('============================================================');
    console.log(`  Reference CSV : ${csvFilePath}`);
    console.log(`  Dynamic Lanes : ${dynamicLanes ? 'ON' : 'OFF'}`);
    console.log(`  Replications  : ${N_REPS}`);
    console.log(`  Base seed     : ${BASE_SEED}`);
    console.log(`  Tolerance     : within Python 95% CI, OR within ${ABS_TOL}s abs, OR within ${REL_TOL*100}% rel`);
    console.log('');

    let allPass = true;
    let totalChecks = 0;
    let passedChecks = 0;

    for (const session of animData.sessions) {
        const cfg = getScenarioConfig(session, 'as_is', animData.defaults.karcis_lanes, animData.defaults.rfid_lanes);

        const kMeanWaits = [];
        const rMeanWaits = [];
        const kUtils = [];
        const rUtils = [];
        const kThroughputs = [];
        const rThroughputs = [];
        const kSwitches = [];

        for (let rep = 0; rep < N_REPS; rep++) {
            const result = runSimulationHeadless({
                lamK: cfg.lamK,
                lamR: cfg.lamR,
                kLanes: cfg.kLanes,
                rLanes: cfg.rLanes,
                serviceTimes: animData.service_times,
                seed: BASE_SEED + rep,
                simDuration: 3600,
                dynamicLanes: dynamicLanes,
                dynThreshold: 20,
                dynRevert: 5,
                dynCooldown: 60
            });
            kMeanWaits.push(result.karcis.meanWait);
            rMeanWaits.push(result.rfid.meanWait);
            kUtils.push(result.karcis.utilisation);
            rUtils.push(result.rfid.utilisation);
            kThroughputs.push(result.karcis.throughput);
            rThroughputs.push(result.rfid.throughput);
            kSwitches.push(result.karcis.switchCount || 0);
        }

        const avg = arr => arr.reduce((a, b) => a + b, 0) / arr.length;

        const jsMeanK = avg(kMeanWaits);
        const jsMeanR = avg(rMeanWaits);
        const jsUtilK = avg(kUtils);
        const jsUtilR = avg(rUtils);
        const jsThrK = avg(kThroughputs);
        const jsThrR = avg(rThroughputs);
        const jsSw = avg(kSwitches);

        // Find matching Python row
        const sesi = session.sesi;
        const hari = session.hari;
        const pyRow = asIsRows.find(r =>
            r[colIdx['hari']].trim() === hari && r[colIdx['sesi']].trim() === sesi
        );

        if (!pyRow) {
            console.log(`  ${session.label}: Python row not found - SKIP`);
            continue;
        }

        const pyMeanK   = parseFloat(pyRow[colIdx['K_mean_wait']]);
        const pyKlo     = parseFloat(pyRow[colIdx['K_mean_wait_CI_lo']]);
        const pyKhi     = parseFloat(pyRow[colIdx['K_mean_wait_CI_hi']]);
        const pyUtilK   = parseFloat(pyRow[colIdx['K_utilisation']]);
        const pyThrK    = parseFloat(pyRow[colIdx['K_throughput']]);

        const pyMeanR   = parseFloat(pyRow[colIdx['R_mean_wait']]);
        const pyRlo     = parseFloat(pyRow[colIdx['R_mean_wait_CI_lo']]);
        const pyRhi     = parseFloat(pyRow[colIdx['R_mean_wait_CI_hi']]);
        const pyUtilR   = parseFloat(pyRow[colIdx['R_utilisation']]);
        const pyThrR    = parseFloat(pyRow[colIdx['R_throughput']]);

        // Check: within CI OR within ABS_TOL OR within REL_TOL (20%)
        const kInCI = jsMeanK >= pyKlo && jsMeanK <= pyKhi;
        const kAbsOk = Math.abs(jsMeanK - pyMeanK) <= ABS_TOL;
        const kRelOk = pyMeanK > 0 ? (Math.abs(jsMeanK - pyMeanK) / pyMeanK <= REL_TOL) : jsMeanK === 0;
        const kPass = kInCI || kAbsOk || kRelOk;

        const rInCI = jsMeanR >= pyRlo && jsMeanR <= pyRhi;
        const rAbsOk = Math.abs(jsMeanR - pyMeanR) <= ABS_TOL;
        const rRelOk = pyMeanR > 0 ? (Math.abs(jsMeanR - pyMeanR) / pyMeanR <= REL_TOL) : jsMeanR === 0;
        const rPass = rInCI || rAbsOk || rRelOk;

        totalChecks += 2;
        if (kPass) passedChecks++;
        if (rPass) passedChecks++;
        if (!kPass || !rPass) allPass = false;

        console.log(`  ${session.label}:`);
        console.log(`    Karcis: JS=${jsMeanK.toFixed(3)}  Py=${pyMeanK.toFixed(3)} [${pyKlo.toFixed(3)}, ${pyKhi.toFixed(3)}]  ${kPass ? 'OK' : 'FAIL'}${kInCI ? ' (in CI)' : kAbsOk ? ' (abs tol)' : kRelOk ? ' (rel tol)' : ''}`);
        console.log(`    RFID:   JS=${jsMeanR.toFixed(3)}  Py=${pyMeanR.toFixed(3)} [${pyRlo.toFixed(3)}, ${pyRhi.toFixed(3)}]  ${rPass ? 'OK' : 'FAIL'}${rInCI ? ' (in CI)' : rAbsOk ? ' (abs tol)' : rRelOk ? ' (rel tol)' : ''}`);
        console.log(`    K util: JS=${jsUtilK.toFixed(3)} Py=${pyUtilK.toFixed(3)}  |  R util: JS=${jsUtilR.toFixed(3)} Py=${pyUtilR.toFixed(3)}`);
        console.log(`    K thr:  JS=${jsThrK.toFixed(1)} Py=${pyThrK.toFixed(1)}  |  R thr:  JS=${jsThrR.toFixed(1)} Py=${pyThrR.toFixed(1)}` + (dynamicLanes ? `  |  Switches: JS=${jsSw.toFixed(2)}` : ''));
        console.log('');
    }

    console.log('------------------------------------------------------------');
    console.log(`  Suite result: ${passedChecks}/${totalChecks} checks passed  ${allPass ? 'ALL OK' : 'SOME FAILED'}`);
    console.log('============================================================\n');

    return { allPass, passedChecks, totalChecks };
}

// ============================================================================
// 6. MAIN
// ============================================================================

function main() {
    const scriptDir = __dirname || path.dirname(process.argv[1] || '.');

    // Load anim_data.json
    const animDataPath = path.join(scriptDir, 'anim_data.json');
    if (!fs.existsSync(animDataPath)) {
        console.error('ERROR: anim_data.json not found. Run export_anim_data.py first.');
        process.exit(1);
    }
    const animData = JSON.parse(fs.readFileSync(animDataPath, 'utf-8'));

    // Part 1: Dynamic Lanes OFF vs v3
    const csvV3Path = path.join(scriptDir, 'results_v3', 'sim_results_v3.csv');
    if (!fs.existsSync(csvV3Path)) {
        console.error('ERROR: results_v3/sim_results_v3.csv not found.');
        process.exit(1);
    }
    const suiteV3 = runValidationSuite(
        'PART 1: Dynamic Lanes OFF vs Python v3',
        csvV3Path,
        animData,
        false
    );

    // Part 2: Dynamic Lanes ON vs v4
    const csvV4Path = path.join(scriptDir, 'results_v4', 'sim_results_v4.csv');
    let suiteV4 = { allPass: true, passedChecks: 0, totalChecks: 0 };
    if (fs.existsSync(csvV4Path)) {
        suiteV4 = runValidationSuite(
            'PART 2: Dynamic Lanes ON vs Python v4',
            csvV4Path,
            animData,
            true
        );
    } else {
        console.log('NOTE: results_v4/sim_results_v4.csv not found yet. Skipping Part 2.');
    }

    const overallPass = suiteV3.allPass && suiteV4.allPass;
    const totalPassed = suiteV3.passedChecks + suiteV4.passedChecks;
    const totalExpected = suiteV3.totalChecks + suiteV4.totalChecks;

    console.log('============================================================');
    console.log(`  FINAL VALIDATION SUMMARY: ${totalPassed}/${totalExpected} checks passed`);
    console.log(`  Status: ${overallPass ? 'SUCCESS (ALL PASSED)' : 'FAILED'}`);
    console.log('============================================================');

    process.exit(overallPass ? 0 : 1);
}

if (require.main === module) {
    main();
}

module.exports = { runSimulationHeadless, mulberry32, MinHeap };
