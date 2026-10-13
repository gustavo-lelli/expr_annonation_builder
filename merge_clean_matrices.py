import pandas as pd
from pathlib import Path

BASE_DIR = Path(__file__).parent
EXPR_DIR = BASE_DIR / "expression_matrices"
ANNOT_DIR = BASE_DIR / "output"

SPECIES = [] # Put species here
MEASUREMENTS = ["counts", "TPM"]


def load_annotation_ids(spe):
    path = ANNOT_DIR / f"{spe}_te_expression_annotation.txt"
    if not path.exists():
        print(f"  [WARN] annotation not found: {path.name}")
        return set()
    df = pd.read_csv(path, sep="\t", usecols=["SampleID"])
    return set(df["SampleID"].str.strip())


def merge_and_clean(spe, measurement, ann_ids):
    main_path = EXPR_DIR / f"{spe}_merged_{measurement}.tsv"
    if not main_path.exists():
        print(f"  [SKIP] main file not found: {main_path.name}")
        return

    df = pd.read_csv(main_path, sep="\t", index_col=0)
    added = []

    for variant in ("miss", "err"):
        path = EXPR_DIR / f"{spe}_{variant}_merged_{measurement}.tsv"
        if not path.exists():
            continue
        extra = pd.read_csv(path, sep="\t", index_col=0)
        new_cols = [c for c in extra.columns if c not in df.columns]
        if new_cols:
            df = pd.concat([df, extra[new_cols]], axis=1)
            added.extend(new_cols)

    before = df.shape[1]
    valid_cols = [c for c in df.columns if c in ann_ids]
    removed = [c for c in df.columns if c not in ann_ids]
    df = df[valid_cols]

    print(
        f"  {measurement}: {before - len(added)} base + {len(added)} added"
        f" - {len(removed)} removed = {df.shape[1]} final columns"
    )
    if removed:
        print(f"    removed (not in annotation): {removed}")

    df.to_csv(main_path, sep="\t")


def main():
    for spe in SPECIES:
        print(f"\n{spe}")
        ann_ids = load_annotation_ids(spe)
        if not ann_ids:
            continue
        print(f"  annotation: {len(ann_ids)} valid SampleIDs")
        for measurement in MEASUREMENTS:
            merge_and_clean(spe, measurement, ann_ids)

    print("\nDone.")


if __name__ == "__main__":
    main()
