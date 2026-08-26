"""
Stress test 04, phase 1: compare a reimplemented measurement CSV against the committed
neurokit2 CSV on the test set — NaN rates, paired deltas, correlation, and the rate at
which the normal/abnormal category (the only thing the judge rubric sees) changes.

Usage: python stage7_compare_measurements.py --ref <committed csv> --new <numpy csv>
       --test-jsonl <any baseline inference jsonl> [--out-md path]
"""
import argparse, ast, json, re
import numpy as np, pandas as pd

# Tool-stated normal ranges, as narrated in the ground-truth dialogues.
RANGES = {
    "Heart_Rate": (60, 100), "PR_Interval_ms": (120, 200),
    "QRS_Duration_ms": (80, 120), "QTc_ms": (-np.inf, 460),
}


def norm(name):
    m = re.match(r"HR0*(\d+)\.mat$", str(name).strip())
    return f"HR{int(m.group(1))}.mat" if m else str(name).strip()


def cat(v, lo, hi):
    if pd.isna(v):
        return "nan"
    return "low" if v < lo else ("high" if v > hi else "normal")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--test-jsonl", required=True)
    ap.add_argument("--out-md")
    a = ap.parse_args()

    test = set()
    for line in open(a.test_jsonl):
        v = json.loads(line)["ecg_file"]
        for n in (ast.literal_eval(v) if str(v).startswith("[") else [v]):
            test.add(norm(n))
    ref = pd.read_csv(a.ref); new = pd.read_csv(a.new)
    ref["k"] = ref.ecg_file_path.map(norm); new["k"] = new.ecg_file_path.map(norm)
    ref = ref[ref.k.isin(test)].set_index("k"); new = new[new.k.isin(test)].set_index("k")
    ids = sorted(set(ref.index) & set(new.index))
    ref, new = ref.loc[ids], new.loc[ids]

    lines = [f"test ids: {len(test)}; matched in both CSVs: {len(ids)}", "",
             "| metric | NaN ref | NaN new | median Δ (new−ref) | IQR Δ | p90 abs Δ | r | crossing % (of both-valid) | ref abnormal % | new abnormal % |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    detail = []
    for m, (lo, hi) in RANGES.items():
        r, n = ref[m], new[m]
        both = r.notna() & n.notna()
        d = (n - r)[both]
        cr = r[both].map(lambda v: cat(v, lo, hi)); cn = n[both].map(lambda v: cat(v, lo, hi))
        cross = (cr != cn)
        pr = np.corrcoef(r[both], n[both])[0, 1] if both.sum() > 2 else np.nan
        lines.append(f"| {m} | {r.isna().mean():.1%} | {n.isna().mean():.1%} | {d.median():+.1f} | "
                     f"[{d.quantile(.25):+.1f}, {d.quantile(.75):+.1f}] | {d.abs().quantile(.9):.1f} | {pr:.3f} | "
                     f"**{cross.mean():.1%}** ({cross.sum()}/{both.sum()}) | "
                     f"{(cr!='normal').mean():.1%} | {(cn!='normal').mean():.1%} |")
        tab = pd.crosstab(cr, cn).reindex(index=["low", "normal", "high"], columns=["low", "normal", "high"], fill_value=0)
        detail.append(f"\n{m} category transition (rows=ref, cols=new):\n{tab.to_string()}")
        detail.append(f"{m}: availability flips (NaN in exactly one impl): {(r.isna() != n.isna()).sum()}")

    any_cross = np.zeros(len(ids), bool)
    for m, (lo, hi) in RANGES.items():
        cr = ref[m].map(lambda v: cat(v, lo, hi)); cn = new[m].map(lambda v: cat(v, lo, hi))
        any_cross |= (cr != cn).values
    lines.append(f"\nECGs with ≥1 category change across the 4 metrics (incl. NaN flips): "
                 f"**{any_cross.mean():.1%}** ({any_cross.sum()}/{len(ids)})")

    out = "\n".join(lines) + "\n" + "\n".join(detail)
    print(out)
    if a.out_md:
        open(a.out_md, "w").write(out + "\n")
    pd.DataFrame({"k": ids, "any_cross": any_cross}).to_csv(
        (a.out_md or "stage7").replace(".md", "") + "_crossing_ids.csv", index=False)


if __name__ == "__main__":
    main()
