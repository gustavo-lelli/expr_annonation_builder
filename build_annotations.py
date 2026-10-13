import csv
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
EXPERIMENTS_DIR = BASE_DIR / "experiments"
ARTICLES_DIR = BASE_DIR / "articles"
OUTPUT_DIR = BASE_DIR / "output"

PLANT_MAP = [
    ("Setaria viridis",      "Svi", "setaria_viridis"),
    ("Setaria italica",      "Sit", "setaria_italica"),
    ("Zea Mays",             "Zma", "zea_mays"),
    ("Sorghum bicolor",      "Sbi", "sorghum_bicolor"),
    ("Saccharum officinarum","Sof", "saccharum_officinarum"),
    ("Saccharum spontaneum", "Ssp", "saccharum_spontaneum"),
    ("Saccharum hybrid",     "Shy", "saccharum_hybrid"),
]

OUTPUT_HEADER = [
    "SampleID", "DOI", "ConditionDescription", "Replicate",
    "Strandness", "Layout", "PO_anatomy", "PO_dev_stage", "PECO",
    "Sample_groups",
]


def load_detailed_run_info(path):
    """Returns dict: (plant_code, run) -> {biological_replicate, library_layout}"""
    info = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["plant_code"].strip(), row["run"].strip())
            info[key] = {
                "biological_replicate": row.get("biological_replicate", "").strip(),
                "library_layout": row.get("library_layout", "").strip(),
            }
    return info


def load_articles(path):
    """Returns dict: SraRunInfo -> DOI (stripped)"""
    doi_map = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            code = row.get("SraRunInfo", "").strip()
            doi = row.get("DOI", "").strip()
            if code:
                doi_map[code] = doi
    return doi_map


def load_all_articles(articles_dir):
    """Loads DOI map from every articles CSV (excluding detailed_run_info)."""
    doi_map = {}
    for path in sorted(articles_dir.glob("*.csv")):
        if path.name == "detailed_run_info.csv":
            continue
        doi_map.update(load_articles(path))
    return doi_map


def is_data_row(row):
    """Returns False for empty/summary rows (blank plant_code or 'TOTAL:' lines)."""
    plant_code = row.get("plant_code", "").strip()
    run = row.get("run", "").strip()
    if not plant_code or not run:
        return False
    if plant_code.upper().startswith("TOTAL"):
        return False
    return True


def parse_replicate(value):
    """'Rep_1' -> '1', '' -> ''"""
    if not value:
        return ""
    cleaned = value.strip()
    if cleaned.upper().startswith("REP_"):
        return cleaned[4:]
    return cleaned


def parse_layout(value):
    """'PAIRED' -> 'paired-end', 'SINGLE' -> 'single-end'"""
    mapping = {"PAIRED": "paired-end", "SINGLE": "single-end"}
    return mapping.get(value.upper().strip(), value.strip().lower())


def process_plant(plant_name, prefix, articles_stem, run_info, doi_map):
    exp_path = EXPERIMENTS_DIR / f"{plant_name}.csv"
    out_path = OUTPUT_DIR / f"{prefix}_te_expression_annotation.txt"

    if not exp_path.exists():
        print(f"  [SKIP] experiments file not found: {exp_path}")
        return

    rows_out = []
    warnings = []

    with open(exp_path, newline="", encoding="utf-8") as f:
        for exp_row in csv.DictReader(f):
            if not is_data_row(exp_row):
                continue
            plant_code = exp_row["plant_code"].strip()
            run = exp_row["run"].strip()
            description = exp_row["description"].strip()
            treatment = exp_row.get("treatment", "").strip()

            doi = doi_map.get(plant_code, "")
            if not doi:
                warnings.append(f"    no DOI for plant_code={plant_code!r}")

            meta = run_info.get((plant_code, run))
            if meta is None:
                warnings.append(f"    no run_info for ({plant_code!r}, {run!r})")
                replicate = ""
                layout = ""
            else:
                replicate = parse_replicate(meta["biological_replicate"])
                layout = parse_layout(meta["library_layout"]) if meta["library_layout"] else ""

            rows_out.append([
                run,
                doi,
                description,
                replicate,
                "",        # Strandness (manual)
                layout,
                "",        # PO_anatomy (manual)
                "",        # PO_dev_stage (manual)
                "",        # PECO (manual)
                f"stress: {treatment}" if treatment else "",
            ])

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\")
        writer.writerow(OUTPUT_HEADER)
        writer.writerows(rows_out)

    print(f"  -> {out_path.name}  ({len(rows_out)} runs)")
    for w in warnings:
        print(f"  [WARN] {w}")


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    run_info = load_detailed_run_info(ARTICLES_DIR / "detailed_run_info.csv")
    doi_map = load_all_articles(ARTICLES_DIR)
    print(f"Loaded {len(run_info)} run_info entries, {len(doi_map)} DOI mappings\n")

    for plant_name, prefix, articles_stem in PLANT_MAP:
        print(f"{plant_name} ({prefix})")
        process_plant(plant_name, prefix, articles_stem, run_info, doi_map)

    print("\nDone.")


if __name__ == "__main__":
    main()
