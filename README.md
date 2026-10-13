# Expression Annotation Builder

Scripts that build per-species **sample annotation tables** for transposable element (TE) expression
matrices of plant RNA-seq data. For each RNA-seq run, the tables record the source article (DOI),
condition, replicate, library layout, and ontology terms (Plant Ontology anatomy / developmental stage,
and PECO experimental condition).

The pipeline is species-agnostic: every species is identified by a short **prefix** (e.g. `Abc`) and
any number of species can be processed. The repository ships with a Poaceae dataset (maize, sorghum,
setaria, sugarcane) as a working example; see [Running it for any species](#running-it-for-any-species)
to use your own.

The pipeline has three steps:

1. `build_annotations.py` – builds the base annotation tables from the curated metadata CSVs.
2. `annotate_ontology.py` – fills the ontology columns (`PO_anatomy`, `PO_dev_stage`, `PECO`) using an LLM.
3. `merge_clean_matrices.py` – merges the expression matrices and keeps only the samples present in the annotation.

## Requirements

- Python 3.9 or newer (tested with 3.12)
- Python packages:

  | Package | Needed by |
  |---|---|
  | `pandas` | `merge_clean_matrices.py` |
  | `anthropic` | `annotate_ontology.py` with `--backend anthropic` (default) |
  | `openai` | `annotate_ontology.py` with `--backend groq` or `--backend ollama` |
  | `google-genai` | `annotate_ontology.py` with `--backend gemini` |

  `build_annotations.py` only uses the standard library. Install only what you need, e.g.:

  ```bash
  python -m venv .venv && source .venv/bin/activate
  pip install pandas anthropic          # add openai and/or google-genai if you use those backends
  ```

- An LLM backend for step 2 (pick one):

  | Backend | Setup |
  |---|---|
  | `anthropic` (default) | `export ANTHROPIC_API_KEY=...` (model: `claude-haiku-4-5`) |
  | `groq` (free tier) | `export GROQ_API_KEY=...` – key from <https://console.groq.com> |
  | `gemini` (free tier) | `export GEMINI_API_KEY=...` – key from <https://aistudio.google.com/apikey> |
  | `ollama` (local) | install [Ollama](https://ollama.com) and run `ollama pull qwen2.5:14b` |

## Repository layout and input data

```
.
├── build_annotations.py        # step 1
├── annotate_ontology.py        # step 2
├── merge_clean_matrices.py     # step 3
├── po.obo                      # Plant Ontology (anatomy + developmental stage terms)
├── peco.obo                    # Plant Experimental Conditions Ontology
├── experiments/                # one CSV per species, one row per SRA run (input)
├── articles/                   # article metadata and detailed SRA run info (input)
├── expression_matrices/        # merged expression matrices (.tsv) – NOT versioned, empty in the repo
├── conditions/                 # per-species sample -> condition tables (.tsv) – NOT versioned, empty in the repo
└── output/                     # generated annotation tables (.txt) – NOT versioned, empty in the repo
```

The `.txt` and `.tsv` data files are **not** included in the repository; the `output/`,
`expression_matrices/` and `conditions/` folders are present but empty. The `.csv` inputs in
`experiments/` and `articles/` and the two `.obo` files are included, so steps 1 and 2 can be run
from a fresh clone. Step 3 requires you to provide the expression matrices yourself (see below).

### Input files

**`experiments/<Species name>.csv`** – one file per species, named exactly as in `PLANT_MAP`
(e.g. `Genus species.csv`). Columns: `plant_code, run, bytes, bioproject, description, treatment`.
`plant_code` identifies the study and must follow the pattern `<prefix>_<study number>`
(e.g. `Abc_1`, `Abc_2`); the part before the underscore is the species prefix.
Rows with an empty `plant_code`/`run` or a `TOTAL...` code are ignored.

**`articles/<species>.csv`** – one file per species (any name ending in `.csv` except
`detailed_run_info.csv`; all of them are read). Columns used:
`Article title`, `Stress type`, `Cultivar`, `DOI`, and `SraRunInfo` (the `plant_code` the row refers to).

**`articles/detailed_run_info.csv`** – SRA run metadata for every run of every study. Columns used:
`plant_code`, `run`, `biological_replicate`, `library_layout`, plus `tissue`, `dev_stage`,
`plant_condition`, `treatment`, `source_name`, `organism` (used as context for the LLM).

**`po.obo` / `peco.obo`** – ontology files (PO release 2024-04-17, PECO release 2023-11-10).
Non-obsolete terms are parsed and offered to the LLM as the allowed vocabulary.

**`expression_matrices/`** (you provide) – tab-separated matrices with TE copies as rows (first column
`Name`, e.g. `TE_00002645_copy1661|scaf_99:35899-45737|-`) and SRA run IDs as columns. Expected names
(`<prefix>` is the species prefix):

```
<prefix>_merged_counts.tsv
<prefix>_merged_TPM.tsv
<prefix>_miss_merged_<counts|TPM>.tsv   # optional: extra runs that were quantified later
<prefix>_err_merged_<counts|TPM>.tsv    # optional: extra runs that were re-quantified after errors
```

**`conditions/<prefix>.tsv`** (you provide, optional) – two columns, `sample` and `condition`
(e.g. `SRR8368655	control`). Not read by the scripts in this repository; it is a companion
table for downstream analysis.

### Species prefixes

Each species is identified by a short, unique prefix of your choosing (the bundled data uses three-letter
codes built from the genus and species, e.g. `Svi` for *Setaria viridis*). The same prefix is used everywhere:

| Where | How the prefix is used |
|---|---|
| `plant_code` in the input CSVs | `<prefix>_<study number>` (e.g. `Abc_1`) |
| `PLANT_MAP` in `build_annotations.py` | `("Genus species", "<prefix>", "<articles file stem>")` |
| `output/` | `<prefix>_te_expression_annotation.txt` |
| `expression_matrices/` | `<prefix>_merged_counts.tsv`, `<prefix>_merged_TPM.tsv` |
| `conditions/` | `<prefix>.tsv` |
| `annotate_ontology.py --prefix` | `--prefix <prefix>` restricts annotation to one species |
| `SPECIES` in `merge_clean_matrices.py` | list of prefixes to merge |

Keep the prefix **identical** across all of these; the scripts match files purely by name.

## How it works

### Step 1 – `build_annotations.py`

```bash
python build_annotations.py
```

For every species in `PLANT_MAP`, it reads `experiments/<Species name>.csv` and, for each run, looks up:

- the **DOI** from `articles/*.csv` (via `plant_code` ↔ `SraRunInfo`);
- the **replicate** (`Rep_1` → `1`) and **layout** (`PAIRED` → `paired-end`, `SINGLE` → `single-end`)
  from `articles/detailed_run_info.csv`, keyed by `(plant_code, run)`.

It writes `output/<prefix>_te_expression_annotation.txt` (tab-separated) with the columns:

| Column | Source |
|---|---|
| `SampleID` | SRA run accession |
| `DOI` | articles CSV |
| `ConditionDescription` | `description` in the experiments CSV |
| `Replicate` | run info |
| `Strandness` | left empty – fill in manually |
| `Layout` | run info |
| `PO_anatomy`, `PO_dev_stage`, `PECO` | left empty – filled by step 2 (or manually) |
| `Sample_groups` | `stress: <treatment>` from the experiments CSV |

Missing DOIs or run info are reported as `[WARN]` lines on the console.

> **Note:** the output file name is built from the prefix in `PLANT_MAP`, `annotate_ontology.py` derives the
> prefix from the part of `plant_code` before the underscore, and `merge_clean_matrices.py` uses the names in
> its `SPECIES` list. These three must resolve to the same prefix (in the bundled example, maize matrices
> and annotation are named `Zma1_*` while its `plant_code`s start with `Zma_`, so that species needs a
> rename or a matching prefix).

### Step 2 – `annotate_ontology.py`

```bash
export ANTHROPIC_API_KEY=...                         # or GROQ_API_KEY / GEMINI_API_KEY
python annotate_ontology.py --dry-run                # preview, no files are modified
python annotate_ontology.py                          # annotate all species with Anthropic
python annotate_ontology.py --backend groq --prefix Abc
```

Options:

| Flag | Meaning |
|---|---|
| `--backend {anthropic,groq,gemini,ollama}` | LLM provider (default: `anthropic`) |
| `--prefix <prefix>` | annotate a single species only (default: every prefix found in `detailed_run_info.csv`) |
| `--dry-run` | run the whole process but do not write any file |

What it does:

1. Parses `po.obo` and `peco.obo` and builds a system prompt listing the valid ontology terms.
   The Anthropic backend gets the full term lists (with definitions/synonyms; the prompt is cached).
   The smaller-context backends (`groq`, `gemini`, `ollama`) get a reduced list: PO terms in the
   `Poaceae`/`Maize`/`Rice` subsets plus a core set, and PECO terms matching common stress keywords.
   For species outside the grasses, use the `anthropic` backend or edit `filter_small_context()`,
   `_PO_CORE` and `_PECO_CORE_KW` in the script.
2. Aggregates the metadata of each **study** (`plant_code`): article title, stress type, cultivar,
   tissues, developmental stages, conditions, treatments and sample descriptions.
3. Makes **one LLM call per study** asking for `{"PO_anatomy", "PO_dev_stage", "PECO"}` as JSON.
   An empty string is returned when the model is unsure.
4. Writes the three terms into every row of `output/<prefix>_te_expression_annotation.txt` that belongs to
   that study (matched through `SampleID` ↔ `run` in `detailed_run_info.csv`). **The file is overwritten in place**,
   so previously filled ontology values for those rows are replaced.

Terms are assigned per study, not per sample, so review the result and refine individual rows manually when
a study contains several tissues or stages. Free-tier backends are rate limited; the script sleeps between
calls (4–5 s for Gemini/Groq).

### Step 3 – `merge_clean_matrices.py`

```bash
python merge_clean_matrices.py
```

For each species and each measurement (`counts`, `TPM`) it:

1. loads `expression_matrices/<Species>_merged_<measurement>.tsv`;
2. appends any columns found only in the optional `<Species>_miss_merged_...` and `<Species>_err_merged_...` files;
3. drops every column (sample) that is not listed in `SampleID` of the species annotation table in `output/`;
4. **overwrites** the main matrix with the result and prints how many columns were added/removed.

The species list is hard-coded at the top of the script (`SPECIES = [...]`, a list of prefixes; it is
currently set to a single species). Edit it to process the species you want. Run step 1 first, since the annotation table
defines which samples are kept.

## Running it for any species

To process a species that is not in the bundled data (replace `Abc` and `Genus species` with your own):

1. **Choose a prefix**, e.g. `Abc`, and name every study `Abc_1`, `Abc_2`, ...
2. **Add the metadata** (all `plant_code`s must start with `Abc_`):
   - `experiments/Genus species.csv` with columns `plant_code, run, bytes, bioproject, description, treatment`;
   - `articles/<any name>.csv` with `Article title, Stress type, Layout, Cultivar, DOI, ..., SraRunInfo`
     (`SraRunInfo` = the `plant_code`);
   - the corresponding run rows appended to `articles/detailed_run_info.csv`.
3. **Register the species** in `PLANT_MAP` in `build_annotations.py`:
   ```python
   ("Genus species", "Abc", "genus_species"),
   ```
4. **Run the pipeline**:
   ```bash
   python build_annotations.py
   python annotate_ontology.py --prefix Abc
   ```
5. **Add the matrices** `expression_matrices/Abc_merged_counts.tsv` and `Abc_merged_TPM.tsv`, set
   `SPECIES = ["Abc"]` in `merge_clean_matrices.py`, and run `python merge_clean_matrices.py`.

## Typical workflow

```bash
# 1. base annotation tables (creates output/*.txt)
python build_annotations.py

# 2. fill PO / PECO columns with an LLM, then review the tables manually
python annotate_ontology.py --dry-run --prefix <prefix>
python annotate_ontology.py --prefix <prefix>

# 3. fill Strandness manually in output/*.txt, put the matrices in expression_matrices/, then
python merge_clean_matrices.py
```

## Notes

- The scripts resolve all paths relative to their own location, so they can be run from any directory.
- Steps 2 and 3 modify files in place; keep a copy of `output/` and `expression_matrices/` before re-running.
- `Strandness` is never filled automatically.
