"""
Stress test 04, phase 1b: score measurement CSVs against PTB-XL+ (Uni-G = the paper's
judge reference; 12SL as a second opinion) at the normal/abnormal category level.

Usage: python stage7_score_vs_unig.py --tools name=csv [name=csv ...] --unig <unig_features.csv>
       --sl12 <12sl_features.csv> --test-jsonl <any baseline inference jsonl> [--out-md path]
"""
import argparse, ast, json, re
import numpy as np, pandas as pd

RANGES = {"Heart_Rate": (60, 100), "PR_Interval_ms": (120, 200),
          "QRS_Duration_ms": (80, 120), "QTc_ms": (-np.inf, 460)}


def norm(n):
    return int(re.match(r"HR0*(\d+)\.mat$", str(n).strip()).group(1))


def cat(v, lo, hi):
    if pd.isna(v):
        return "nan"
    return "low" if v < lo else ("high" if v > hi else "normal")


def ref_table(path):
    d = pd.read_csv(path).set_index("ecg_id")
    out = pd.DataFrame(index=d.index)
    out["Heart_Rate"] = d["HR__Global"]
    out["PR_Interval_ms"] = d["PR_Int_Global"]
    out["QRS_Duration_ms"] = d["QRS_Dur_Global"]
    # Bazett recomputed from QT/RR so the correction formula matches the tool's.
    out["QTc_ms"] = d["QT_Int_Global"] / np.sqrt(d["RR_Mean_Global"] / 1000.0)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tools", nargs="+", required=True, help="name=csv pairs")
    ap.add_argument("--unig", required=True)
    ap.add_argument("--sl12", required=True)
    ap.add_argument("--test-jsonl", required=True)
    ap.add_argument("--out-md")
    a = ap.parse_args()

    test = set()
    for line in open(a.test_jsonl):
        v = json.loads(line)["ecg_file"]
        for n in (ast.literal_eval(v) if str(v).startswith("[") else [v]):
            test.add(norm(n))
    refs = {"UniG": ref_table(a.unig), "12SL": ref_table(a.sl12)}

    lines = [f"test ids: {len(test)}", "",
             "| tool | metric | ref | n | category agreement | tool abnormal % | ref abnormal % | r | median Δ (tool−ref) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for spec in a.tools:
        name, path = spec.split("=", 1)
        t = pd.read_csv(path); t["eid"] = t.ecg_file_path.map(norm)
        t = t[t.eid.isin(test)].set_index("eid")
        for m, (lo, hi) in RANGES.items():
            for rn, r in refs.items():
                ids = sorted(set(t.index) & set(r.index))
                tv, rv = t.loc[ids, m], r.loc[ids, m]
                both = tv.notna() & rv.notna()
                ct = tv[both].map(lambda v: cat(v, lo, hi)); cr = rv[both].map(lambda v: cat(v, lo, hi))
                pr = np.corrcoef(tv[both], rv[both])[0, 1]
                # Tool QRS is Q-peak->S-peak; PTB-XL+ QRS is onset->offset — not comparable.
                flag = " (definition mismatch)" if m == "QRS_Duration_ms" else ""
                lines.append(f"| {name} | {m}{flag} | {rn} | {both.sum()} | **{(ct==cr).mean():.1%}** | "
                             f"{(ct!='normal').mean():.1%} | {(cr!='normal').mean():.1%} | {pr:.3f} | {(tv-rv)[both].median():+.1f} |")

    lines += ["", "Reference-vs-reference (UniG vs 12SL) category agreement on test ids:"]
    u, s = refs["UniG"], refs["12SL"]
    ids = sorted((set(u.index) & set(s.index)) & test)
    for m, (lo, hi) in RANGES.items():
        cu = u.loc[ids, m].map(lambda v: cat(v, lo, hi)); cs = s.loc[ids, m].map(lambda v: cat(v, lo, hi))
        lines.append(f"- {m}: {(cu==cs).mean():.1%} (n={len(ids)})")

    out = "\n".join(lines)
    print(out)
    if a.out_md:
        open(a.out_md, "w").write(out + "\n")


if __name__ == "__main__":
    main()
