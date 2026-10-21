#!/usr/bin/env python3
"""
Discrete-Event Simulation of a Vehicle Exit-Gate (Tap-Out) System  v2
=====================================================================
Uses SimPy to model vehicles arriving at parking-lot exit gates,
paying via Karcis (manual ticket) or RFID, and departing.

Data source: "Pengumpulan Data Pemodelan Tap Out.xlsx"
             Sheets: Selasa (Tuesday) and Rabu (Wednesday)
             3 one-hour sessions per day.

v2 changes
----------
- Per-session arrival rates (no averaging across day×session).
- Lane-count sensitivity (--lanes-list).
- 200 default replications.
- New "fixed_rfid" scenario (RFID tail capped at Karcis p-th percentile).
- M/G/c (Allen-Cunneen) analytical sanity check.
- Two-panel sweep plot + scenario comparison bar chart.

Author : auto-generated
Date   : 2026-10-15
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")  # non-interactive backend for CLI
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import simpy
from scipy import stats
from scipy.special import factorial as _factorial

warnings.filterwarnings("ignore", category=UserWarning)

# ──────────────────────────────────────────────────────────────────
# 1.  DATA LOADING & CLEANING
# ──────────────────────────────────────────────────────────────────

EXCEL_FILE = "Pengumpulan Data Pemodelan Tap Out.xlsx"

# Column layout (0-indexed): col 2/3 → Sesi 1 Karcis/RFID, …
SESSION_COLS = {
    1: {"karcis": 2, "rfid": 3},
    2: {"karcis": 4, "rfid": 5},
    3: {"karcis": 6, "rfid": 7},
}


def _parse_service_time(v) -> Optional[float]:
    """Convert a raw cell value to seconds (float).

    Handles:
      - Numeric values already in seconds (e.g. 12.8)
      - Text like "01:06.35" or "1:05.55" (mm:ss.xx → seconds)
      - String-typed numbers like "13.59"
    Returns None for unparseable values.
    """
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if ":" in s:
        try:
            parts = s.split(":")
            return float(parts[0]) * 60 + float(parts[1])
        except (ValueError, IndexError):
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _remove_outliers_iqr(arr: np.ndarray, factor: float = 3.0) -> np.ndarray:
    """Remove outliers outside [Q1 - factor*IQR, Q3 + factor*IQR].

    Uses a generous factor of 3.0 (vs the usual 1.5) because service-time
    distributions are right-skewed; we only want to remove clearly erroneous
    readings while preserving the natural tail.
    """
    q1, q3 = np.percentile(arr, [25, 75])
    iqr = q3 - q1
    lo = q1 - factor * iqr
    hi = q3 + factor * iqr
    mask = (arr >= lo) & (arr <= hi)
    return arr[mask]


@dataclass
class SessionData:
    """Cleaned service-time data for one day × one session."""

    day: str
    session: int
    karcis_times: np.ndarray
    rfid_times: np.ndarray

    @property
    def n_karcis(self) -> int:
        return len(self.karcis_times)

    @property
    def n_rfid(self) -> int:
        return len(self.rfid_times)

    @property
    def n_total(self) -> int:
        return self.n_karcis + self.n_rfid

    @property
    def p_rfid(self) -> float:
        return self.n_rfid / self.n_total if self.n_total > 0 else 0.0

    @property
    def arrival_rate(self) -> float:
        """Poisson arrival rate λ (vehicles per second)."""
        return self.n_total / 3600.0

    @property
    def label(self) -> str:
        return f"{self.day}_S{self.session}"


def load_data(excel_path: str) -> List[SessionData]:
    """Load and clean service-time data from the Excel workbook.

    Returns a list of SessionData (one per day × session = 6 total).
    """
    sessions: List[SessionData] = []
    for sheet in ["Selasa", "Rabu"]:
        df = pd.read_excel(excel_path, sheet_name=sheet, header=None)
        for snum, cols in SESSION_COLS.items():
            raw_k = df.iloc[4:, cols["karcis"]].dropna().tolist()
            raw_r = df.iloc[4:, cols["rfid"]].dropna().tolist()

            k_parsed = [_parse_service_time(v) for v in raw_k]
            r_parsed = [_parse_service_time(v) for v in raw_r]

            k_arr = np.array([x for x in k_parsed if x is not None and x > 0])
            r_arr = np.array([x for x in r_parsed if x is not None and x > 0])

            # Remove outliers
            if len(k_arr) > 5:
                k_arr = _remove_outliers_iqr(k_arr)
            if len(r_arr) > 5:
                r_arr = _remove_outliers_iqr(r_arr)

            sessions.append(SessionData(sheet, snum, k_arr, r_arr))
    return sessions


def pool_service_times(sessions: List[SessionData]):
    """Pool all service times across days and sessions.

    Returns (all_karcis, all_rfid).
    """
    all_k = np.concatenate([s.karcis_times for s in sessions])
    all_r = np.concatenate([s.rfid_times for s in sessions])
    return all_k, all_r


# ──────────────────────────────────────────────────────────────────
# 2.  LOGNORMAL FIT  &  RFID-CAP HELPER
# ──────────────────────────────────────────────────────────────────


def fit_lognormal(data: np.ndarray):
    """Fit a lognormal distribution and return (shape=σ, loc, scale=exp(μ))."""
    shape, loc, scale = stats.lognorm.fit(data, floc=0)
    return shape, loc, scale


def cap_rfid_times(rfid_data: np.ndarray, karcis_data: np.ndarray,
                   cap_pct: float) -> np.ndarray:
    """Return a copy of rfid_data with values capped at the given percentile
    of karcis_data.  Values above the cap are clipped (not removed)."""
    cap_val = np.percentile(karcis_data, cap_pct)
    return np.clip(rfid_data, None, cap_val)


# ──────────────────────────────────────────────────────────────────
# 3.  M/G/c  ANALYTICAL APPROXIMATION  (Allen-Cunneen)
# ──────────────────────────────────────────────────────────────────


def _erlang_c(c: int, rho_total: float) -> float:
    """Erlang-C probability that an arriving customer must wait (M/M/c).

    Parameters
    ----------
    c : int          – number of servers
    rho_total : float – total offered load  a = λ / μ  (NOT per-server ρ)
    """
    a = rho_total  # total offered load
    if a / c >= 1.0:
        return 1.0  # unstable
    # Compute P0 first
    sum_part = sum(a ** k / math.factorial(k) for k in range(c))
    last_term = (a ** c / math.factorial(c)) * (1.0 / (1.0 - a / c))
    p0 = 1.0 / (sum_part + last_term)
    c_prob = last_term * p0
    return min(c_prob, 1.0)


def mgc_mean_wait(lam: float, mean_svc: float, cv2_svc: float,
                  c: int) -> Tuple[float, float]:
    """Allen-Cunneen approximation for E[Wq] in an M/G/c queue.

    Returns (E[Wq], offered_load_per_server).

    The approximation is:
        E[Wq]_MG/c  ≈  E[Wq]_MM/c  ×  (1 + cv²) / 2
    where cv² = Var[S] / E[S]² is the squared coefficient of variation of
    service time.
    """
    mu = 1.0 / mean_svc
    a = lam / mu           # total offered load
    rho = a / c            # per-server utilisation
    if rho >= 1.0:
        return float("inf"), rho

    ec = _erlang_c(c, a)
    wq_mmc = ec / (c * mu * (1 - rho))       # E[Wq] for M/M/c
    wq_mgc = wq_mmc * (1 + cv2_svc) / 2.0    # Allen-Cunneen correction
    return wq_mgc, rho


# ──────────────────────────────────────────────────────────────────
# 4.  SIMPY SIMULATION
# ──────────────────────────────────────────────────────────────────

SIM_DURATION = 3600.0  # 1 hour (one session)
WARMUP = 300.0  # 5-minute warm-up (statistics discarded)


@dataclass
class SimResult:
    """Raw result of a single replication."""

    waits: list = field(default_factory=list)
    queue_lengths: list = field(default_factory=list)
    max_queue: int = 0
    vehicles_served: int = 0


def _sample_service(
    rng: np.random.Generator,
    is_rfid: bool,
    karcis_data: np.ndarray,
    rfid_data: np.ndarray,
    service_mode: str,
    lognorm_params: dict,
) -> float:
    """Sample a service time for one vehicle."""
    if service_mode == "empirical":
        pool = rfid_data if is_rfid else karcis_data
        return float(rng.choice(pool))
    else:  # lognormal
        key = "rfid" if is_rfid else "karcis"
        shape, loc, scale = lognorm_params[key]
        return float(stats.lognorm.rvs(shape, loc=loc, scale=scale,
                                       random_state=rng))


def _vehicle(
    env: simpy.Environment,
    gate: simpy.Resource,
    vid: int,
    rng: np.random.Generator,
    p_rfid: float,
    karcis_data: np.ndarray,
    rfid_data: np.ndarray,
    service_mode: str,
    lognorm_params: dict,
    result: SimResult,
):
    """SimPy process for a single vehicle."""
    arrive_time = env.now
    with gate.request() as req:
        yield req
        wait = env.now - arrive_time
        is_rfid = rng.random() < p_rfid
        svc = _sample_service(
            rng, is_rfid, karcis_data, rfid_data, service_mode, lognorm_params
        )
        yield env.timeout(svc)

        # Record stats only after warm-up
        if arrive_time >= WARMUP:
            result.waits.append(wait)
            result.vehicles_served += 1


def _queue_monitor(env: simpy.Environment, gate: simpy.Resource,
                   result: SimResult):
    """Record queue length every second after warm-up."""
    while True:
        yield env.timeout(1.0)
        qlen = len(gate.queue)
        if env.now >= WARMUP:
            result.queue_lengths.append(qlen)
            if qlen > result.max_queue:
                result.max_queue = qlen


def _arrivals(
    env: simpy.Environment,
    gate: simpy.Resource,
    rng: np.random.Generator,
    arrival_rate: float,
    p_rfid: float,
    karcis_data: np.ndarray,
    rfid_data: np.ndarray,
    service_mode: str,
    lognorm_params: dict,
    result: SimResult,
):
    """Poisson arrival generator."""
    vid = 0
    while True:
        iat = rng.exponential(1.0 / arrival_rate)
        yield env.timeout(iat)
        vid += 1
        env.process(
            _vehicle(
                env, gate, vid, rng, p_rfid,
                karcis_data, rfid_data, service_mode, lognorm_params, result,
            )
        )


def run_one_replication(
    seed: int,
    n_lanes: int,
    arrival_rate: float,
    p_rfid: float,
    karcis_data: np.ndarray,
    rfid_data: np.ndarray,
    service_mode: str,
    lognorm_params: dict,
) -> SimResult:
    """Run a single simulation replication and return results."""
    rng = np.random.default_rng(seed)
    env = simpy.Environment()
    gate = simpy.Resource(env, capacity=n_lanes)
    result = SimResult()

    env.process(
        _arrivals(
            env, gate, rng, arrival_rate, p_rfid,
            karcis_data, rfid_data, service_mode, lognorm_params, result,
        )
    )
    env.process(_queue_monitor(env, gate, result))
    env.run(until=SIM_DURATION)
    return result


# ──────────────────────────────────────────────────────────────────
# 5.  MULTI-REPLICATION STATISTICS
# ──────────────────────────────────────────────────────────────────


@dataclass
class ScenarioStats:
    """Aggregated statistics across replications for one scenario."""

    day: str
    session: int
    scenario: str
    p_rfid: float
    n_lanes: int
    arrival_rate: float
    service_mode: str
    n_reps: int
    mean_wait: float
    ci_mean_wait: tuple
    p95_wait: float
    ci_p95_wait: tuple
    avg_queue: float
    ci_avg_queue: tuple
    max_queue: float
    ci_max_queue: tuple
    offered_load: float
    mgc_mean_wait: float  # analytical approximation


def _ci95(data: np.ndarray):
    """95 % confidence interval (mean ± t × se)."""
    n = len(data)
    if n < 2:
        m = data[0] if n == 1 else 0.0
        return m, (m, m)
    m = float(np.mean(data))
    se = float(stats.sem(data))
    h = se * stats.t.ppf(0.975, n - 1)
    return m, (m - h, m + h)


def _compute_offered_load(p_rfid: float, karcis_data: np.ndarray,
                          rfid_data: np.ndarray, arrival_rate: float,
                          n_lanes: int) -> float:
    mean_svc = p_rfid * np.mean(rfid_data) + (1 - p_rfid) * np.mean(karcis_data)
    return arrival_rate * mean_svc / n_lanes


def _compute_mgc(p_rfid: float, karcis_data: np.ndarray,
                 rfid_data: np.ndarray, arrival_rate: float,
                 n_lanes: int) -> float:
    """Allen-Cunneen E[Wq] for the mixed service-time distribution."""
    mean_k, var_k = float(np.mean(karcis_data)), float(np.var(karcis_data))
    mean_r, var_r = float(np.mean(rfid_data)), float(np.var(rfid_data))
    p = p_rfid
    # Mixture moments
    mean_s = (1 - p) * mean_k + p * mean_r
    # E[S^2] for a mixture = (1-p)*E[Sk^2] + p*E[Sr^2]
    e_s2 = (1 - p) * (var_k + mean_k ** 2) + p * (var_r + mean_r ** 2)
    var_s = e_s2 - mean_s ** 2
    cv2 = var_s / (mean_s ** 2) if mean_s > 0 else 0.0
    wq, _ = mgc_mean_wait(arrival_rate, mean_s, cv2, n_lanes)
    return wq


def run_scenario(
    day: str,
    session: int,
    label: str,
    p_rfid: float,
    n_lanes: int,
    arrival_rate: float,
    karcis_data: np.ndarray,
    rfid_data: np.ndarray,
    service_mode: str,
    lognorm_params: dict,
    n_reps: int,
    base_seed: int = 42,
) -> ScenarioStats:
    """Run n_reps replications and return aggregated statistics."""
    rep_mean_wait = []
    rep_p95_wait = []
    rep_avg_q = []
    rep_max_q = []

    for i in range(n_reps):
        res = run_one_replication(
            seed=base_seed + i,
            n_lanes=n_lanes,
            arrival_rate=arrival_rate,
            p_rfid=p_rfid,
            karcis_data=karcis_data,
            rfid_data=rfid_data,
            service_mode=service_mode,
            lognorm_params=lognorm_params,
        )
        if res.waits:
            rep_mean_wait.append(np.mean(res.waits))
            rep_p95_wait.append(np.percentile(res.waits, 95))
        else:
            rep_mean_wait.append(0.0)
            rep_p95_wait.append(0.0)
        if res.queue_lengths:
            rep_avg_q.append(np.mean(res.queue_lengths))
        else:
            rep_avg_q.append(0.0)
        rep_max_q.append(float(res.max_queue))

    rep_mean_wait = np.array(rep_mean_wait)
    rep_p95_wait = np.array(rep_p95_wait)
    rep_avg_q = np.array(rep_avg_q)
    rep_max_q = np.array(rep_max_q)

    mw, ci_mw = _ci95(rep_mean_wait)
    pw, ci_pw = _ci95(rep_p95_wait)
    aq, ci_aq = _ci95(rep_avg_q)
    mq, ci_mq = _ci95(rep_max_q)

    rho = _compute_offered_load(p_rfid, karcis_data, rfid_data,
                                arrival_rate, n_lanes)
    mgc_wq = _compute_mgc(p_rfid, karcis_data, rfid_data,
                           arrival_rate, n_lanes)

    return ScenarioStats(
        day=day, session=session, scenario=label,
        p_rfid=p_rfid, n_lanes=n_lanes, arrival_rate=arrival_rate,
        service_mode=service_mode, n_reps=n_reps,
        mean_wait=mw, ci_mean_wait=ci_mw,
        p95_wait=pw, ci_p95_wait=ci_pw,
        avg_queue=aq, ci_avg_queue=ci_aq,
        max_queue=mq, ci_max_queue=ci_mq,
        offered_load=rho, mgc_mean_wait=mgc_wq,
    )


# ──────────────────────────────────────────────────────────────────
# 6.  REPORTING
# ──────────────────────────────────────────────────────────────────


def stats_to_row(s: ScenarioStats) -> dict:
    return {
        "day": s.day,
        "session": s.session,
        "lanes": s.n_lanes,
        "scenario": s.scenario,
        "p_rfid": f"{s.p_rfid:.3f}",
        "arrival_rate": f"{s.arrival_rate:.4f}",
        "service_mode": s.service_mode,
        "n_reps": s.n_reps,
        "mean_wait_s": f"{s.mean_wait:.2f}",
        "mean_wait_CI_lo": f"{s.ci_mean_wait[0]:.2f}",
        "mean_wait_CI_hi": f"{s.ci_mean_wait[1]:.2f}",
        "p95_wait_s": f"{s.p95_wait:.2f}",
        "p95_wait_CI_lo": f"{s.ci_p95_wait[0]:.2f}",
        "p95_wait_CI_hi": f"{s.ci_p95_wait[1]:.2f}",
        "avg_queue_len": f"{s.avg_queue:.2f}",
        "avg_queue_CI_lo": f"{s.ci_avg_queue[0]:.2f}",
        "avg_queue_CI_hi": f"{s.ci_avg_queue[1]:.2f}",
        "max_queue_len": f"{s.max_queue:.1f}",
        "max_queue_CI_lo": f"{s.ci_max_queue[0]:.1f}",
        "max_queue_CI_hi": f"{s.ci_max_queue[1]:.1f}",
        "offered_load_rho": f"{s.offered_load:.4f}",
        "mgc_mean_wait_s": f"{s.mgc_mean_wait:.2f}" if np.isfinite(s.mgc_mean_wait) else "inf",
    }


def print_session_table(day: str, session: int,
                        results: List[ScenarioStats], quiet: bool):
    """Print a compact per-session results table."""
    hdr = (f"\n{'─'*100}\n"
           f"  {day} Sesi {session}  │  "
           f"λ = {results[0].arrival_rate:.4f} veh/s  "
           f"({results[0].arrival_rate*3600:.0f} veh/hr)  │  "
           f"lanes = {results[0].n_lanes}")
    print(hdr)
    print(f"  {'Scenario':<16} {'p':<6} {'ρ':<7} "
          f"{'E[W]':>7} {'CI95':>17} "
          f"{'P95(W)':>7} {'AvgQ':>5} {'MaxQ':>5} "
          f"{'M/G/c':>7} {'Δ%':>6}")
    for s in results:
        ci = f"[{s.ci_mean_wait[0]:.1f},{s.ci_mean_wait[1]:.1f}]"
        if np.isfinite(s.mgc_mean_wait) and s.mgc_mean_wait > 0:
            delta = (s.mean_wait - s.mgc_mean_wait) / s.mgc_mean_wait * 100
            mgc_str = f"{s.mgc_mean_wait:.2f}"
            delta_str = f"{delta:+.0f}%"
        else:
            mgc_str = "inf" if not np.isfinite(s.mgc_mean_wait) else f"{s.mgc_mean_wait:.2f}"
            delta_str = "—"
        warn = " ⚠ OVERLOADED" if s.offered_load >= 1.0 else ""
        print(f"  {s.scenario:<16} {s.p_rfid:<6.3f} {s.offered_load:<7.3f} "
              f"{s.mean_wait:>7.2f} {ci:>17} "
              f"{s.p95_wait:>7.2f} {s.avg_queue:>5.2f} {s.max_queue:>5.1f} "
              f"{mgc_str:>7} {delta_str:>6}{warn}")


def save_csv(all_stats: List[ScenarioStats], path: str):
    rows = [stats_to_row(s) for s in all_stats]
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False)
    print(f"\n✓ Results saved to {path}")


# ──────────────────────────────────────────────────────────────────
# 7.  PLOTS
# ──────────────────────────────────────────────────────────────────

# Colour palette – one per day×session
_COLORS = {
    "Selasa_S1": "#2563eb", "Selasa_S2": "#7c3aed", "Selasa_S3": "#db2777",
    "Rabu_S1": "#059669", "Rabu_S2": "#d97706", "Rabu_S3": "#dc2626",
}


def _session_key(s: ScenarioStats) -> str:
    return f"{s.day}_S{s.session}"


def save_sweep_plot(all_stats: List[ScenarioStats], path: str):
    """Two-panel plot: mean wait and p95 wait vs p(RFID), one curve per
    day×session.  Vertical markers show each session's observed mix."""

    sweep = [s for s in all_stats if s.scenario.startswith("sweep_")]
    if not sweep:
        print("  (no sweep data to plot)")
        return

    # Group sweep results by day×session key
    groups: Dict[str, List[ScenarioStats]] = {}
    for s in sweep:
        key = _session_key(s)
        groups.setdefault(key, []).append(s)
    # Sort each group by p_rfid
    for g in groups.values():
        g.sort(key=lambda x: x.p_rfid)

    # Collect observed-mix markers
    obs_markers: Dict[str, ScenarioStats] = {}
    for s in all_stats:
        if s.scenario == "observed_mix":
            obs_markers[_session_key(s)] = s

    # Determine whether we need log scale
    all_means = [s.mean_wait for s in sweep if s.mean_wait > 0]
    use_log = (max(all_means) / max(min(all_means), 0.01)) > 20 if all_means else False

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5.5), sharey=False)

    for key, grp in sorted(groups.items()):
        color = _COLORS.get(key, "#888888")
        ps = [s.p_rfid for s in grp]

        # Panel 1 – mean wait
        mw = [s.mean_wait for s in grp]
        lo_mw = [s.ci_mean_wait[0] for s in grp]
        hi_mw = [s.ci_mean_wait[1] for s in grp]
        ax1.plot(ps, mw, "o-", color=color, linewidth=1.5, markersize=3,
                 label=key)
        ax1.fill_between(ps, lo_mw, hi_mw, alpha=0.12, color=color)

        # Panel 2 – p95 wait
        pw = [s.p95_wait for s in grp]
        lo_pw = [s.ci_p95_wait[0] for s in grp]
        hi_pw = [s.ci_p95_wait[1] for s in grp]
        ax2.plot(ps, pw, "o-", color=color, linewidth=1.5, markersize=3,
                 label=key)
        ax2.fill_between(ps, lo_pw, hi_pw, alpha=0.12, color=color)

        # Observed-mix vertical marker
        if key in obs_markers:
            obs = obs_markers[key]
            ax1.axvline(obs.p_rfid, color=color, linestyle=":", linewidth=0.8,
                        alpha=0.6)
            ax2.axvline(obs.p_rfid, color=color, linestyle=":", linewidth=0.8,
                        alpha=0.6)

    for ax, ylabel, title in [
        (ax1, "Mean Wait (s)", "Mean Waiting Time vs p(RFID)"),
        (ax2, "P95 Wait (s)", "95th-Percentile Wait vs p(RFID)"),
    ]:
        ax.set_xlabel("Probability of RFID (p)")
        ax.set_ylabel(ylabel)
        ax.set_title(title, fontsize=11)
        ax.legend(fontsize=8, ncol=2)
        ax.grid(True, alpha=0.25)
        if use_log:
            ax.set_yscale("log")

    fig.suptitle(f"Exit Gate DES – Sweep  (lanes={sweep[0].n_lanes}, "
                 f"reps={sweep[0].n_reps})", fontsize=13, y=1.02)
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"✓ Sweep plot saved to {path}")


def save_scenario_comparison_plot(all_stats: List[ScenarioStats], path: str):
    """Grouped bar chart comparing the four named scenarios by day×session."""

    SCENARIOS = ["observed_mix", "full_karcis", "full_rfid", "fixed_rfid"]
    scenario_colors = {
        "observed_mix": "#2563eb",
        "full_karcis": "#ef4444",
        "full_rfid": "#10b981",
        "fixed_rfid": "#f59e0b",
    }

    # Collect named-scenario results grouped by day×session
    groups: Dict[str, Dict[str, ScenarioStats]] = {}
    for s in all_stats:
        if s.scenario in SCENARIOS:
            key = _session_key(s)
            groups.setdefault(key, {})[s.scenario] = s

    if not groups:
        return

    keys = sorted(groups.keys())
    n_groups = len(keys)
    n_bars = len(SCENARIOS)
    bar_width = 0.18
    x = np.arange(n_groups)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5.5))

    for idx, scn in enumerate(SCENARIOS):
        means = []
        p95s = []
        ci_lo_m = []
        ci_hi_m = []
        ci_lo_p = []
        ci_hi_p = []
        for k in keys:
            s = groups[k].get(scn)
            if s:
                means.append(s.mean_wait)
                p95s.append(s.p95_wait)
                ci_lo_m.append(s.mean_wait - s.ci_mean_wait[0])
                ci_hi_m.append(s.ci_mean_wait[1] - s.mean_wait)
                ci_lo_p.append(s.p95_wait - s.ci_p95_wait[0])
                ci_hi_p.append(s.ci_p95_wait[1] - s.p95_wait)
            else:
                means.append(0)
                p95s.append(0)
                ci_lo_m.append(0)
                ci_hi_m.append(0)
                ci_lo_p.append(0)
                ci_hi_p.append(0)

        offset = (idx - n_bars / 2 + 0.5) * bar_width
        color = scenario_colors.get(scn, "#888")
        ax1.bar(x + offset, means, bar_width, label=scn, color=color,
                yerr=[ci_lo_m, ci_hi_m], capsize=2, error_kw={"linewidth": 0.8})
        ax2.bar(x + offset, p95s, bar_width, label=scn, color=color,
                yerr=[ci_lo_p, ci_hi_p], capsize=2, error_kw={"linewidth": 0.8})

    for ax, title in [(ax1, "Mean Wait by Scenario"),
                      (ax2, "P95 Wait by Scenario")]:
        ax.set_xticks(x)
        ax.set_xticklabels(keys, fontsize=9)
        ax.set_ylabel("Wait (s)")
        ax.set_title(title, fontsize=11)
        ax.legend(fontsize=8)
        ax.grid(axis="y", alpha=0.25)

    fig.suptitle(f"Scenario Comparison  (lanes={all_stats[0].n_lanes}, "
                 f"reps={all_stats[0].n_reps})", fontsize=13, y=1.02)
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"✓ Scenario comparison plot saved to {path}")


# ──────────────────────────────────────────────────────────────────
# 8.  CLI ENTRY POINT
# ──────────────────────────────────────────────────────────────────


def _parse_lanes_list(s: str) -> List[int]:
    """Parse a comma-separated list of integers."""
    return [int(x.strip()) for x in s.split(",")]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Discrete-Event Simulation of a parking exit gate "
                    "(tap-out) – v2 with per-session rates & lane sweeps.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples
--------
  python tap_out_sim.py                              # 2 lanes, 200 reps
  python tap_out_sim.py --lanes 3 --reps 50          # 3 lanes, 50 reps
  python tap_out_sim.py --lanes-list 1,2,3,4         # compare lane counts
  python tap_out_sim.py --service lognormal           # fit lognormal
  python tap_out_sim.py --rfid-cap-pct 80             # cap RFID at Karcis P80
""",
    )
    p.add_argument("--data", default=EXCEL_FILE,
                   help="Path to the Excel data file")
    p.add_argument("--lanes", type=int, default=2,
                   help="Number of exit gate lanes (default: 2)")
    p.add_argument("--lanes-list", type=str, default=None,
                   help="Comma-separated lane counts to compare, e.g. 1,2,3,4 "
                        "(overrides --lanes)")
    p.add_argument("--reps", type=int, default=200,
                   help="Replications per scenario (default: 200)")
    p.add_argument("--service",
                   choices=["empirical", "lognormal"], default="empirical",
                   help="Service-time sampling (default: empirical)")
    p.add_argument("--sweep-steps", type=int, default=11,
                   help="Steps in p_rfid sweep 0→1 (default: 11)")
    p.add_argument("--rfid-cap-pct", type=float, default=90.0,
                   help="Percentile of Karcis data used to cap RFID times "
                        "in the fixed_rfid scenario (default: 90)")
    p.add_argument("--output-csv", default="sim_results.csv",
                   help="Output CSV file")
    p.add_argument("--output-sweep-plot", default="sweep_wait_vs_p.png",
                   help="Sweep plot file")
    p.add_argument("--output-scenario-plot", default="scenario_comparison.png",
                   help="Scenario comparison plot file")
    p.add_argument("--seed", type=int, default=42,
                   help="Base random seed")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress progress messages")
    return p


def main():
    args = build_parser().parse_args()

    # ── Resolve lane counts ──
    if args.lanes_list:
        lanes_list = _parse_lanes_list(args.lanes_list)
    else:
        lanes_list = [args.lanes]

    # ── Load data ──
    data_path = args.data
    if not os.path.isfile(data_path):
        alt = data_path.replace("_", " ")
        if os.path.isfile(alt):
            data_path = alt
        else:
            print(f"ERROR: Cannot find data file: {data_path}", file=sys.stderr)
            sys.exit(1)

    if not args.quiet:
        print(f"Loading data from: {data_path}")

    sessions = load_data(data_path)
    all_k, all_r = pool_service_times(sessions)

    # Capped RFID data for fixed_rfid scenario
    rfid_capped = cap_rfid_times(all_r, all_k, args.rfid_cap_pct)

    if not args.quiet:
        print(f"  Pooled service times: {len(all_k)} Karcis, {len(all_r)} RFID")
        print(f"  Karcis  – mean={all_k.mean():.2f}s  std={all_k.std():.2f}s")
        print(f"  RFID    – mean={all_r.mean():.2f}s  std={all_r.std():.2f}s")
        cap_val = np.percentile(all_k, args.rfid_cap_pct)
        print(f"  RFID (capped at Karcis P{args.rfid_cap_pct:.0f}={cap_val:.1f}s)"
              f" – mean={rfid_capped.mean():.2f}s  std={rfid_capped.std():.2f}s")
        print(f"  Sessions: {len(sessions)}")
        for sd in sessions:
            print(f"    {sd.label}: n={sd.n_total}, λ={sd.arrival_rate:.4f} "
                  f"({sd.n_total} veh/hr), p_rfid={sd.p_rfid:.3f}")
        print(f"  Lane counts to test: {lanes_list}")
        print(f"  Reps={args.reps}, Service={args.service}, Seed={args.seed}")

    # ── Fit lognormal if needed ──
    lognorm_params: dict = {}
    lognorm_params_capped: dict = {}
    if args.service == "lognormal":
        sh_k, lo_k, sc_k = fit_lognormal(all_k)
        sh_r, lo_r, sc_r = fit_lognormal(all_r)
        sh_rc, lo_rc, sc_rc = fit_lognormal(rfid_capped)
        lognorm_params = {"karcis": (sh_k, lo_k, sc_k),
                          "rfid": (sh_r, lo_r, sc_r)}
        lognorm_params_capped = {"karcis": (sh_k, lo_k, sc_k),
                                 "rfid": (sh_rc, lo_rc, sc_rc)}
        if not args.quiet:
            print(f"  Lognormal Karcis: σ={sh_k:.3f}, μ={np.log(sc_k):.3f}")
            print(f"  Lognormal RFID:   σ={sh_r:.3f}, μ={np.log(sc_r):.3f}")
            print(f"  Lognormal RFID(cap): σ={sh_rc:.3f}, μ={np.log(sc_rc):.3f}")

    # ── Run all scenarios ──
    all_stats: List[ScenarioStats] = []
    total_combos = (len(sessions) * len(lanes_list)
                    * (4 + args.sweep_steps))  # named + sweep
    done = 0

    for n_lanes in lanes_list:
        for sd in sessions:
            lam = sd.arrival_rate
            obs_p = sd.p_rfid

            # Named scenarios: (label, p_rfid, rfid_data_to_use, lognorm_to_use)
            named = [
                ("observed_mix", obs_p, all_r, lognorm_params),
                ("full_karcis", 0.0, all_r, lognorm_params),
                ("full_rfid", 1.0, all_r, lognorm_params),
                ("fixed_rfid", obs_p, rfid_capped, lognorm_params_capped),
            ]

            session_results: List[ScenarioStats] = []

            for label, p, rfid_d, lnp in named:
                done += 1
                if not args.quiet:
                    pct = done / total_combos * 100
                    print(f"\r  [{pct:5.1f}%]  lanes={n_lanes}  {sd.label}  "
                          f"{label:<16}", end="", flush=True)
                s = run_scenario(
                    day=sd.day, session=sd.session, label=label,
                    p_rfid=p, n_lanes=n_lanes, arrival_rate=lam,
                    karcis_data=all_k, rfid_data=rfid_d,
                    service_mode=args.service, lognorm_params=lnp,
                    n_reps=args.reps, base_seed=args.seed,
                )
                session_results.append(s)
                all_stats.append(s)

            # Sweep
            sweep_ps = np.linspace(0, 1, args.sweep_steps)
            for p in sweep_ps:
                done += 1
                if not args.quiet:
                    pct = done / total_combos * 100
                    print(f"\r  [{pct:5.1f}%]  lanes={n_lanes}  {sd.label}  "
                          f"sweep p={p:.2f}    ", end="", flush=True)
                s = run_scenario(
                    day=sd.day, session=sd.session,
                    label=f"sweep_p{p:.2f}",
                    p_rfid=p, n_lanes=n_lanes, arrival_rate=lam,
                    karcis_data=all_k, rfid_data=all_r,
                    service_mode=args.service, lognorm_params=lognorm_params,
                    n_reps=args.reps, base_seed=args.seed,
                )
                all_stats.append(s)

            # Print per-session table
            if not args.quiet:
                print()  # newline after progress
                print_session_table(sd.day, sd.session, session_results,
                                    args.quiet)

    # ── Overload warnings ──
    overloaded = [s for s in all_stats if s.offered_load >= 1.0]
    if overloaded:
        print(f"\n⚠  WARNING: {len(overloaded)} scenario(s) have "
              f"offered load ρ ≥ 1.0 (system unstable):")
        for s in overloaded:
            print(f"    {s.day} S{s.session} {s.scenario} "
                  f"lanes={s.n_lanes}  ρ={s.offered_load:.3f}")

    # ── Save outputs ──
    save_csv(all_stats, args.output_csv)

    # For plots, use only the first lane count (or default) to avoid clutter
    plot_lanes = lanes_list[0]
    plot_stats = [s for s in all_stats if s.n_lanes == plot_lanes]
    save_sweep_plot(plot_stats, args.output_sweep_plot)
    save_scenario_comparison_plot(plot_stats, args.output_scenario_plot)

    print("\nDone.")


if __name__ == "__main__":
    main()
