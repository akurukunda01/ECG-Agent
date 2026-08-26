"""
Preprocess PTB-XL records500 into 10-s .mat files for the fairseq-signals diagnosis
fine-tune. Adapted from src/preprocess/preprocess_ptbxl.py: adds --csv-dir and embeds a
71-dim multi-hot SCP-code `label` in each file.

Usage: python stage2_preprocess_ptbxl_diagnosis.py <records500 dir> --dest <out dir> --csv-dir <dir with ptbxl_database.csv + scp_statements.csv>
"""
import argparse
import ast
import os

import numpy as np
import pandas as pd
import scipy.io
import wfdb

# Verbatim from src/medrax/tools/classification.py — the tool zips sigmoid outputs
# against this list, so label order must match it exactly.
CLASS_LABELS = [
    '1AVB', '2AVB', '3AVB', 'ABQRS', 'AFIB', 'AFLT', 'ALMI', 'AMI', 'ANEUR', 'ASMI',
    'BIGU', 'CLBBB', 'CRBBB', 'DIG', 'EL', 'HVOLT', 'ILBBB', 'ILMI', 'IMI', 'INJAL',
    'INJAS', 'INJIL', 'INJIN', 'INJLA', 'INVT', 'IPLMI', 'IPMI', 'IRBBB', 'ISCAL',
    'ISCAN', 'ISCAS', 'ISCIL', 'ISCIN', 'ISCLA', 'ISC_', 'IVCD', 'LAFB', 'LAO/LAE',
    'LMI', 'LNGQT', 'LOWT', 'LPFB', 'LPR', 'LVH', 'LVOLT', 'NDT', 'NORM', 'NST_',
    'NT_', 'PAC', 'PACE', 'PMI', 'PRC(S)', 'PSVT', 'PVC', 'QWAVE', 'RAO/RAE', 'RVH',
    'SARRH', 'SBRAD', 'SEHYP', 'SR', 'STACH', 'STD_', 'STE_', 'SVARR', 'SVTAC',
    'TAB_', 'TRIGU', 'VCLVH', 'WPW'
]


def get_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", metavar="DIR", help="records500 directory of PTB-XL")
    parser.add_argument("--dest", type=str, metavar="DIR", help="output directory")
    parser.add_argument("--csv-dir", type=str, required=True,
                        help="directory containing ptbxl_database.csv and scp_statements.csv")
    parser.add_argument("--leads", default="0,1,2,3,4,5,6,7,8,9,10,11", type=str)
    parser.add_argument("--sample-rate", default=500, type=int)
    parser.add_argument("--seed", default=42, type=int)
    return parser


def main(args):
    dir_path = os.path.realpath(args.root)
    dest_path = os.path.realpath(args.dest)
    os.makedirs(dest_path, exist_ok=True)

    leads_to_load = [int(x) for x in args.leads.replace(' ', '').split(',')]
    np.random.seed(args.seed)

    scp_df = pd.read_csv(os.path.join(args.csv_dir, "scp_statements.csv"), index_col=0)
    assert sorted(scp_df.index.tolist()) == sorted(CLASS_LABELS), (
        "scp_statements.csv codes do not match the tool's CLASS_LABELS"
    )
    label_index = {code: i for i, code in enumerate(CLASS_LABELS)}

    csv = pd.read_csv(os.path.join(args.csv_dir, "ptbxl_database.csv"))
    # patient_id must be numeric: the fairseq manifest script does int(data['patient_id'][0][0]),
    # which on a saved string keeps only the first character and collapses the patient split.
    ecg_meta = {
        str(row.ecg_id): (float(row.patient_id), ast.literal_eval(row.scp_codes))
        for row in csv.itertuples()
    }

    n_saved = n_skipped = 0
    unknown_codes = set()
    for root, subdirs, _ in os.walk(dir_path):
        for subdir in sorted(subdirs):
            subdir_path = os.path.join(root, subdir)
            for file in sorted(os.listdir(subdir_path)):
                if not file.endswith(".hea"):
                    continue
                ecg_id = str(int(file[:-7]))
                record_path = os.path.join(subdir_path, file[:-4])
                if ecg_id not in ecg_meta:
                    print(f"Warning: ECG ID {ecg_id} not found in CSV, skipping.")
                    n_skipped += 1
                    continue
                patient_id, scp_codes = ecg_meta[ecg_id]

                try:
                    record, metadata = wfdb.rdsamp(record_path)
                    if args.sample_rate and metadata['fs'] != args.sample_rate:
                        n_skipped += 1
                        continue
                    record = record.T
                    if np.isnan(record).any():
                        print(f"Detected NaN values in: {record_path}, skipping.")
                        n_skipped += 1
                        continue

                    label = np.zeros(len(CLASS_LABELS), dtype=bool)
                    for code in scp_codes:
                        if code in label_index:
                            label[label_index[code]] = 1
                        else:
                            unknown_codes.add(code)

                    scipy.io.savemat(
                        os.path.join(dest_path, f"HR{ecg_id}.mat"),
                        {
                            "curr_sample_rate": metadata['fs'],
                            "feats": record[leads_to_load, :],
                            "patient_id": patient_id,
                            "label": label,
                        },
                    )
                    n_saved += 1
                    if n_saved % 1000 == 0:
                        print(f"saved {n_saved}...")
                except Exception as e:
                    print(f"Error processing {record_path}: {e}")
                    n_skipped += 1

    print(f"DONE: saved {n_saved}, skipped {n_skipped}")
    if unknown_codes:
        print(f"WARNING: scp codes not in CLASS_LABELS (ignored): {sorted(unknown_codes)}")


if __name__ == "__main__":
    main(get_parser().parse_args())
