"""
Stress test 04: numpy/scipy reimplementation of the measurement tool. Same measurement
definitions as ECGAnalysisTool (lead II; PR = P-onset->R-onset; QRS = Q-peak->S-peak;
QT = Q-peak->T-offset; per-beat Bazett QTc; +1-sample convention; same median filters),
different detection algorithms (Pan-Tompkins R peaks, slope-threshold onsets,
tangent-method T offset). Output CSV is a drop-in for the committed measurements CSV.

Usage: python stage7_measure_numpy.py --ecg-dir <dir of .mat files> --out-csv <path> [--limit N]
"""
import argparse
import glob
import math
import os

import numpy as np
import pandas as pd
import scipy.io
from scipy.signal import butter, filtfilt, find_peaks

FS = 500


def bandpass(x, lo, hi, fs=FS, order=2):
    b, a = butter(order, [lo / (fs / 2), hi / (fs / 2)], btype="band")
    return filtfilt(b, a, x)


def lowpass(x, hi, fs=FS, order=2):
    b, a = butter(order, hi / (fs / 2), btype="low")
    return filtfilt(b, a, x)


def pan_tompkins_r_peaks(x, fs=FS):
    """Classic Pan-Tompkins: bandpass -> derivative -> square -> MWI -> threshold."""
    bp = bandpass(x, 5, 15, fs)
    deriv = np.gradient(bp)
    sq = deriv ** 2
    win = int(0.150 * fs)
    mwi = np.convolve(sq, np.ones(win) / win, mode="same")

    thr = 0.35 * np.quantile(mwi, 0.99)
    cand, _ = find_peaks(mwi, height=thr, distance=int(0.200 * fs))
    if len(cand) == 0:
        return np.array([], dtype=int)

    # refine each detection to the local |bp| maximum
    half = int(0.075 * fs)
    peaks = []
    for c in cand:
        s, e = max(0, c - half), min(len(bp), c + half)
        peaks.append(s + int(np.argmax(np.abs(bp[s:e]))))
    peaks = np.unique(peaks)
    keep = [peaks[0]]
    for p in peaks[1:]:
        if p - keep[-1] >= int(0.200 * fs):
            keep.append(p)
        elif np.abs(bp[p]) > np.abs(bp[keep[-1]]):
            keep[-1] = p
    return np.array(keep, dtype=int)


def measure_record(sig, fs=FS):
    """sig: lead II 1-D array. Returns dict of the four features (np.nan on failure)."""
    out = {"Heart_Rate": np.nan, "PR_Interval_ms": np.nan,
           "QRS_Duration_ms": np.nan, "QTc_ms": np.nan}
    if np.all(sig == 0) or np.all(np.isnan(sig)):
        return out
    clean = bandpass(sig.astype(np.float64), 0.5, 40, fs)
    r_peaks = pan_tompkins_r_peaks(clean, fs)
    if len(r_peaks) < 7:  # reference tool's minimum
        return out

    w80 = int(0.080 * fs)
    slow = lowpass(clean, 25, fs)
    grad = np.gradient(clean)

    q_peaks, s_peaks, r_onsets, p_onsets, t_offsets = [], [], [], [], []
    for k, r in enumerate(r_peaks):
        qs, qe = max(0, r - w80), r
        ss, se = r + 1, min(len(clean), r + 1 + w80)
        if qe <= qs or se <= ss:
            q_peaks.append(np.nan); s_peaks.append(np.nan)
            r_onsets.append(np.nan); p_onsets.append(np.nan); t_offsets.append(np.nan)
            continue
        q = qs + int(np.argmin(clean[qs:qe]))
        s = ss + int(np.argmin(clean[ss:se]))
        q_peaks.append(q); s_peaks.append(s)

        # R onset: walk back from Q until |slope| < 10% of the QRS max slope
        qrs_slope = np.max(np.abs(grad[q:s + 1])) if s > q else np.nan
        onset = np.nan
        if not np.isnan(qrs_slope):
            j = q
            lim = max(0, q - int(0.100 * fs))
            while j > lim:
                if abs(grad[j]) < 0.10 * qrs_slope:
                    onset = j
                    break
                j -= 1
        r_onsets.append(onset)

        # P peak in [-300, -80] ms before R onset; onset = backward crossing under 25% of prominence
        p_on = np.nan
        base = onset if not np.isnan(onset) else q
        if not np.isnan(base):
            ps, pe = int(max(0, base - 0.300 * fs)), int(max(0, base - 0.080 * fs))
            if pe - ps > 5:
                seg = slow[ps:pe]
                pp = ps + int(np.argmax(seg))
                prom = slow[pp] - np.min(seg)
                if prom > 0:
                    thr_amp = np.min(seg) + 0.25 * prom
                    j = pp
                    while j > ps:
                        if slow[j] < thr_amp:
                            p_on = j
                            break
                        j -= 1
        p_onsets.append(p_on)

        # T peak = extremum after S; offset by the tangent method
        t_off = np.nan
        rr_next = (r_peaks[k + 1] - r) if k + 1 < len(r_peaks) else int(0.8 * fs)
        ts = s + int(0.080 * fs)
        te = min(len(clean), r + int(min(0.420 * fs, 0.6 * rr_next)))
        if te - ts > 10:
            seg = slow[ts:te]
            tp = ts + int(np.argmax(np.abs(seg - np.median(seg))))
            de = min(len(clean) - 1, tp + int(0.120 * fs))
            if de - tp > 3:
                gseg = np.gradient(slow[tp:de + 1])
                sign = -1.0 if slow[tp] >= np.median(seg) else 1.0
                rel = sign * gseg
                mi = tp + int(np.argmin(rel))
                slope = np.gradient(slow)[mi]
                if abs(slope) > 1e-9:
                    baseline = np.median(seg)
                    t_off_f = mi + (baseline - slow[mi]) / slope
                    if tp < t_off_f < te + 0.200 * fs:
                        t_off = t_off_f
        t_offsets.append(t_off)

    r_peaks_f = r_peaks.astype(np.float64)
    q_peaks = np.array(q_peaks, dtype=np.float64)
    s_peaks = np.array(s_peaks, dtype=np.float64)
    r_onsets = np.array(r_onsets, dtype=np.float64)
    p_onsets = np.array(p_onsets, dtype=np.float64)
    t_offsets = np.array(t_offsets, dtype=np.float64)

    def safe_median(vals, lo, hi):
        arr = np.array(vals, dtype=np.float64)
        arr = arr[~np.isnan(arr)]
        arr = arr[(arr >= lo) & (arr <= hi)]
        return np.median(arr) if arr.size else np.nan

    # The four features below follow the reference's positional beat pairing.
    rr_ms = [(r_peaks_f[j + 1] - r_peaks_f[j]) / fs * 1000
             for j in range(len(r_peaks_f) - 1)
             if (r_peaks_f[j + 1] - r_peaks_f[j]) / fs < 2.0]
    med_rr = safe_median(rr_ms, 300, 2000)
    if not np.isnan(med_rr) and med_rr > 0:
        out["Heart_Rate"] = round(60 / (med_rr / 1000), 2)

    pr = [((r_onsets[i] - p_onsets[i] + 1) / fs) * 1000
          for i in range(min(len(p_onsets), len(r_onsets)))
          if not (np.isnan(p_onsets[i]) or np.isnan(r_onsets[i]))]
    v = safe_median(pr, 80, 400)
    if pr and not np.isnan(v):
        out["PR_Interval_ms"] = round(float(v), 2)

    qrs = [((s_peaks[i] - q_peaks[i] + 1) / fs) * 1000
           for i in range(min(len(q_peaks), len(s_peaks)))
           if not (np.isnan(q_peaks[i]) or np.isnan(s_peaks[i])) and s_peaks[i] > q_peaks[i]]
    v = safe_median(qrs, 80, 240)
    if qrs and not np.isnan(v):
        out["QRS_Duration_ms"] = round(float(v), 2)

    qt = [((t_offsets[i] - q_peaks[i] + 1) / fs) * 1000
          for i in range(min(len(q_peaks), len(t_offsets)))
          if not (np.isnan(q_peaks[i]) or np.isnan(t_offsets[i]))]
    qtc = []
    for j in range(min(len(qt), len(r_peaks_f) - 1)):
        rr_sec = (r_peaks_f[j + 1] - r_peaks_f[j]) / fs
        if 0 < rr_sec < 2.0:
            qtc.append(qt[j] / math.sqrt(rr_sec))
    v = safe_median(qtc, 300, 600)
    if qtc and not np.isnan(v):
        out["QTc_ms"] = round(float(v), 2)

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ecg-dir", required=True, help="directory of .mat files (feats 12xN)")
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.ecg_dir, "*.mat")))
    if args.limit:
        files = files[: args.limit]
    rows = []
    for i, f in enumerate(files):
        try:
            feats = scipy.io.loadmat(f)["feats"]
            m = measure_record(feats[1, :])
        except Exception:
            m = {"Heart_Rate": np.nan, "PR_Interval_ms": np.nan,
                 "QRS_Duration_ms": np.nan, "QTc_ms": np.nan}
        rows.append({"ecg_file_path": os.path.basename(f), **m})
        if (i + 1) % 2000 == 0:
            print(f"{i + 1}/{len(files)}")

    df = pd.DataFrame(rows)[["ecg_file_path", "PR_Interval_ms", "QTc_ms",
                             "QRS_Duration_ms", "Heart_Rate"]]
    df.to_csv(args.out_csv, index=False)
    nn = df.notna().mean()
    print(f"WROTE {args.out_csv}: {len(df)} rows")
    print("non-NaN rates:", {c: round(float(nn[c]), 3) for c in df.columns[1:]})


if __name__ == "__main__":
    main()
