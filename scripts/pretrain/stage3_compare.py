"""
Agreement between the reproduced classification tool CSV and the authors' committed CSV.

Usage: python stage3_compare.py   (paths are set below)
"""
import re
import pandas as pd

OURS = "/home/kuruk/ecg_runs/stage3_tool_outputs/ecg_analysis_classification.csv"
THEIRS = "/mnt/c/Users/kuruk/Desktop/VS_Projects/ECG-Agent/ECG-Agent/src/results/ecg_analysis_classification.csv"


def parse(cell):
    """'SR (98.23%), NORM (86.88%)' -> {label: prob}; 'None > 50%' -> {}"""
    if not isinstance(cell, str) or cell.startswith("None >") or cell == "Classification Error":
        return {}
    return {m.group(1).strip(): float(m.group(2)) / 100
            for m in re.finditer(r"([^,()]+?) \(([\d.]+)%\)", cell)}


ours = pd.read_csv(OURS)
theirs = pd.read_csv(THEIRS)

ours["key"] = ours.ecg_file_path.str.replace(r"^HR0*", "HR", regex=True)
theirs["key"] = theirs.ecg_file_path

print(f"ours rows: {len(ours)} (padded keys) | theirs: {len(theirs)} (unpadded keys)")
pad_ours = ours.ecg_file_path.str.fullmatch(r"HR\d{5}\.mat").mean()
pad_theirs = theirs.ecg_file_path.str.fullmatch(r"HR\d{5}\.mat").mean()
print(f"5-digit-padded key fraction: ours {pad_ours:.3f}, theirs {pad_theirs:.3f}")

only_theirs = set(theirs.key) - set(ours.key)
only_ours = set(ours.key) - set(theirs.key)
print(f"keys only in theirs: {len(only_theirs)} (e.g. {sorted(only_theirs)[:5]})")
print(f"keys only in ours:   {len(only_ours)} (e.g. {sorted(only_ours)[:5]})")

m = ours.merge(theirs, on="key", suffixes=("_ours", "_theirs"))
print(f"joined: {len(m)}")

exact = top1 = jacc_sum = both_nonempty = 0
for a_cell, b_cell in zip(m.top_classes_ours, m.top_classes_theirs):
    a, b = parse(a_cell), parse(b_cell)
    sa, sb = set(a), set(b)
    if sa == sb:
        exact += 1
    if sa and sb:
        both_nonempty += 1
        if max(a, key=a.get) == max(b, key=b.get):
            top1 += 1
        jacc_sum += len(sa & sb) / len(sa | sb)
    elif not sa and not sb:
        top1 += 1
n = len(m)
print(f"exact label-set match: {exact}/{n} = {exact/n:.1%}")
print(f"top-1 label match (both non-empty or both empty): {top1}/{n} = {top1/n:.1%}")
print(f"mean Jaccard over both-non-empty pairs: {jacc_sum/both_nonempty:.3f} (n={both_nonempty})")

diff = [(k, a, b) for k, a, b in zip(m.key, m.top_classes_ours, m.top_classes_theirs)
        if set(parse(a)) != set(parse(b))][:5]
for k, a, b in diff:
    print(f"  DIFF {k}: ours=[{a}] theirs=[{b}]")
