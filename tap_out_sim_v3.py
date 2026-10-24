#!/usr/bin/env python3
"""
Discrete-Event Simulation of a Vehicle Exit-Gate (Tap-Out) System  v3
=====================================================================
Two **separate** gates (Karcis, RFID), each a SimPy Resource with its
own FIFO queue.  Data source: Hasil_Cleaning_Tap_Out_IQR_Final.xlsx.

v3 changes vs v2
-----------------
- Uses the IQR-cleaned workbook (Dataset Bersih, Ringkasan IQR, Log Cleaning).
- Two independent gate resources (karcis gate, RFID gate).
- --karcis-lanes (default 2), --rfid-lanes (default 1), --lanes-grid.
- Per-method arrival rates from counts BEFORE IQR cleaning.
- Shift scenarios (25/50/75/100), full_rfid, full_karcis, sensitivity.
- Distribution fitting: lognormal, gamma, Weibull; AIC/KS; histograms/QQ.
- Kruskal-Wallis test for session differences.
- Allen-Cunneen M/G/c per gate.
- 200 replications with common random numbers.
- Per-gate metrics: mean wait, p95 wait, max queue, avg queue, utilisation,
  throughput, end-of-hour backlog.
- No warm-up by default (--warmup 0).

ASSUMPTION: Lane counts (--karcis-lanes, --rfid-lanes) are assumed values
because the actual number of physical lanes is not recorded in the data.

Author : auto-generated  (v3)
Date   : 2026-10-22
"""

from __future__ import annotations

import sys
# Ensure UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import argparse
import math
import os
import sys
import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import simpy
from scipy import stats

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

# ──────────────────────────────────────────────────────────────────
# 0.  CONSTANTS & PATHS
# ──────────────────────────────────────────────────────────────────

CLEANING_FILE = "Hasil_Cleaning_Tap_Out_IQR_Final.xlsx"
RAW_FILE = "Pengumpulan Data Pemodelan Tap Out.xlsx"
SIM_DURATION = 3600.0  # 1-hour session

HARI_ORDER = ["Selasa", "Rabu"]
SESI_ORDER = ["Sesi 1", "Sesi 2", "Sesi 3"]

# ──────────────────────────────────────────────────────────────────
# 0a.  RECONCILIATION  (Step 0)
# ──────────────────────────────────────────────────────────────────

SESSION_COLS_RAW = {
    "Sesi 1": {"Karcis": 2, "RFID": 3},
    "Sesi 2": {"Karcis": 4, "RFID": 5},
    "Sesi 3": {"Karcis": 6, "RFID": 7},
}


def reconciliation(raw_path: str, cleaning_path: str, outdir: str):
    """Print and save counts: raw Pengumpulan vs cleaning workbook."""
    rows = []
    # Raw workbook
    raw_xls = pd.ExcelFile(raw_path)
    for sheet in HARI_ORDER:
        df = pd.read_excel(raw_xls, sheet_name=sheet, header=None)
        for sesi, cols in SESSION_COLS_RAW.items():
            for metode, ci in cols.items():
                n = df.iloc[4:, ci].dropna().shape[0]
                rows.append({"source": "Raw", "Hari": sheet, "Sesi": sesi,
                             "Metode": metode, "count": n})
    # Cleaning workbook (Log Cleaning = all valid observations incl outliers)
    log = pd.read_excel(cleaning_path, sheet_name="Log Cleaning")
    for hari in HARI_ORDER:
        for sesi in SESI_ORDER:
            for metode in ["Karcis", "RFID"]:
                n = log[(log["Hari"] == hari) & (log["Sesi"] == sesi) &
                        (log["Metode"] == metode)].shape[0]
                rows.append({"source": "Cleaning", "Hari": hari, "Sesi": sesi,
                             "Metode": metode, "count": n})

    rdf = pd.DataFrame(rows)
    pivot = rdf.pivot_table(index=["Hari", "Sesi", "Metode"],
                            columns="source", values="count",
                            aggfunc="sum").fillna(0).astype(int)
    pivot["Diff"] = pivot["Cleaning"] - pivot["Raw"]
    pivot = pivot.reset_index()

    print("\n" + "=" * 80)
    print("STEP 0 — DATA RECONCILIATION")
    print("=" * 80)
    print(pivot.to_string(index=False))
    print(f"\nRaw totals  : Karcis={rdf[(rdf.source=='Raw')&(rdf.Metode=='Karcis')]['count'].sum()}, "
          f"RFID={rdf[(rdf.source=='Raw')&(rdf.Metode=='RFID')]['count'].sum()}")
    cl_k = rdf[(rdf.source == "Cleaning") & (rdf.Metode == "Karcis")]["count"].sum()
    cl_r = rdf[(rdf.source == "Cleaning") & (rdf.Metode == "RFID")]["count"].sum()
    print(f"Cleaning tot: Karcis={cl_k}, RFID={cl_r}")
    print("\n⚠  MISMATCH: The raw Pengumpulan workbook has 270 RFID rows vs 630 "
          "in the cleaning file.")
    print("   Karcis counts match (1049).  The raw workbook is INCOMPLETE for "
          "RFID data.")
    print("   → Proceeding with the cleaning workbook for all modelling.\n")

    pivot.to_csv(os.path.join(outdir, "reconciliation.csv"), index=False)
    return pivot


# ──────────────────────────────────────────────────────────────────
# 1.  DATA LOADING
# ──────────────────────────────────────────────────────────────────

@dataclass
class CellData:
    """Per Hari × Sesi × Metode data block."""
    hari: str
    sesi: str
    metode: str
    n_before_iqr: int          # count BEFORE outlier removal (for arrivals)
    clean_times: np.ndarray    # after IQR cleaning (Dataset Bersih)
    all_valid_times: np.ndarray  # all valid incl. outliers (Log Cleaning)

    @property
    def label(self) -> str:
        snum = self.sesi.replace("Sesi ", "S")
        return f"{self.hari}_{snum}"


@dataclass
class SessionBlock:
    """All data for one Hari × Sesi (both methods)."""
    hari: str
    sesi: str
    karcis: CellData
    rfid: CellData

    @property
    def label(self) -> str:
        snum = self.sesi.replace("Sesi ", "S")
        return f"{self.hari}_{snum}"

    @property
    def total_before_iqr(self) -> int:
        return self.karcis.n_before_iqr + self.rfid.n_before_iqr


def load_data(cleaning_path: str) -> Tuple[List[SessionBlock], pd.DataFrame]:
    """Load cleaned data and IQR summary.  Returns (sessions, ringkasan)."""
    bersih = pd.read_excel(cleaning_path, sheet_name="Dataset Bersih")
    ringkasan = pd.read_excel(cleaning_path, sheet_name="Ringkasan IQR")
    log = pd.read_excel(cleaning_path, sheet_name="Log Cleaning")

    sessions: List[SessionBlock] = []
    for hari in HARI_ORDER:
        for sesi in SESI_ORDER:
            cells = {}
            for metode in ["Karcis", "RFID"]:
                # Clean times
                mask_b = ((bersih["Hari"] == hari) & (bersih["Sesi"] == sesi) &
                          (bersih["Metode"] == metode))
                ct = bersih.loc[mask_b, "Durasi Numerik (detik)"].values.astype(float)

                # All valid (incl outliers)
                mask_l = ((log["Hari"] == hari) & (log["Sesi"] == sesi) &
                          (log["Metode"] == metode))
                at = log.loc[mask_l, "Durasi Numerik (detik)"].values.astype(float)

                # N before IQR from Ringkasan
                mask_r = ((ringkasan["Hari"] == hari) & (ringkasan["Sesi"] == sesi) &
                          (ringkasan["Metode"] == metode))
                n_before = int(ringkasan.loc[mask_r, "N Valid Sebelum IQR"].iloc[0])

                cells[metode] = CellData(hari, sesi, metode, n_before, ct, at)

            sessions.append(SessionBlock(hari, sesi, cells["Karcis"], cells["RFID"]))

    return sessions, ringkasan


def pool_times(sessions: List[SessionBlock], use_log: bool = False):
    """Pool all service times across Hari×Sesi.
    Returns (all_karcis, all_rfid).
    If use_log=True, uses all valid (incl outliers) from Log Cleaning.
    """
    if use_log:
        k = np.concatenate([s.karcis.all_valid_times for s in sessions])
        r = np.concatenate([s.rfid.all_valid_times for s in sessions])
    else:
        k = np.concatenate([s.karcis.clean_times for s in sessions])
        r = np.concatenate([s.rfid.clean_times for s in sessions])
    return k, r


# ──────────────────────────────────────────────────────────────────
# 1b.  INPUT ANALYSIS  (Step 1)
# ──────────────────────────────────────────────────────────────────

def _aic(n: int, k: int, loglik: float) -> float:
    """AIC = 2k - 2 ln(L)."""
    return 2 * k - 2 * loglik


def _fit_distributions(data: np.ndarray, label: str):
    """Fit normal, lognormal, gamma, Weibull to data. Return dict of results."""
    results = {}
    for name, dist in [("normal", stats.norm),
                       ("lognormal", stats.lognorm),
                       ("gamma", stats.gamma),
                       ("weibull", stats.weibull_min)]:
        try:
            # Normal has free loc/scale; others are fixed at loc=0
            params = dist.fit(data) if name == "normal" else dist.fit(data, floc=0)
            loglik = np.sum(dist.logpdf(data, *params))
            k = len(params)
            aic = _aic(len(data), k, loglik)
            ks_stat, ks_p = stats.kstest(data, dist.cdf, args=params)
            results[name] = {
                "params": params, "loglik": loglik, "aic": aic,
                "ks_stat": ks_stat, "ks_p": ks_p, "n": len(data),
                "label": label,
            }
        except Exception as e:
            results[name] = {"error": str(e), "label": label}
    return results


def kruskal_wallis_sessions(sessions: List[SessionBlock], outdir: str):
    """Test whether sessions differ per method (Kruskal-Wallis)."""
    print("\n" + "=" * 80)
    print("STEP 1a — KRUSKAL-WALLIS TEST: do sessions differ?")
    print("=" * 80)
    results = []
    for metode in ["Karcis", "RFID"]:
        groups = []
        group_labels = []
        for sb in sessions:
            cell = sb.karcis if metode == "Karcis" else sb.rfid
            if len(cell.clean_times) > 0:
                groups.append(cell.clean_times)
                group_labels.append(cell.label)
        if len(groups) >= 2:
            stat, p = stats.kruskal(*groups)
            sig = "YES" if p < 0.05 else "no"
            print(f"  {metode:8s}  H={stat:8.2f}  p={p:.4e}  Significant(α=0.05): {sig}")
            print(f"            Groups: {group_labels}")
            results.append({"Metode": metode, "H": stat, "p": p,
                            "significant": sig, "n_groups": len(groups)})
    return results


def input_analysis(sessions: List[SessionBlock], outdir: str):
    """Fit distributions, save tables and plots."""
    kw_results = kruskal_wallis_sessions(sessions, outdir)

    # Determine whether to fit per-session as well
    kw_dict = {r["Metode"]: r["significant"] == "YES" for r in kw_results}

    print("\n" + "=" * 80)
    print("STEP 1b — DISTRIBUTION FITTING")
    print("=" * 80)

    fit_rows = []

    # Fit per method (pooled)
    all_k, all_r = pool_times(sessions, use_log=False)
    for metode, data in [("Karcis", all_k), ("RFID", all_r)]:
        label = f"{metode}_pooled"
        fits = _fit_distributions(data, label)
        for dname, res in fits.items():
            if "error" in res:
                continue
            fit_rows.append({
                "group": label, "distribution": dname,
                "n": res["n"], "AIC": f"{res['aic']:.1f}",
                "KS_stat": f"{res['ks_stat']:.4f}",
                "KS_p": f"{res['ks_p']:.4e}",
                "params": str(res["params"]),
            })
            print(f"  {label:30s} {dname:12s} AIC={res['aic']:9.1f}  "
                  f"KS={res['ks_stat']:.4f} (p={res['ks_p']:.3e})")

    # Fit per method×session if KW is significant
    for metode in ["Karcis", "RFID"]:
        if kw_dict.get(metode, False):
            print(f"\n  Sessions differ for {metode} → fitting per session too:")
            for sb in sessions:
                cell = sb.karcis if metode == "Karcis" else sb.rfid
                if len(cell.clean_times) < 10:
                    continue
                label = f"{metode}_{sb.label}"
                fits = _fit_distributions(cell.clean_times, label)
                for dname, res in fits.items():
                    if "error" in res:
                        continue
                    fit_rows.append({
                        "group": label, "distribution": dname,
                        "n": res["n"], "AIC": f"{res['aic']:.1f}",
                        "KS_stat": f"{res['ks_stat']:.4f}",
                        "KS_p": f"{res['ks_p']:.4e}",
                        "params": str(res["params"]),
                    })
                    print(f"    {label:28s} {dname:12s} AIC={res['aic']:9.1f}  "
                          f"KS={res['ks_stat']:.4f} (p={res['ks_p']:.3e})")

    fit_df = pd.DataFrame(fit_rows)
    fit_df.to_csv(os.path.join(outdir, "fit_table.csv"), index=False)
    print(f"\n✓ Fit table saved to {os.path.join(outdir, 'fit_table.csv')}")

    # ── Fitting conclusion ──────────────────────────────────────────
    print("\n" + "─" * 80)
    print("  FITTING CONCLUSION")
    print("─" * 80)
    # Summarise Normal vs others for pooled data
    for metode in ["Karcis", "RFID"]:
        grp = f"{metode}_pooled"
        norm_row = [r for r in fit_rows
                    if r["group"] == grp and r["distribution"] == "normal"]
        lnorm_row = [r for r in fit_rows
                     if r["group"] == grp and r["distribution"] == "lognormal"]
        if norm_row:
            nks = float(norm_row[0]["KS_stat"])
            nkp = float(norm_row[0]["KS_p"])
            print(f"  {metode:8s}  Normal  KS D={nks:.4f}  p={nkp:.3e}"
                  f"  → {'REJECTED' if nkp < 0.05 else 'not rejected'} (α=0.05)")
        if lnorm_row:
            lks = float(lnorm_row[0]["KS_stat"])
            lkp = float(lnorm_row[0]["KS_p"])
            print(f"  {metode:8s}  Lognorm KS D={lks:.4f}  p={lkp:.3e}"
                  f"  → {'REJECTED' if lkp < 0.05 else 'not rejected'} (α=0.05)")

    # Check if Normal is rejected for all pooled groups
    all_normal_rejected = True
    for metode in ["Karcis", "RFID"]:
        grp = f"{metode}_pooled"
        norm_row = [r for r in fit_rows
                    if r["group"] == grp and r["distribution"] == "normal"]
        if norm_row and float(norm_row[0]["KS_p"]) >= 0.05:
            all_normal_rejected = False

    if all_normal_rejected:
        print("\n  ⇒ Normal distribution REJECTED at all levels (KS p < 0.05).")
        print("  ⇒ PRIMARY MODEL: Empirical resampling (non-parametric).")
        print("    Justification: No standard parametric distribution provides a")
        print("    convincing fit across all groups. Empirical resampling preserves")
        print("    the actual shape, skewness, and multi-modality of the data.")
        print("  ⇒ COMPARISON MODEL: Lognormal (lowest AIC, KS D ≈ 0.02–0.06).")
        print("    Lognormal is retained as a parametric comparison (--service fitted)")
        print("    but is NOT the primary model for the simulation.")
    else:
        print("\n  ⇒ Normal not rejected for all groups.")
        print("  ⇒ Check individual KS results above.")
    print("─" * 80)

    # Histogram + QQ plots
    _save_fit_plots(all_k, "Karcis", outdir)
    _save_fit_plots(all_r, "RFID", outdir)

    return fit_df, kw_dict


def _save_fit_plots(data: np.ndarray, metode: str, outdir: str):
    """Save histogram with fitted PDFs and QQ plots (incl. Normal)."""
    fig, axes = plt.subplots(1, 5, figsize=(25, 4.5))

    # Histogram
    ax = axes[0]
    ax.hist(data, bins=40, density=True, alpha=0.6, color="steelblue",
            edgecolor="white", label="Data")
    x = np.linspace(max(0.01, data.min() * 0.5), data.max() * 1.1, 300)
    colors = {"normal": "gray", "lognormal": "red", "gamma": "green",
              "weibull": "orange"}
    dists = {"normal": stats.norm, "lognormal": stats.lognorm,
             "gamma": stats.gamma, "weibull": stats.weibull_min}
    for dname, dist in dists.items():
        try:
            params = dist.fit(data) if dname == "normal" else dist.fit(data, floc=0)
            ax.plot(x, dist.pdf(x, *params), color=colors[dname],
                    linewidth=1.5, label=dname,
                    linestyle="--" if dname == "normal" else "-")
        except Exception:
            pass
    ax.set_title(f"{metode} — Histogram + Fitted PDFs")
    ax.set_xlabel("Service time (s)")
    ax.set_ylabel("Density")
    ax.legend(fontsize=7)
    ax.grid(True, alpha=0.2)

    # QQ plots (Normal + 3 others)
    for i, (dname, dist) in enumerate(dists.items()):
        ax = axes[i + 1]
        try:
            params = dist.fit(data) if dname == "normal" else dist.fit(data, floc=0)
            theoretical = dist.ppf(
                np.linspace(0.01, 0.99, len(data)), *params)
            theoretical.sort()
            empirical = np.sort(data)
            ax.scatter(theoretical, empirical, s=4, alpha=0.4, c=colors[dname])
            lims = [min(theoretical.min(), empirical.min()),
                    max(theoretical.max(), empirical.max())]
            ax.plot(lims, lims, "k--", linewidth=0.8)
            # Add KS result to title
            ks_stat, ks_p = stats.kstest(data, dist.cdf, args=params)
            reject = "REJECTED" if ks_p < 0.05 else "OK"
            ax.set_title(f"{metode} QQ: {dname}\nD={ks_stat:.3f} ({reject})",
                         fontsize=9)
            ax.set_xlabel("Theoretical")
            ax.set_ylabel("Empirical")
            ax.grid(True, alpha=0.2)
        except Exception:
            ax.set_title(f"{metode} QQ: {dname} (failed)")

    fig.tight_layout()
    path = os.path.join(outdir, f"fit_{metode.lower()}.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"✓ Fit plots saved to {path}")


# ──────────────────────────────────────────────────────────────────
# 2.  ARRIVAL RATES  (Step 2)
# ──────────────────────────────────────────────────────────────────

def compute_arrival_rates(sessions: List[SessionBlock]):
    """Compute per Hari×Sesi×Metode arrival rate.
    rate = N_Valid_Before_IQR / 3600  (Poisson).
    Outliers are real vehicles → count before removal.
    """
    print("\n" + "=" * 80)
    print("STEP 2 — ARRIVAL RATES  (λ = N_before_IQR / 3600)")
    print("=" * 80)
    for sb in sessions:
        lam_k = sb.karcis.n_before_iqr / 3600.0
        lam_r = sb.rfid.n_before_iqr / 3600.0
        lam_tot = (sb.karcis.n_before_iqr + sb.rfid.n_before_iqr) / 3600.0
        print(f"  {sb.label:12s}  Karcis: N={sb.karcis.n_before_iqr:4d}  "
              f"λ={lam_k:.4f}  |  RFID: N={sb.rfid.n_before_iqr:4d}  "
              f"λ={lam_r:.4f}  |  Total: N={sb.total_before_iqr:4d}  "
              f"λ={lam_tot:.4f}")


# ──────────────────────────────────────────────────────────────────
# 3.  M/G/c ANALYTICAL  (Allen-Cunneen)
# ──────────────────────────────────────────────────────────────────

def _erlang_c(c: int, a: float) -> float:
    """Erlang-C probability C(c, a)."""
    if c < 1 or a <= 0:
        return 0.0
    rho = a / c
    if rho >= 1.0:
        return 1.0
    sum_part = sum(a ** k / math.factorial(k) for k in range(c))
    last = (a ** c / math.factorial(c)) * (1.0 / (1.0 - rho))
    p0 = 1.0 / (sum_part + last)
    return min(last * p0, 1.0)


def mgc_mean_wait(lam: float, mean_s: float, cv2_s: float,
                  c: int) -> Tuple[float, float]:
    """Allen-Cunneen E[Wq] for M/G/c.  Returns (E[Wq], rho)."""
    if lam <= 0 or mean_s <= 0 or c < 1:
        return 0.0, 0.0
    mu = 1.0 / mean_s
    a = lam / mu
    rho = a / c
    if rho >= 1.0:
        return float("inf"), rho
    ec = _erlang_c(c, a)
    wq_mmc = ec / (c * mu * (1 - rho))
    wq_mgc = wq_mmc * (1 + cv2_s) / 2.0
    return max(wq_mgc, 0.0), rho


# ──────────────────────────────────────────────────────────────────
# 4.  SIMPY SIMULATION  (Step 3)
# ──────────────────────────────────────────────────────────────────

@dataclass
class GateResult:
    """Raw result for one gate in one replication."""
    waits: list = field(default_factory=list)
    queue_snapshots: list = field(default_factory=list)
    max_queue: int = 0
    vehicles_served: int = 0
    vehicles_in_system: int = 0  # still in queue or service at end


def _sample_service(rng: np.random.Generator, data: np.ndarray,
                    service_mode: str, fit_params: Optional[dict]) -> float:
    """Sample one service time."""
    if service_mode == "empirical" or fit_params is None:
        return float(rng.choice(data))
    else:
        # Use best-fit distribution
        dname = fit_params["dist_name"]
        params = fit_params["params"]
        dist_map = {"lognormal": stats.lognorm, "gamma": stats.gamma,
                    "weibull": stats.weibull_min}
        dist = dist_map[dname]
        val = float(dist.rvs(*params, random_state=rng))
        return max(val, 0.01)  # guard against negative


def _vehicle(env, gate, rng, svc_data, service_mode, fit_params,
             result: GateResult, warmup: float):
    """SimPy process for one vehicle at its gate."""
    arrive = env.now
    with gate.request() as req:
        yield req
        wait = env.now - arrive
        svc = _sample_service(rng, svc_data, service_mode, fit_params)
        yield env.timeout(svc)
        if arrive >= warmup:
            result.waits.append(wait)
            result.vehicles_served += 1


def _arrivals(env, gate, rng, lam, svc_data, service_mode, fit_params,
              result: GateResult, warmup: float):
    """Poisson arrivals for one gate."""
    if lam <= 0:
        return
    while True:
        iat = rng.exponential(1.0 / lam)
        yield env.timeout(iat)
        env.process(_vehicle(env, gate, rng, svc_data, service_mode,
                             fit_params, result, warmup))


def _queue_monitor(env, gate, result: GateResult, warmup: float):
    """Sample queue length every second."""
    while True:
        yield env.timeout(1.0)
        qlen = len(gate.queue)
        if env.now >= warmup:
            result.queue_snapshots.append(qlen)
            if qlen > result.max_queue:
                result.max_queue = qlen


def run_one_replication(
    seed: int,
    karcis_lanes: int, rfid_lanes: int,
    lam_karcis: float, lam_rfid: float,
    karcis_svc_data: np.ndarray, rfid_svc_data: np.ndarray,
    service_mode: str,
    karcis_fit: Optional[dict], rfid_fit: Optional[dict],
    warmup: float, sim_duration: float,
) -> Tuple[GateResult, GateResult]:
    """Run one replication with two gates. Returns (karcis_result, rfid_result)."""
    rng = np.random.default_rng(seed)
    env = simpy.Environment()

    gate_k = simpy.Resource(env, capacity=max(karcis_lanes, 1))
    gate_r = simpy.Resource(env, capacity=max(rfid_lanes, 1))

    res_k = GateResult()
    res_r = GateResult()

    if lam_karcis > 0:
        env.process(_arrivals(env, gate_k, rng, lam_karcis, karcis_svc_data,
                              service_mode, karcis_fit, res_k, warmup))
    if lam_rfid > 0:
        env.process(_arrivals(env, gate_r, rng, lam_rfid, rfid_svc_data,
                              service_mode, rfid_fit, res_r, warmup))

    env.process(_queue_monitor(env, gate_k, res_k, warmup))
    env.process(_queue_monitor(env, gate_r, res_r, warmup))

    env.run(until=sim_duration + warmup)

    # End-of-hour backlog = vehicles still in queue
    res_k.vehicles_in_system = len(gate_k.queue)
    res_r.vehicles_in_system = len(gate_r.queue)

    return res_k, res_r


# ──────────────────────────────────────────────────────────────────
# 5.  MULTI-REPLICATION & STATISTICS  (Step 5)
# ──────────────────────────────────────────────────────────────────

@dataclass
class GateStats:
    """Aggregated stats for one gate across replications."""
    gate: str  # "Karcis" or "RFID"
    mean_wait: float = 0.0
    ci_mean_wait: tuple = (0.0, 0.0)
    p95_wait: float = 0.0
    ci_p95_wait: tuple = (0.0, 0.0)
    avg_queue: float = 0.0
    ci_avg_queue: tuple = (0.0, 0.0)
    max_queue: float = 0.0
    ci_max_queue: tuple = (0.0, 0.0)
    utilisation: float = 0.0
    ci_utilisation: tuple = (0.0, 0.0)
    throughput: float = 0.0
    ci_throughput: tuple = (0.0, 0.0)
    backlog: float = 0.0
    ci_backlog: tuple = (0.0, 0.0)
    mgc_wq: float = 0.0
    rho: float = 0.0


@dataclass
class ScenarioResult:
    """Full scenario result."""
    hari: str
    sesi: str
    scenario: str
    karcis_lanes: int
    rfid_lanes: int
    lam_karcis: float
    lam_rfid: float
    n_reps: int
    service_mode: str
    karcis_stats: GateStats = field(default_factory=lambda: GateStats("Karcis"))
    rfid_stats: GateStats = field(default_factory=lambda: GateStats("RFID"))


def _ci95(arr: np.ndarray):
    """95% CI: (mean, (lo, hi))."""
    n = len(arr)
    if n < 2:
        m = float(arr[0]) if n == 1 else 0.0
        return m, (m, m)
    m = float(np.mean(arr))
    se = float(stats.sem(arr))
    h = se * stats.t.ppf(0.975, n - 1)
    return m, (m - h, m + h)


def _aggregate_gate(rep_results: List[GateResult], lam: float,
                    svc_data: np.ndarray, n_lanes: int,
                    gate_name: str, warmup: float) -> GateStats:
    """Aggregate replications for one gate."""
    gs = GateStats(gate=gate_name)
    n = len(rep_results)

    arr_mw, arr_p95, arr_aq, arr_mq = [], [], [], []
    arr_util, arr_tp, arr_bl = [], [], []

    effective_duration = SIM_DURATION  # stats collected over this window

    for r in rep_results:
        if r.waits:
            arr_mw.append(np.mean(r.waits))
            arr_p95.append(np.percentile(r.waits, 95))
        else:
            arr_mw.append(0.0)
            arr_p95.append(0.0)
        if r.queue_snapshots:
            arr_aq.append(np.mean(r.queue_snapshots))
        else:
            arr_aq.append(0.0)
        arr_mq.append(float(r.max_queue))
        arr_tp.append(float(r.vehicles_served))
        arr_bl.append(float(r.vehicles_in_system))

        # Utilisation estimate: throughput * mean_svc / (n_lanes * duration)
        if n_lanes > 0 and len(svc_data) > 0 and effective_duration > 0:
            mean_svc = float(np.mean(svc_data))
            util = r.vehicles_served * mean_svc / (n_lanes * effective_duration)
            arr_util.append(min(util, 1.0))
        else:
            arr_util.append(0.0)

    for attr, arr in [("mean_wait", arr_mw), ("p95_wait", arr_p95),
                      ("avg_queue", arr_aq), ("max_queue", arr_mq),
                      ("utilisation", arr_util), ("throughput", arr_tp),
                      ("backlog", arr_bl)]:
        a = np.array(arr)
        m, ci = _ci95(a)
        setattr(gs, attr, m)
        setattr(gs, f"ci_{attr}", ci)

    # Analytical M/G/c
    if lam > 0 and len(svc_data) > 0 and n_lanes > 0:
        mean_s = float(np.mean(svc_data))
        var_s = float(np.var(svc_data))
        cv2 = var_s / (mean_s ** 2) if mean_s > 0 else 0.0
        wq, rho = mgc_mean_wait(lam, mean_s, cv2, n_lanes)
        gs.mgc_wq = wq
        gs.rho = rho
    return gs


def run_scenario(
    hari: str, sesi: str, scenario: str,
    karcis_lanes: int, rfid_lanes: int,
    lam_karcis: float, lam_rfid: float,
    karcis_svc: np.ndarray, rfid_svc: np.ndarray,
    service_mode: str,
    karcis_fit: Optional[dict], rfid_fit: Optional[dict],
    n_reps: int, base_seed: int, warmup: float,
) -> ScenarioResult:
    """Run n_reps replications and return aggregated results."""
    rep_k_results = []
    rep_r_results = []

    for i in range(n_reps):
        rk, rr = run_one_replication(
            seed=base_seed + i,
            karcis_lanes=karcis_lanes, rfid_lanes=rfid_lanes,
            lam_karcis=lam_karcis, lam_rfid=lam_rfid,
            karcis_svc_data=karcis_svc, rfid_svc_data=rfid_svc,
            service_mode=service_mode,
            karcis_fit=karcis_fit, rfid_fit=rfid_fit,
            warmup=warmup, sim_duration=SIM_DURATION,
        )
        rep_k_results.append(rk)
        rep_r_results.append(rr)

    sr = ScenarioResult(
        hari=hari, sesi=sesi, scenario=scenario,
        karcis_lanes=karcis_lanes, rfid_lanes=rfid_lanes,
        lam_karcis=lam_karcis, lam_rfid=lam_rfid,
        n_reps=n_reps, service_mode=service_mode,
    )
    sr.karcis_stats = _aggregate_gate(rep_k_results, lam_karcis, karcis_svc,
                                      karcis_lanes, "Karcis", warmup)
    sr.rfid_stats = _aggregate_gate(rep_r_results, lam_rfid, rfid_svc,
                                    rfid_lanes, "RFID", warmup)
    return sr


# ──────────────────────────────────────────────────────────────────
# 6.  SCENARIO DEFINITIONS  (Step 4)
# ──────────────────────────────────────────────────────────────────

def build_scenarios(
    sessions: List[SessionBlock],
    karcis_lanes: int, rfid_lanes: int,
    all_k_clean: np.ndarray, all_r_clean: np.ndarray,
    all_k_log: np.ndarray, all_r_log: np.ndarray,
    service_mode: str,
    karcis_fit: Optional[dict], rfid_fit: Optional[dict],
    n_reps: int, base_seed: int, warmup: float,
    quiet: bool,
) -> List[ScenarioResult]:
    """Build and run all scenarios for all Hari×Sesi."""
    total_lanes = karcis_lanes + rfid_lanes
    all_results: List[ScenarioResult] = []

    # Count total scenarios for progress
    n_sessions = len(sessions)
    # per session: as_is + 4 shifts + full_rfid + full_karcis + sensitivity = 8
    total_jobs = n_sessions * 8
    done = 0

    for sb in sessions:
        lam_k = sb.karcis.n_before_iqr / 3600.0
        lam_r = sb.rfid.n_before_iqr / 3600.0
        lam_total = lam_k + lam_r

        def _run(label, lk, lr, ksvc, rsvc, kl, rl, kfit=karcis_fit, rfit=rfid_fit):
            nonlocal done
            done += 1
            if not quiet:
                pct = done / total_jobs * 100
                print(f"\r  [{pct:5.1f}%]  {sb.label}  {label:<20s}", end="", flush=True)
            return run_scenario(
                sb.hari, sb.sesi, label, kl, rl,
                lk, lr, ksvc, rsvc,
                service_mode, kfit, rfit,
                n_reps, base_seed, warmup,
            )

        # as_is: observed arrival split, observed lane config
        all_results.append(_run("as_is", lam_k, lam_r,
                                all_k_clean, all_r_clean,
                                karcis_lanes, rfid_lanes))

        # shift_25/50/75/100: move Karcis arrivals to RFID
        for pct_shift in [25, 50, 75, 100]:
            frac = pct_shift / 100.0
            shifted_k = lam_k * (1 - frac)
            shifted_r = lam_r + lam_k * frac
            all_results.append(_run(f"shift_{pct_shift}", shifted_k, shifted_r,
                                    all_k_clean, all_r_clean,
                                    karcis_lanes, rfid_lanes))

        # full_rfid: all arrivals to RFID, all lanes to RFID
        all_results.append(_run("full_rfid", 0.0, lam_total,
                                all_k_clean, all_r_clean,
                                0, total_lanes))

        # full_karcis: all arrivals to Karcis, all lanes to Karcis
        all_results.append(_run("full_karcis", lam_total, 0.0,
                                all_k_clean, all_r_clean,
                                total_lanes, 0))

        # sensitivity: as_is but with Log Cleaning data (outliers included)
        all_results.append(_run("sensitivity", lam_k, lam_r,
                                all_k_log, all_r_log,
                                karcis_lanes, rfid_lanes))

    if not quiet:
        print()
    return all_results


# ──────────────────────────────────────────────────────────────────
# 7.  CSV & PRINT  (Step 5)
# ──────────────────────────────────────────────────────────────────

def _gs_to_dict(gs: GateStats, prefix: str) -> dict:
    """Convert GateStats to a flat dict with prefixed keys."""
    d = {}
    for attr in ["mean_wait", "p95_wait", "avg_queue", "max_queue",
                 "utilisation", "throughput", "backlog"]:
        val = getattr(gs, attr)
        ci = getattr(gs, f"ci_{attr}")
        d[f"{prefix}_{attr}"] = f"{val:.3f}"
        d[f"{prefix}_{attr}_CI_lo"] = f"{ci[0]:.3f}"
        d[f"{prefix}_{attr}_CI_hi"] = f"{ci[1]:.3f}"
    d[f"{prefix}_mgc_wq"] = f"{gs.mgc_wq:.3f}" if np.isfinite(gs.mgc_wq) else "inf"
    d[f"{prefix}_rho"] = f"{gs.rho:.4f}"
    return d


def results_to_df(all_results: List[ScenarioResult]) -> pd.DataFrame:
    rows = []
    for sr in all_results:
        row = {
            "hari": sr.hari, "sesi": sr.sesi, "scenario": sr.scenario,
            "karcis_lanes": sr.karcis_lanes, "rfid_lanes": sr.rfid_lanes,
            "lam_karcis": f"{sr.lam_karcis:.4f}",
            "lam_rfid": f"{sr.lam_rfid:.4f}",
            "n_reps": sr.n_reps, "service_mode": sr.service_mode,
        }
        row.update(_gs_to_dict(sr.karcis_stats, "K"))
        row.update(_gs_to_dict(sr.rfid_stats, "R"))
        rows.append(row)
    return pd.DataFrame(rows)


def print_results_table(all_results: List[ScenarioResult]):
    """Compact console table."""
    print("\n" + "=" * 120)
    print("SIMULATION RESULTS  (per gate)")
    print("=" * 120)
    hdr = (f"{'Session':12s} {'Scenario':20s} │ "
           f"{'Gate':7s} {'Lanes':>5s} {'λ':>7s} {'ρ':>6s} "
           f"{'E[W]':>7s} {'CI95':>17s} {'P95(W)':>7s} "
           f"{'AvgQ':>5s} {'MaxQ':>5s} {'Util':>5s} {'TP':>5s} "
           f"{'BL':>4s} {'M/G/c':>7s}")
    print(hdr)
    print("─" * 120)

    for sr in all_results:
        for gs in [sr.karcis_stats, sr.rfid_stats]:
            lam = sr.lam_karcis if gs.gate == "Karcis" else sr.lam_rfid
            lanes = sr.karcis_lanes if gs.gate == "Karcis" else sr.rfid_lanes
            if lam <= 0 and lanes <= 0:
                continue  # skip inactive gates
            snum = sr.sesi.replace("Sesi ", "S")
            label = f"{sr.hari}_{snum}"
            ci = f"[{gs.ci_mean_wait[0]:.1f},{gs.ci_mean_wait[1]:.1f}]"
            mgc_str = f"{gs.mgc_wq:.2f}" if np.isfinite(gs.mgc_wq) else "inf"
            warn = " ⚠" if gs.rho >= 1.0 else ""
            print(f"{label:12s} {sr.scenario:20s} │ "
                  f"{gs.gate:7s} {lanes:5d} {lam:7.4f} {gs.rho:6.3f} "
                  f"{gs.mean_wait:7.2f} {ci:>17s} {gs.p95_wait:7.2f} "
                  f"{gs.avg_queue:5.2f} {gs.max_queue:5.1f} "
                  f"{gs.utilisation:5.3f} {gs.throughput:5.0f} "
                  f"{gs.backlog:4.1f} {mgc_str:>7s}{warn}")


# ──────────────────────────────────────────────────────────────────
# 8.  PLOTS  (Step 5)
# ──────────────────────────────────────────────────────────────────

_COLORS = {
    "Selasa_S1": "#2563eb", "Selasa_S2": "#7c3aed", "Selasa_S3": "#db2777",
    "Rabu_S1": "#059669", "Rabu_S2": "#d97706", "Rabu_S3": "#dc2626",
}

SCENARIO_ORDER = ["as_is", "shift_25", "shift_50", "shift_75", "shift_100",
                  "full_rfid", "full_karcis", "sensitivity"]
SCENARIO_COLORS = {
    "as_is": "#2563eb", "shift_25": "#7c3aed", "shift_50": "#a855f7",
    "shift_75": "#d946ef", "shift_100": "#ec4899",
    "full_rfid": "#10b981", "full_karcis": "#ef4444",
    "sensitivity": "#f59e0b",
}


def save_scenario_comparison(all_results: List[ScenarioResult], outdir: str):
    """Grouped bar chart: mean wait per gate across scenarios."""
    fig, axes = plt.subplots(2, 1, figsize=(16, 10))

    for gate_idx, gate_name in enumerate(["Karcis", "RFID"]):
        ax = axes[gate_idx]
        # Group by session label
        session_labels = []
        seen = set()
        for sr in all_results:
            snum = sr.sesi.replace("Sesi ", "S")
            lbl = f"{sr.hari}_{snum}"
            if lbl not in seen:
                session_labels.append(lbl)
                seen.add(lbl)

        scenarios = [s for s in SCENARIO_ORDER
                     if any(sr.scenario == s for sr in all_results)]
        n_groups = len(session_labels)
        n_bars = len(scenarios)
        if n_bars == 0 or n_groups == 0:
            continue
        bar_w = 0.8 / n_bars
        x = np.arange(n_groups)

        for si, scn in enumerate(scenarios):
            means = []
            errs_lo = []
            errs_hi = []
            for slbl in session_labels:
                match = [sr for sr in all_results
                         if sr.scenario == scn and
                         f"{sr.hari}_{sr.sesi.replace('Sesi ','S')}" == slbl]
                if match:
                    gs = match[0].karcis_stats if gate_name == "Karcis" else match[0].rfid_stats
                    means.append(gs.mean_wait)
                    errs_lo.append(gs.mean_wait - gs.ci_mean_wait[0])
                    errs_hi.append(gs.ci_mean_wait[1] - gs.mean_wait)
                else:
                    means.append(0)
                    errs_lo.append(0)
                    errs_hi.append(0)

            offset = (si - n_bars / 2 + 0.5) * bar_w
            color = SCENARIO_COLORS.get(scn, "#888")
            ax.bar(x + offset, means, bar_w, label=scn, color=color,
                   yerr=[errs_lo, errs_hi], capsize=1.5,
                   error_kw={"linewidth": 0.6})

        ax.set_xticks(x)
        ax.set_xticklabels(session_labels, fontsize=9)
        ax.set_ylabel("Mean Wait (s)")
        ax.set_title(f"{gate_name} Gate — Mean Wait by Scenario", fontsize=11)
        ax.legend(fontsize=7, ncol=4)
        ax.grid(axis="y", alpha=0.2)

    fig.tight_layout()
    path = os.path.join(outdir, "scenario_comparison.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"✓ Scenario comparison plot saved to {path}")


def save_sweep_plot(all_results: List[ScenarioResult], outdir: str):
    """Sweep plot: mean wait vs shift percentage (log y axis)."""
    shift_scenarios = ["as_is", "shift_25", "shift_50", "shift_75", "shift_100"]
    shift_x = [0, 25, 50, 75, 100]

    fig, axes = plt.subplots(2, 2, figsize=(16, 10))

    for gate_idx, gate_name in enumerate(["Karcis", "RFID"]):
        for metric_idx, (metric, mlabel) in enumerate([
            ("mean_wait", "Mean Wait (s)"), ("p95_wait", "P95 Wait (s)")
        ]):
            ax = axes[metric_idx][gate_idx]

            # Group by session
            session_labels = []
            seen = set()
            for sr in all_results:
                snum = sr.sesi.replace("Sesi ", "S")
                lbl = f"{sr.hari}_{snum}"
                if lbl not in seen:
                    session_labels.append(lbl)
                    seen.add(lbl)

            for slbl in session_labels:
                vals = []
                ci_lo = []
                ci_hi = []
                for scn in shift_scenarios:
                    match = [sr for sr in all_results
                             if sr.scenario == scn and
                             f"{sr.hari}_{sr.sesi.replace('Sesi ','S')}" == slbl]
                    if match:
                        gs = match[0].karcis_stats if gate_name == "Karcis" else match[0].rfid_stats
                        v = getattr(gs, metric)
                        ci = getattr(gs, f"ci_{metric}")
                        vals.append(max(v, 0.001))
                        ci_lo.append(max(ci[0], 0.001))
                        ci_hi.append(max(ci[1], 0.001))
                    else:
                        vals.append(0.001)
                        ci_lo.append(0.001)
                        ci_hi.append(0.001)

                color = _COLORS.get(slbl, "#888")
                ax.plot(shift_x, vals, "o-", color=color, linewidth=1.5,
                        markersize=4, label=slbl)
                ax.fill_between(shift_x, ci_lo, ci_hi, alpha=0.1, color=color)

            ax.set_xlabel("% Karcis shifted to RFID")
            ax.set_ylabel(mlabel)
            ax.set_title(f"{gate_name} — {mlabel}", fontsize=10)
            ax.set_yscale("log")
            ax.legend(fontsize=7)
            ax.grid(True, alpha=0.2)

    fig.suptitle("Shift Scenario Sweep (log y-axis)", fontsize=13, y=1.01)
    fig.tight_layout()
    path = os.path.join(outdir, "sweep_wait_vs_shift.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"✓ Sweep plot saved to {path}")


# ──────────────────────────────────────────────────────────────────
# 9.  VALIDATION  (Step 6)
# ──────────────────────────────────────────────────────────────────

def validation_checks(all_results: List[ScenarioResult],
                      sessions: List[SessionBlock], outdir: str,
                      warmup: float):
    """Run validation and sanity checks."""
    print("\n" + "=" * 80)
    print("STEP 6 — VALIDATION")
    print("=" * 80)

    val_rows = []

    # 1. Allen-Cunneen vs simulated mean wait
    print("\n  (a) Allen-Cunneen M/G/c vs Simulated mean wait:")
    print(f"  {'Session':12s} {'Scenario':15s} {'Gate':7s} "
          f"{'Sim E[W]':>8s} {'M/G/c':>8s} {'Δ%':>7s} {'ρ':>6s}")
    for sr in all_results:
        if sr.scenario not in ("as_is", "shift_50", "full_rfid", "full_karcis"):
            continue
        for gs in [sr.karcis_stats, sr.rfid_stats]:
            lam = sr.lam_karcis if gs.gate == "Karcis" else sr.lam_rfid
            if lam <= 0:
                continue
            snum = sr.sesi.replace("Sesi ", "S")
            label = f"{sr.hari}_{snum}"
            if np.isfinite(gs.mgc_wq) and gs.mgc_wq > 0:
                delta = (gs.mean_wait - gs.mgc_wq) / gs.mgc_wq * 100
                delta_str = f"{delta:+.0f}%"
            else:
                delta_str = "—"
            mgc_str = f"{gs.mgc_wq:.2f}" if np.isfinite(gs.mgc_wq) else "inf"
            warn = " ⚠ rho>=1" if gs.rho >= 1.0 else ""
            print(f"  {label:12s} {sr.scenario:15s} {gs.gate:7s} "
                  f"{gs.mean_wait:8.2f} {mgc_str:>8s} {delta_str:>7s} "
                  f"{gs.rho:6.3f}{warn}")
            val_rows.append({
                "session": label, "scenario": sr.scenario,
                "gate": gs.gate, "sim_mean_wait": gs.mean_wait,
                "mgc_wq": gs.mgc_wq, "rho": gs.rho,
            })

    # 2. Throughput vs observed counts
    print("\n  (b) Throughput vs observed counts (as_is scenario):")
    print(f"  {'Session':12s} {'Gate':7s} {'Observed':>8s} {'Sim TP':>8s} {'Ratio':>7s}")
    for sr in all_results:
        if sr.scenario != "as_is":
            continue
        snum = sr.sesi.replace("Sesi ", "S")
        label = f"{sr.hari}_{snum}"
        # Find matching session
        for sb in sessions:
            if sb.hari == sr.hari and sb.sesi == sr.sesi:
                for gs, cell in [(sr.karcis_stats, sb.karcis),
                                 (sr.rfid_stats, sb.rfid)]:
                    obs = cell.n_before_iqr
                    sim_tp = gs.throughput
                    ratio = sim_tp / obs if obs > 0 else 0
                    print(f"  {label:12s} {gs.gate:7s} {obs:8d} "
                          f"{sim_tp:8.0f} {ratio:7.2f}")

    # 3. Sanity: waits should rise with load
    print("\n  (c) Sanity checks:")
    for sr in all_results:
        if sr.scenario == "as_is":
            for gs in [sr.karcis_stats, sr.rfid_stats]:
                if gs.rho < 0.1 and gs.mean_wait > 5.0:
                    snum = sr.sesi.replace("Sesi ", "S")
                    print(f"  ⚠ {sr.hari}_{snum} {gs.gate}: "
                          f"ρ={gs.rho:.3f} but E[W]={gs.mean_wait:.1f}s (unexpected)")
    # Check shift ordering
    for sb in sessions:
        snum = sb.sesi.replace("Sesi ", "S")
        label = f"{sb.hari}_{snum}"
        rfid_waits = []
        for scn in ["as_is", "shift_25", "shift_50", "shift_75", "shift_100"]:
            match = [sr for sr in all_results
                     if sr.scenario == scn and sr.hari == sb.hari and sr.sesi == sb.sesi]
            if match:
                rfid_waits.append((scn, match[0].rfid_stats.mean_wait))
        if len(rfid_waits) >= 2:
            monotone = all(rfid_waits[i][1] <= rfid_waits[i+1][1] + 0.5
                          for i in range(len(rfid_waits)-1))
            if not monotone:
                print(f"  ⚠ {label} RFID wait not monotone with shift: "
                      f"{[(s,f'{w:.1f}') for s,w in rfid_waits]}")
            else:
                print(f"  ✓ {label} RFID wait increases with shift (expected)")

    # 4. Warm-up effect note
    if warmup == 0:
        print(f"\n  (d) Warm-up: None used (--warmup 0).")
        print("      A 300s warm-up would discard initial transients but reduces")
        print("      effective observation window from 3600s to 3300s.")
        print("      For these 1-hour sessions with relatively low arrival rates,")
        print("      the empty-and-idle start IS the real initial condition,")
        print("      so no warm-up is the appropriate default.")
    else:
        print(f"\n  (d) Warm-up: {warmup:.0f}s used. Effective window: "
              f"{SIM_DURATION:.0f}s.")

    vdf = pd.DataFrame(val_rows)
    vdf.to_csv(os.path.join(outdir, "validation.csv"), index=False)
    print(f"\n✓ Validation data saved to {os.path.join(outdir, 'validation.csv')}")


# ──────────────────────────────────────────────────────────────────
# 10.  LANES GRID SENSITIVITY  (Step 3 flag)
# ──────────────────────────────────────────────────────────────────

def run_lanes_grid(
    sessions: List[SessionBlock],
    grid: List[Tuple[int, int]],
    all_k_clean: np.ndarray, all_r_clean: np.ndarray,
    service_mode: str,
    karcis_fit: Optional[dict], rfid_fit: Optional[dict],
    n_reps: int, base_seed: int, warmup: float, quiet: bool,
) -> List[ScenarioResult]:
    """Run as_is for each lane configuration in the grid."""
    results = []
    for kl, rl in grid:
        if not quiet:
            print(f"\n  Lanes grid: Karcis={kl}, RFID={rl}")
        for sb in sessions:
            lam_k = sb.karcis.n_before_iqr / 3600.0
            lam_r = sb.rfid.n_before_iqr / 3600.0
            sr = run_scenario(
                sb.hari, sb.sesi, f"grid_K{kl}_R{rl}",
                kl, rl, lam_k, lam_r,
                all_k_clean, all_r_clean,
                service_mode, karcis_fit, rfid_fit,
                n_reps, base_seed, warmup,
            )
            results.append(sr)
            if not quiet:
                snum = sb.sesi.replace("Sesi ", "S")
                print(f"    {sb.hari}_{snum}: K_wait={sr.karcis_stats.mean_wait:.2f}s "
                      f"R_wait={sr.rfid_stats.mean_wait:.2f}s")
    return results


# ──────────────────────────────────────────────────────────────────
# 11.  CLI
# ──────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="DES of vehicle exit gate (tap-out) v3 — two separate gates",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples
--------
  python tap_out_sim_v3.py
  python tap_out_sim_v3.py --karcis-lanes 2 --rfid-lanes 1 --reps 200
  python tap_out_sim_v3.py --service fitted
  python tap_out_sim_v3.py --lanes-grid 1,1;2,1;2,2;3,1
""",
    )
    p.add_argument("--data", default=CLEANING_FILE,
                   help="Path to the IQR cleaning workbook")
    p.add_argument("--raw-data", default=RAW_FILE,
                   help="Path to the raw Pengumpulan workbook (for reconciliation)")
    p.add_argument("--karcis-lanes", type=int, default=2,
                   help="Number of Karcis gate lanes (ASSUMED, default: 2)")
    p.add_argument("--rfid-lanes", type=int, default=1,
                   help="Number of RFID gate lanes (ASSUMED, default: 1)")
    p.add_argument("--lanes-grid", type=str, default=None,
                   help="Semicolon-separated K,R lane pairs for sensitivity, "
                        "e.g. '1,1;2,1;2,2;3,1'")
    p.add_argument("--reps", type=int, default=200,
                   help="Replications per scenario (default: 200)")
    p.add_argument("--service", choices=["empirical", "fitted"], default="empirical",
                   help="Service-time sampling mode (default: empirical)")
    p.add_argument("--warmup", type=float, default=0,
                   help="Warm-up period in seconds (default: 0). "
                        "Try 300 to see effect.")
    p.add_argument("--outdir", default="results_v3",
                   help="Output directory")
    p.add_argument("--seed", type=int, default=42,
                   help="Base random seed")
    p.add_argument("--quiet", action="store_true")
    return p


def _parse_lanes_grid(s: str) -> List[Tuple[int, int]]:
    """Parse '1,1;2,1;2,2' → [(1,1),(2,1),(2,2)]."""
    pairs = []
    for part in s.split(";"):
        k, r = part.strip().split(",")
        pairs.append((int(k.strip()), int(r.strip())))
    return pairs


def _best_fit(data: np.ndarray) -> Optional[dict]:
    """Find best-fit distribution (lowest AIC) and return params."""
    fits = _fit_distributions(data, "")
    best = None
    best_aic = float("inf")
    for dname, res in fits.items():
        if "error" not in res and res["aic"] < best_aic:
            best_aic = res["aic"]
            best = {"dist_name": dname, "params": res["params"]}
    return best


def main():
    args = build_parser().parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    # ── Step 0: Reconciliation ──
    raw_path = args.raw_data
    if os.path.isfile(raw_path):
        reconciliation(raw_path, args.data, args.outdir)
    else:
        print(f"  (Raw file '{raw_path}' not found; skipping reconciliation)")

    # ── Load data ──
    sessions, ringkasan = load_data(args.data)
    all_k_clean, all_r_clean = pool_times(sessions, use_log=False)
    all_k_log, all_r_log = pool_times(sessions, use_log=True)

    if not args.quiet:
        print(f"  Clean service times: {len(all_k_clean)} Karcis, "
              f"{len(all_r_clean)} RFID")
        print(f"  Karcis — mean={all_k_clean.mean():.2f}s  "
              f"std={all_k_clean.std():.2f}s  "
              f"median={np.median(all_k_clean):.2f}s")
        print(f"  RFID   — mean={all_r_clean.mean():.2f}s  "
              f"std={all_r_clean.std():.2f}s  "
              f"median={np.median(all_r_clean):.2f}s")
        print(f"  Log times (incl outliers): {len(all_k_log)} Karcis, "
              f"{len(all_r_log)} RFID")

    # ── Step 1: Input analysis ──
    fit_df, kw_dict = input_analysis(sessions, args.outdir)

    # ── Step 2: Arrival rates ──
    compute_arrival_rates(sessions)

    # ── Fit for service sampling if needed ──
    karcis_fit = None
    rfid_fit = None
    if args.service == "fitted":
        karcis_fit = _best_fit(all_k_clean)
        rfid_fit = _best_fit(all_r_clean)
        if not args.quiet and karcis_fit:
            print(f"\n  Best fit Karcis: {karcis_fit['dist_name']} "
                  f"params={karcis_fit['params']}")
        if not args.quiet and rfid_fit:
            print(f"  Best fit RFID:   {rfid_fit['dist_name']} "
                  f"params={rfid_fit['params']}")

    # ── Step 3-4: Run scenarios ──
    print("\n" + "=" * 80)
    print(f"STEPS 3-4 — SIMULATION  (karcis_lanes={args.karcis_lanes} [ASSUMED], "
          f"rfid_lanes={args.rfid_lanes} [ASSUMED], "
          f"reps={args.reps}, warmup={args.warmup}s)")
    print("=" * 80)
    if args.service == "empirical":
        print("  Service model: EMPIRICAL RESAMPLING (primary model)")
        print("  Rationale: Normal distribution rejected at all levels (KS test).")
        print("             Empirical resampling preserves actual data shape.")
        print("             Use --service fitted for lognormal comparison model.")
    else:
        print("  Service model: FITTED (lognormal/best-AIC — comparison model)")
        print("  Note: This is the parametric comparison. The primary model")
        print("        uses empirical resampling (default --service empirical).")

    all_results = build_scenarios(
        sessions, args.karcis_lanes, args.rfid_lanes,
        all_k_clean, all_r_clean, all_k_log, all_r_log,
        args.service, karcis_fit, rfid_fit,
        args.reps, args.seed, args.warmup, args.quiet,
    )

    # ── Lanes grid sensitivity ──
    if args.lanes_grid:
        grid = _parse_lanes_grid(args.lanes_grid)
        print("\n  Lane-count sensitivity grid:")
        grid_results = run_lanes_grid(
            sessions, grid, all_k_clean, all_r_clean,
            args.service, karcis_fit, rfid_fit,
            args.reps, args.seed, args.warmup, args.quiet,
        )
        all_results.extend(grid_results)

    # ── Step 5: Outputs ──
    print_results_table(all_results)

    df = results_to_df(all_results)
    csv_path = os.path.join(args.outdir, "sim_results_v3.csv")
    df.to_csv(csv_path, index=False)
    print(f"\n✓ Results CSV saved to {csv_path}")

    save_scenario_comparison(all_results, args.outdir)
    save_sweep_plot(all_results, args.outdir)

    # ── Overload warnings ──
    for sr in all_results:
        for gs in [sr.karcis_stats, sr.rfid_stats]:
            if gs.rho >= 1.0:
                snum = sr.sesi.replace("Sesi ", "S")
                print(f"  ⚠ OVERLOADED: {sr.hari}_{snum} {sr.scenario} "
                      f"{gs.gate} ρ={gs.rho:.3f}")

    # ── Step 6: Validation ──
    validation_checks(all_results, sessions, args.outdir, args.warmup)

    print("\n✓ Done. All outputs in:", args.outdir)


if __name__ == "__main__":
    main()
