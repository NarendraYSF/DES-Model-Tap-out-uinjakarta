"""
export_anim_data.py
--------------------
Reads the IQR-cleaned workbook and logo, produces anim_data.json,
then injects data + logo into animation_template.html to produce
the standalone animation.html.

Usage:
    python export_anim_data.py
"""

import json
import base64
import os
import sys

import pandas as pd

# ── Paths ────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLEANING_PATH = os.path.join(SCRIPT_DIR, "Hasil_Cleaning_Tap_Out_IQR_Final.xlsx")
LOGO_PATH = os.path.join(SCRIPT_DIR, "docs", "img", "logo.png")
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "animation_template.html")
JSON_PATH = os.path.join(SCRIPT_DIR, "anim_data.json")
HTML_PATH = os.path.join(SCRIPT_DIR, "animation.html")

# ── Constants (must match v3) ────────────────────────────────────
HARI_ORDER = ["Selasa", "Rabu"]
SESI_ORDER = ["Sesi 1", "Sesi 2", "Sesi 3"]
SIM_DURATION = 3600.0
DEFAULT_KARCIS_LANES = 2
DEFAULT_RFID_LANES = 1


def load_and_export():
    """Read Excel, build anim_data dict, write JSON."""

    # ── Read sheets ──────────────────────────────────────────────
    bersih = pd.read_excel(CLEANING_PATH, sheet_name="Dataset Bersih")
    ringkasan = pd.read_excel(CLEANING_PATH, sheet_name="Ringkasan IQR")

    # ── Pool service times across all Hari x Sesi (matching v3) ──
    mask_k = bersih["Metode"] == "Karcis"
    mask_r = bersih["Metode"] == "RFID"

    svc_karcis = bersih.loc[mask_k, "Durasi Numerik (detik)"].values.astype(float)
    svc_rfid = bersih.loc[mask_r, "Durasi Numerik (detik)"].values.astype(float)

    # ── Verify counts ────────────────────────────────────────────
    n_k = len(svc_karcis)
    n_r = len(svc_rfid)
    n_total = n_k + n_r
    print(f"Pooled service times: Karcis={n_k}, RFID={n_r}, Total={n_total}")
    assert n_total == 1584, f"Expected 1584 total, got {n_total}"
    assert n_r == 592, f"Expected 592 RFID, got {n_r}"
    assert n_k == 992, f"Expected 992 Karcis, got {n_k}"
    print("  Counts verified OK")

    # ── Build per-session arrival rate data ──────────────────────
    sessions = []
    for hari in HARI_ORDER:
        for sesi in SESI_ORDER:
            row_k = ringkasan[
                (ringkasan["Hari"] == hari)
                & (ringkasan["Sesi"] == sesi)
                & (ringkasan["Metode"] == "Karcis")
            ]
            row_r = ringkasan[
                (ringkasan["Hari"] == hari)
                & (ringkasan["Sesi"] == sesi)
                & (ringkasan["Metode"] == "RFID")
            ]
            n_k_before = int(row_k["N Valid Sebelum IQR"].iloc[0])
            n_r_before = int(row_r["N Valid Sebelum IQR"].iloc[0])

            snum = sesi.replace("Sesi ", "S")
            sessions.append({
                "hari": hari,
                "sesi": sesi,
                "label": f"{hari}_{snum}",
                "karcis": {
                    "n_before_iqr": n_k_before,
                    "lambda": round(n_k_before / SIM_DURATION, 6),
                },
                "rfid": {
                    "n_before_iqr": n_r_before,
                    "lambda": round(n_r_before / SIM_DURATION, 6),
                },
            })

    # Print arrival rate summary
    print("\nArrival rates (lambda = N_before_IQR / 3600):")
    for s in sessions:
        print(
            f"  {s['label']:12s}  "
            f"Karcis: N={s['karcis']['n_before_iqr']:4d}  "
            f"lam={s['karcis']['lambda']:.4f}  |  "
            f"RFID: N={s['rfid']['n_before_iqr']:4d}  "
            f"lam={s['rfid']['lambda']:.4f}"
        )

    # ── Assemble JSON ────────────────────────────────────────────
    anim_data = {
        "sessions": sessions,
        "service_times": {
            "karcis": [round(float(v), 2) for v in svc_karcis],
            "rfid": [round(float(v), 2) for v in svc_rfid],
        },
        "defaults": {
            "karcis_lanes": DEFAULT_KARCIS_LANES,
            "rfid_lanes": DEFAULT_RFID_LANES,
            "lanes_assumed": True,
        },
        "sim_duration": int(SIM_DURATION),
    }

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(anim_data, f, ensure_ascii=False)
    print(f"\nWrote {JSON_PATH} ({os.path.getsize(JSON_PATH):,} bytes)")

    return anim_data


def build_html(anim_data):
    """Inject data and logo into template, write animation.html."""

    if not os.path.exists(TEMPLATE_PATH):
        print(f"WARNING: {TEMPLATE_PATH} not found, skipping HTML build.")
        return

    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = f.read()

    # Encode logo
    with open(LOGO_PATH, "rb") as f:
        logo_b64 = base64.b64encode(f.read()).decode("ascii")

    # Inject data and logo
    data_json = json.dumps(anim_data, ensure_ascii=False)
    html = template.replace("/*__ANIM_DATA__*/null", data_json)
    html = html.replace("/*__LOGO_BASE64__*/", logo_b64)

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {HTML_PATH} ({os.path.getsize(HTML_PATH):,} bytes)")

    # Sanity checks
    assert "ANIM_DATA" in html, "Data injection failed"
    assert "data:image/png;base64" not in template or logo_b64 in html, \
        "Logo injection failed"
    print("HTML build OK")


def main():
    print("=" * 60)
    print("export_anim_data.py -- Data Export & HTML Build")
    print("=" * 60)
    anim_data = load_and_export()
    build_html(anim_data)
    print("\nDone.")


if __name__ == "__main__":
    main()
