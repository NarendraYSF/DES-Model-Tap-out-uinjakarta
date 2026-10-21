# Exit-Gate Tap-Out Discrete Event Simulation (v2)

## Overview

A SimPy-based Discrete Event Simulation (DES) modelling vehicle departures
through parking-lot exit gates. The simulation uses observed service-time data
from **"Pengumpulan Data Pemodelan Tap Out.xlsx"** (sheets *Selasa* and *Rabu*,
three one-hour sessions each).

Each of the **6 day×session combinations** is simulated independently with its
own Poisson arrival rate (λ = observed count / 3600 s) and observed RFID
proportion, while service-time samples are drawn from the **pooled** dataset.

## Quick Start

### 1. Install dependencies

```bash
pip install simpy numpy pandas scipy matplotlib openpyxl
```

### 2. Run with defaults

```bash
python tap_out_sim.py
```

Default: 2 lanes, 200 replications, empirical resampling, 11-step p sweep,
RFID cap at Karcis P90.

### 3. Common options

| Flag | Default | Description |
|------|---------|-------------|
| `--lanes N` | 2 | Number of parallel exit lanes |
| `--lanes-list L` | *(none)* | Comma-separated lane counts, e.g. `1,2,3,4` (overrides `--lanes`) |
| `--reps N` | 200 | Replications per scenario |
| `--service {empirical,lognormal}` | empirical | Resample from data or fit lognormal |
| `--sweep-steps N` | 11 | Steps in the p(RFID) 0→1 sweep |
| `--rfid-cap-pct P` | 90 | Percentile of Karcis distribution used to cap RFID service times in the `fixed_rfid` scenario |
| `--output-csv FILE` | sim_results.csv | CSV output |
| `--output-sweep-plot FILE` | sweep_wait_vs_p.png | Two-panel sweep plot |
| `--output-scenario-plot FILE` | scenario_comparison.png | Scenario comparison bar chart |
| `--seed N` | 42 | Base random seed for reproducibility |
| `--quiet` | off | Suppress progress messages |

### Examples

```bash
# Compare 1–4 lanes, 100 reps each
python tap_out_sim.py --lanes-list 1,2,3,4 --reps 100

# Lognormal service times, finer sweep
python tap_out_sim.py --service lognormal --sweep-steps 21

# Cap RFID at Karcis 80th percentile
python tap_out_sim.py --rfid-cap-pct 80
```

## Outputs

| File | Contents |
|------|----------|
| `sim_results.csv` | All scenarios × day × session × lane count with mean wait, p95 wait, queue lengths, offered load, M/G/c approximation, and 95% CIs |
| `sweep_wait_vs_p.png` | Two-panel plot: mean wait and P95 wait vs p(RFID), one curve per day×session |
| `scenario_comparison.png` | Grouped bar chart comparing the four named scenarios (observed_mix, full_karcis, full_rfid, fixed_rfid) by day×session |

## Scenarios

| Scenario | Description |
|----------|-------------|
| `observed_mix` | Each session's own observed p(RFID) and λ |
| `full_karcis` | p = 0 — all vehicles use manual tickets |
| `full_rfid` | p = 1 — all vehicles use RFID |
| `fixed_rfid` | Same p as observed, but RFID service times capped at the P90 of Karcis (simulates "fixing" RFID to be at most as slow as a typical Karcis transaction) |
| `sweep_pX.XX` | Systematic sweep of p from 0 to 1 |

## Analytical Sanity Check (M/G/c)

For each scenario the script also computes the **Allen-Cunneen** approximation
of mean waiting time E[Wq] for an M/G/c queue:

    E[Wq]_MG/c ≈ E[Wq]_MM/c × (1 + Cv²) / 2

where Cv² is the squared coefficient of variation of the *mixture*
service-time distribution.  This analytical value is printed alongside the
simulated mean with a percentage delta.  The Erlang-C formula provides the
M/M/c base.

If ρ ≥ 1 the system is **overloaded** and a warning is printed.

## Model Assumptions

1. **Arrival process** — Poisson (exponential inter-arrival times).
   Rate λ is computed **per session**: (observed vehicle count) / 3600 s.

2. **Service-time distributions** — Two modes:
   - *Empirical*: bootstrap resampling from the pooled (all days + sessions)
     cleaned data.
   - *Lognormal*: MLE fit, sampled per vehicle.

3. **Payment type** — Each vehicle independently uses RFID with probability
   *p* and Karcis with *1 − p*.

4. **Gate resource** — All lanes are identical, FIFO. No balking or jockeying.

5. **Warm-up** — The first 300 s (5 min) of each replication is discarded.

6. **Replication length** — 3 600 s (1 hour) per replication.

7. **Outlier removal** — IQR ×3 fences (generous to preserve right skew).

8. **Offered load** — ρ = λ · E[S] / c.

9. **Waiting times were not directly observed** in the field data (only
   service times were recorded), so the simulated waiting-time outputs
   **cannot be validated against observations**. The M/G/c analytical
   approximation serves as a partial cross-check instead.

## Data Layout

The Excel file has sheets *Selasa* and *Rabu* with this structure:

| Column | B | C | D | E | F | G | H |
|--------|---|---|---|---|---|---|---|
| Row 2  | — | Sesi 1 | | Sesi 2 | | Sesi 3 | |
| Row 3  | — | Karcis | RFID | Karcis | RFID | Karcis | RFID |
| Row 4+ | No | time (s) | time (s) | … | … | … | … |

Cells with timestamps like `"01:06.35"` are parsed as 66.35 s.
