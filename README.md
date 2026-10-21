# Exit-Gate Tap-Out Discrete Event Simulation

## Overview

A SimPy-based Discrete Event Simulation (DES) modelling vehicle departures
through parking-lot exit gates at UIN Jakarta.  **Two separate gates** (Karcis
and RFID), each with its own FIFO queue, serve vehicles according to their
payment method.

### Versions

| Version | Script | Data Source | Notes |
|---------|--------|-------------|-------|
| **v3 (current)** | `tap_out_sim_v3.py` | `Hasil_Cleaning_Tap_Out_IQR_Final.xlsx` | Two gates, IQR-cleaned data, full scenario suite |
| v2 (provisional) | `tap_out_sim.py` | `Pengumpulan Data Pemodelan Tap Out.xlsx` | Single shared gate, raw data only. **Provisional diagnostics — do not use for final analysis.** |

### Data Reconciliation

The raw Pengumpulan workbook contains only 270 RFID observations vs 630 in the
IQR-cleaned workbook.  Karcis counts match (1 049).  The raw workbook is
**incomplete** for RFID.  All modelling in v3 uses the cleaning workbook.

## Quick Start

### 1. Install dependencies

```bash
pip install simpy numpy pandas scipy matplotlib openpyxl
```

### 2. Run with defaults

```bash
python tap_out_sim_v3.py
```

Default: Karcis 2 lanes, RFID 1 lane, 200 replications, empirical resampling,
no warm-up.

### 3. Common options

| Flag | Default | Description |
|------|---------|-------------|
| `--karcis-lanes N` | 2 | Number of Karcis gate lanes **(assumed)** |
| `--rfid-lanes N` | 1 | Number of RFID gate lanes **(assumed)** |
| `--lanes-grid L` | *(none)* | Semicolon-separated K,R pairs, e.g. `1,1;2,1;2,2` |
| `--reps N` | 200 | Replications per scenario |
| `--service {empirical,fitted}` | empirical | Resample from data or use best-fit distribution |
| `--warmup S` | 0 | Warm-up period (seconds); try 300 to see effect |
| `--outdir DIR` | results_v3 | Output directory |
| `--seed N` | 42 | Base random seed for reproducibility |
| `--quiet` | off | Suppress progress messages |

### Examples

```bash
# Default run (200 reps, empirical)
python tap_out_sim_v3.py

# With fitted distributions
python tap_out_sim_v3.py --service fitted

# Lane-count sensitivity
python tap_out_sim_v3.py --lanes-grid "1,1;2,1;2,2;3,1"

# 300s warm-up
python tap_out_sim_v3.py --warmup 300
```

## Outputs (in `results_v3/`)

| File | Contents |
|------|----------|
| `sim_results_v3.csv` | All scenarios × session with per-gate metrics, 95% CIs, M/G/c |
| `scenario_comparison.png` | Grouped bar chart: mean wait by scenario per session |
| `sweep_wait_vs_shift.png` | Shift sweep: mean/P95 wait vs % Karcis shifted to RFID (log y) |
| `fit_table.csv` | Distribution fit results (AIC, KS) per method |
| `fit_karcis.png` / `fit_rfid.png` | Histogram + QQ plots for Karcis / RFID |
| `reconciliation.csv` | Raw vs cleaning workbook counts |
| `validation.csv` | M/G/c vs simulation, throughput checks |

## Scenarios

For each of the 6 Hari × Sesi combinations:

| Scenario | Description |
|----------|-------------|
| `as_is` | Observed Karcis/RFID arrival split, default lanes |
| `shift_25/50/75/100` | Move 25–100% of Karcis arrivals to RFID, lanes unchanged |
| `full_rfid` | All arrivals to RFID, all lanes reassigned to RFID |
| `full_karcis` | All arrivals to Karcis, all lanes reassigned to Karcis |
| `sensitivity` | as_is but with IQR-outlier rows included (Log Cleaning data) |

## Model Architecture

```
Karcis vehicles ──→ [Karcis Gate: simpy.Resource(karcis_lanes)] ──→ exit
RFID vehicles   ──→ [RFID Gate:   simpy.Resource(rfid_lanes)]  ──→ exit
```

- **Two independent gates**, each with its own FIFO queue.
- **Poisson arrivals**: λ = N_before_IQR / 3600 s per method per session.
  Counts *before* outlier removal are used because outliers are real vehicles.
- **Service times**: Empirical resampling from pooled cleaned data, or
  best-fit parametric distribution (lognormal, gamma, or Weibull).
- **200 replications** with common random numbers; 95% confidence intervals.

## Analytical Validation (M/G/c)

Allen-Cunneen approximation per gate:

    E[Wq]_MG/c ≈ E[Wq]_MM/c × (1 + Cv²) / 2

Printed alongside simulated mean with Δ%.  Overloaded scenarios (ρ ≥ 1) are
flagged.  Throughput is compared to observed vehicle counts.

## Summary of Key Findings

1. **RFID service times are slower and more variable** than Karcis
   (mean 16.8 s vs 13.9 s; CV 0.74 vs 0.43), likely due to the RFID gate
   needing a longer scan/validation time in current implementation.

2. **Shifting Karcis traffic to RFID without adding RFID lanes causes
   overload**: at shift_50 for Selasa_S3 the RFID gate reaches ρ ≈ 1.8.

3. **full_rfid with all 3 lanes** performs well (ρ < 0.9 for most sessions),
   suggesting RFID *could* work system-wide if lane capacity is reallocated
   and/or RFID service times are reduced.

4. **Kruskal-Wallis confirms** that both Karcis and RFID service times differ
   significantly across sessions (p < 10⁻¹⁵), supporting per-session analysis.

5. **Lognormal is the best fit** for both Karcis (AIC 6089) and RFID (AIC 4304)
   based on AIC and KS tests.

## Assumptions & Limitations

1. **Lane counts are assumed** (Karcis=2, RFID=1). The actual number of
   physical lanes is not recorded in the data. Use `--lanes-grid` for
   sensitivity analysis.

2. **Poisson arrival process** assumed. Real arrivals may be bursty or
   time-varying within a session; the model uses a constant rate per hour.

3. **Waiting times were not directly observed** in the field — only service
   times were recorded. Simulated waiting-time outputs cannot be validated
   against observations. The M/G/c analytical approximation serves as a
   partial cross-check.

4. **No warm-up by default** (`--warmup 0`). Each 1-hour session starts
   with an empty system, which reflects the real initial condition. A 300 s
   warm-up can be enabled to study the effect.

5. **Service times pooled across sessions** for resampling. Although the
   Kruskal-Wallis test shows session differences, pooling provides a larger
   sample. The `--service fitted` option uses a parametric fit instead.

6. **No balking, reneging, or jockeying** modelled — vehicles wait in
   their assigned gate's queue until served.

7. **Data mismatch**: The raw Pengumpulan workbook has only 270 RFID rows
   vs 630 in the cleaning file (Karcis matches at 1 049). All v3 analysis
   uses the cleaning workbook.

## Data Layout

`Hasil_Cleaning_Tap_Out_IQR_Final.xlsx` sheets:

| Sheet | Contents |
|-------|----------|
| Dataset Bersih | 1 584 cleaned service times (after IQR removal) |
| Ringkasan IQR | Per Hari×Sesi×Metode: counts before/after IQR, quartiles, fences |
| Log Cleaning | All 1 679 valid observations including outliers |
| Data Dikeluarkan | 95 IQR outliers removed |
| Langkah Cleaning | Cleaning methodology (IQR ×1.5 fences) |
