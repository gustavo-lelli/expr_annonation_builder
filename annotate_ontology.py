"""
annotate_ontology.py
Mapeia PO_anatomy, PO_dev_stage e PECO para cada artigo (plant_code)
nos arquivos *_te_expression_annotation.txt.

Backends suportados:
  --backend anthropic  (padrão) — requer ANTHROPIC_API_KEY
  --backend groq       — gratuito, requer GROQ_API_KEY
                         Chave em: https://console.groq.com  (login Google/GitHub)
  --backend gemini     — gratuito, requer GEMINI_API_KEY
                         Chave em: https://aistudio.google.com/apikey
  --backend ollama     — local, requer `ollama pull qwen2.5:14b`

Uso:
    export GROQ_API_KEY=gsk_...
    python annotate_ontology.py --backend groq [--dry-run] [--prefix Svi]
"""

import csv
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

BASE_DIR     = Path(__file__).parent
ANNOT_DIR    = BASE_DIR / "output"
ARTICLES_DIR = BASE_DIR / "articles"
PO_OBO       = BASE_DIR / "po.obo"
PECO_OBO     = BASE_DIR / "peco.obo"

# ── Backend config ────────────────────────────────────────────────────────────
def _get_backend() -> str:
    if "--backend" in sys.argv:
        i = sys.argv.index("--backend")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return "anthropic"

BACKEND = _get_backend()

ANTHROPIC_MODEL = "claude-haiku-4-5"
GEMINI_MODEL    = "gemini-2.0-flash"      # free: 15 req/min
GROQ_MODEL      = "llama-3.1-8b-instant"     # free: 131K contexto, 20 000 TPM
GROQ_URL        = "https://api.groq.com/openai/v1"
OLLAMA_MODEL    = "qwen2.5:14b"
OLLAMA_URL      = "http://localhost:11434/v1"

# Groq free: 12 000 tokens/min. Prompt compacto ~14 K tokens → ~1 chamada/min.
# Delay de 5s garante margem segura sem chegar ao limite.
GROQ_DELAY_S    = 5.0
GEMINI_DELAY_S  = 4.0


# ── OBO parsing ─────────────────────────────────────────────────────────────

def parse_obo(path: Path) -> list[dict]:
    """Return list of non-obsolete term dicts from an OBO file."""
    terms, current = [], {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip()
            if line == "[Term]":
                if current.get("id"):
                    terms.append(current)
                current = {}
            elif line.startswith("id:"):
                current["id"] = line[3:].strip().split()[0]
            elif line.startswith("name:"):
                current["name"] = line[5:].strip()
            elif line.startswith("def:"):
                m = re.match(r'def: "([^"]+)"', line)
                current["def"] = m.group(1) if m else ""
            elif line.startswith("namespace:"):
                current["namespace"] = line[10:].strip()
            elif line.startswith("is_obsolete:"):
                current["obsolete"] = line[12:].strip() == "true"
            elif line.startswith("subset:"):
                current.setdefault("subsets", []).append(line[7:].strip())
            elif line.startswith("synonym:"):
                m = re.match(r'synonym: "([^"]+)"', line)
                if m:
                    current.setdefault("synonyms", []).append(m.group(1))
    if current.get("id"):
        terms.append(current)
    return [t for t in terms if not t.get("obsolete")]


# Termos PO nucleares válidos para qualquer planta (anatomia fundamental)
_PO_CORE = {
    "PO:0025034",  # vascular leaf
    "PO:0009005",  # root
    "PO:0009047",  # stem
    "PO:0025029",  # shoot axis
    "PO:0009049",  # inflorescence
    "PO:0009010",  # seed
    "PO:0009089",  # endosperm
    "PO:0009009",  # plant embryo
    "PO:0025281",  # shoot apex
    "PO:0006001",  # leaf blade
    "PO:0020142",  # stem internode
    "PO:0000003",  # whole plant
    "PO:0025034",  # vascular leaf
    "PO:0009006",  # shoot system
    "PO:0025025",  # root system
    "PO:0009030",  # carpel
    "PO:0025022",  # collective leaf structure
    "PO:0000013",  # cauline leaf
    "PO:0009026",  # stamen
    "PO:0009031",  # sepal
    "PO:0009032",  # petal
    "PO:0009046",  # flower
}

_PECO_CORE_KW = {
    "drought","water deficit","water deprivation","osmotic",
    "heat","high temperature","temperature","cold","chilling","freezing",
    "salt","salinity","sodium","nacl",
    "light","dark","photoperiod","irradiance",
    "nitrogen","phosphorus","nutrient","carbon",
    "control","normal","untreated","mock",
    "biotic","pathogen","fungal","bacterial","viral","wound",
    "flooding","submergence","hypoxia","anoxia",
    "oxidative","hydrogen peroxide","reactive oxygen",
    "hormone","abscisic acid","aba","ethylene","auxin","gibberellin",
}


def filter_small_context(po_terms: list[dict],
                          peco_terms: list[dict]) -> tuple[list, list, list]:
    """Return (anatomy, dev_stage, peco) filtered to ~3K tokens for Groq/Gemini."""
    anatomy = [t for t in po_terms
               if t.get("namespace") == "plant_anatomy"
               and ("Poaceae" in t.get("subsets", [])
                    or "Maize" in t.get("subsets", [])
                    or "Rice" in t.get("subsets", [])
                    or t.get("id") in _PO_CORE)]
    dev = [t for t in po_terms
           if t.get("namespace") == "plant_structure_development_stage"
           and ("Poaceae" in t.get("subsets", [])
                or "Maize" in t.get("subsets", [])
                or "Rice" in t.get("subsets", []))]
    peco = [t for t in peco_terms
            if t.get("id", "").startswith("PECO:")
            and any(kw in t.get("name", "").lower() for kw in _PECO_CORE_KW)]
    return anatomy, dev, peco


def format_terms(terms: list[dict], id_prefix: str, compact: bool = False) -> str:
    """Format OBO terms for the prompt.

    compact=False (Anthropic): ID | name | short_def [synonyms]  (~90K tokens total)
    compact=True  (Ollama):    ID | name                          (~11K tokens total)
    """
    lines = []
    for t in terms:
        if not t.get("id", "").startswith(id_prefix):
            continue
        if compact:
            lines.append(f"{t['id']} | {t.get('name', '')}")
        else:
            defn = (t.get("def") or "")[:180].replace("\n", " ")
            line = f"{t['id']} | {t.get('name', '')} | {defn}"
            syns = t.get("synonyms", [])[:2]
            if syns:
                line += f" (syn: {'; '.join(syns)})"
            lines.append(line)
    return "\n".join(lines)


# ── Metadata loading ─────────────────────────────────────────────────────────

def load_run_to_code() -> dict[str, str]:
    """Return dict: run_accession → plant_code from detailed_run_info.csv"""
    mapping = {}
    path = ARTICLES_DIR / "detailed_run_info.csv"
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            run  = row.get("run", "").strip()
            code = row.get("plant_code", "").strip()
            if run and code:
                mapping[run] = code
    return mapping


def load_code_metadata() -> dict[str, dict]:
    """Aggregate all metadata per plant_code from detailed_run_info + articles CSVs."""
    meta: dict[str, dict] = defaultdict(lambda: {
        "tissues": set(), "dev_stages": set(), "conditions": set(),
        "treatments": set(), "descriptions": set(), "organisms": set(),
        "doi": "", "title": "", "stress_type": "", "cultivar": "",
    })

    path = ARTICLES_DIR / "detailed_run_info.csv"
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            code = row.get("plant_code", "").strip()
            if not code:
                continue
            m = meta[code]
            for src, dst in [
                ("tissue",          "tissues"),
                ("dev_stage",       "dev_stages"),
                ("plant_condition", "conditions"),
                ("treatment",       "treatments"),
                ("source_name",     "descriptions"),
                ("organism",        "organisms"),
            ]:
                v = row.get(src, "").strip()
                if v:
                    m[dst].add(v)

    for art in sorted(ARTICLES_DIR.glob("*.csv")):
        if art.name == "detailed_run_info.csv":
            continue
        with open(art, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                code = row.get("SraRunInfo", "").strip()
                if not code:
                    continue
                m = meta[code]
                if not m["doi"]:
                    m["doi"]        = row.get("DOI", "").strip()
                    m["title"]      = row.get("Article title", "").strip()
                    m["stress_type"]= row.get("Stress type", "").strip()
                    m["cultivar"]   = row.get("Cultivar", "").strip()

    return meta


# ── Prompt helpers ───────────────────────────────────────────────────────────

def build_system_prompt(po_terms: list[dict], peco_terms: list[dict]) -> str:
    # Backends sem caching ou com contexto limitado usam lista filtrada + formato compacto
    small = BACKEND in ("ollama", "groq", "gemini")

    if small:
        anat_list, dev_list, peco_list = filter_small_context(po_terms, peco_terms)
    else:
        anat_list = [t for t in po_terms if t.get("namespace") == "plant_anatomy"]
        dev_list  = [t for t in po_terms if t.get("namespace") == "plant_structure_development_stage"]
        peco_list = peco_terms

    anatomy  = format_terms(anat_list,  "PO:",   compact=small)
    dev      = format_terms(dev_list,   "PO:",   compact=small)
    peco_txt = format_terms(peco_list,  "PECO:", compact=small)

    return f"""You are an expert in plant biology and RNA-seq experiment ontology annotation.
Given metadata about an RNA-seq experiment, select the best ontology terms.

## PO_anatomy — Plant Anatomy terms (namespace: plant_anatomy):
{anatomy}

## PO_dev_stage — Plant Developmental Stage terms (namespace: plant_structure_development_stage):
{dev}

## PECO — Plant Experimental Conditions Ontology terms:
{peco_txt}

RULES:
- PO_anatomy: most specific anatomical structure of the tissue sampled
- PO_dev_stage: developmental stage of the plant at sampling time (leave empty if not mentioned)
- PECO: most specific experimental condition or treatment applied
- Respond with ONLY a JSON object: {{"PO_anatomy":"PO:XXXXXXX","PO_dev_stage":"PO:XXXXXXX","PECO":"PECO:XXXXXXX"}}
- Use exact IDs from the lists above; use "" when not applicable or uncertain
"""


def build_user_message(code: str, m: dict) -> str:
    def top(s: set, n=5) -> str:
        return ", ".join(sorted(s)[:n]) if s else ""

    parts = [f"Plant code: {code}"]
    if m["doi"]:        parts.append(f"DOI: {m['doi']}")
    if m["title"]:      parts.append(f"Article: {m['title']}")
    if m["stress_type"]:parts.append(f"Stress type: {m['stress_type']}")
    if m["cultivar"]:   parts.append(f"Cultivar: {m['cultivar']}")
    if m["organisms"]:  parts.append(f"Organism: {top(m['organisms'], 2)}")
    if m["tissues"]:    parts.append(f"Tissues sampled: {top(m['tissues'])}")
    if m["dev_stages"]: parts.append(f"Developmental stages: {top(m['dev_stages'])}")
    if m["conditions"]: parts.append(f"Plant conditions: {top(m['conditions'])}")
    if m["treatments"]: parts.append(f"Treatments: {top(m['treatments'])}")
    if m["descriptions"]:parts.append(f"Sample descriptions: {top(m['descriptions'])}")
    parts.append("\nReturn ONLY the JSON object with PO_anatomy, PO_dev_stage, PECO.")
    return "\n".join(parts)


# ── Backend clients ───────────────────────────────────────────────────────────

def make_client():
    if BACKEND == "anthropic":
        import anthropic
        return anthropic.Anthropic()

    elif BACKEND == "gemini":
        try:
            from google import genai
        except ImportError:
            print("Instale o SDK: pip install google-genai")
            sys.exit(1)
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            print("Defina GEMINI_API_KEY. Chave gratuita em https://aistudio.google.com/apikey")
            sys.exit(1)
        return genai.Client(api_key=api_key)

    elif BACKEND == "groq":
        try:
            from openai import OpenAI
        except ImportError:
            print("Instale openai: pip install openai")
            sys.exit(1)
        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            print("Defina GROQ_API_KEY. Chave gratuita em https://console.groq.com")
            sys.exit(1)
        return OpenAI(base_url=GROQ_URL, api_key=api_key)

    else:  # ollama
        try:
            from openai import OpenAI
        except ImportError:
            print("Instale openai: pip install openai")
            sys.exit(1)
        return OpenAI(base_url=OLLAMA_URL, api_key="ollama", timeout=600.0)


# ── API call (backends unificados) ────────────────────────────────────────────

class _FakeUsage:
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0
    input_tokens = 0
    output_tokens = 0


def annotate_code(client, system: str, code: str, meta: dict) -> tuple[dict, object]:
    """Chama o LLM e retorna ({PO_anatomy, PO_dev_stage, PECO}, usage)."""
    user_msg = build_user_message(code, meta)

    if BACKEND == "anthropic":
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=128,
            system=[{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_msg}],
        )
        text  = next((b.text for b in response.content if b.type == "text"), "{}")
        usage = response.usage

    elif BACKEND == "gemini":
        from google.genai import types
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_msg,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.0,
                max_output_tokens=128,
            ),
        )
        text  = response.text or "{}"
        u     = _FakeUsage()
        if response.usage_metadata:
            u.input_tokens  = response.usage_metadata.prompt_token_count or 0
            u.output_tokens = response.usage_metadata.candidates_token_count or 0
        usage = u
        # Respeita o rate limit do tier gratuito (15 req/min)
        time.sleep(GEMINI_DELAY_S)

    elif BACKEND == "groq":
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            max_tokens=128,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user_msg},
            ],
        )
        text  = response.choices[0].message.content or "{}"
        u     = _FakeUsage()
        if response.usage:
            u.input_tokens  = response.usage.prompt_tokens or 0
            u.output_tokens = response.usage.completion_tokens or 0
        usage = u
        time.sleep(GROQ_DELAY_S)

    else:  # ollama
        response = client.chat.completions.create(
            model=OLLAMA_MODEL,
            max_tokens=128,
            temperature=0,
            extra_body={"keep_alive": -1},
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user_msg},
            ],
        )
        text  = response.choices[0].message.content or "{}"
        usage = _FakeUsage()

    m = re.search(r'\{[^{}]+\}', text, re.DOTALL)
    result = json.loads(m.group()) if m else {}
    return {
        "PO_anatomy":   result.get("PO_anatomy",  "").strip(),
        "PO_dev_stage": result.get("PO_dev_stage","").strip(),
        "PECO":         result.get("PECO",        "").strip(),
    }, usage


# ── Annotation file update ───────────────────────────────────────────────────

def update_annotation_file(ann_path: Path,
                            run_to_code: dict[str, str],
                            code_to_terms: dict[str, dict],
                            dry_run: bool) -> int:
    """Rewrite the annotation file with ontology terms filled in. Returns #rows updated."""
    rows = []
    with open(ann_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fieldnames = reader.fieldnames or []
        for row in reader:
            rows.append(dict(row))

    updated = 0
    for row in rows:
        run  = row.get("SampleID", "").strip()
        code = run_to_code.get(run, "")
        if not code or code not in code_to_terms:
            continue
        terms = code_to_terms[code]
        row["PO_anatomy"]   = terms["PO_anatomy"]
        row["PO_dev_stage"] = terms["PO_dev_stage"]
        row["PECO"]         = terms["PECO"]
        updated += 1

    if not dry_run:
        with open(ann_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t",
                                    extrasaction="ignore",
                                    quoting=csv.QUOTE_NONE, escapechar="\\")
            writer.writeheader()
            writer.writerows(rows)

    return updated


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    dry_run   = "--dry-run" in sys.argv
    prefix_filter = None
    if "--prefix" in sys.argv:
        i = sys.argv.index("--prefix")
        prefix_filter = sys.argv[i + 1] if i + 1 < len(sys.argv) else None

    t0 = time.monotonic()

    def elapsed() -> str:
        return f"{time.monotonic() - t0:.1f}s"

    def log(msg: str, end: str = "\n") -> None:
        print(f"[{elapsed():>6}] {msg}", end=end, flush=True)

    if dry_run:
        log("DRY RUN — arquivos não serão modificados")

    log("Parsing po.obo...", end=" ")
    po_terms = parse_obo(PO_OBO)
    print(f"{len(po_terms)} termos ativos")

    log("Parsing peco.obo...", end=" ")
    peco_terms = parse_obo(PECO_OBO)
    print(f"{len(peco_terms)} termos ativos")

    log("Construindo system prompt...", end=" ")
    system_prompt = build_system_prompt(po_terms, peco_terms)
    tok_est = int(len(system_prompt.split()) * 1.3)
    print(f"~{tok_est:,} tokens ({'compacto' if BACKEND == 'ollama' else 'completo'})")

    log("Carregando run_to_code (detailed_run_info)...", end=" ")
    run_to_code = load_run_to_code()
    print(f"{len(run_to_code):,} runs mapeados")

    log("Carregando metadados por plant_code...", end=" ")
    code_metadata = load_code_metadata()
    print(f"{len(code_metadata)} plant_codes")

    # Group plant_codes by species prefix
    by_prefix: dict[str, list] = defaultdict(list)
    for code in code_metadata:
        pfx = code.split("_")[0]
        if prefix_filter and pfx != prefix_filter:
            continue
        by_prefix[pfx].append(code)

    total_codes = sum(len(v) for v in by_prefix.values())
    log(f"Backend: {BACKEND} | modelo: {OLLAMA_MODEL if BACKEND == 'ollama' else ANTHROPIC_MODEL}")
    log(f"Espécies: {len(by_prefix)} | plant_codes a anotar: {total_codes}")

    if BACKEND == "ollama":
        log("Conectando ao Ollama (primeira chamada carrega o modelo na RAM)...")

    client = make_client()

    total = {"cache_write": 0, "cache_read": 0, "input": 0, "output": 0, "calls": 0}
    grand_updated = 0
    done = 0

    for pfx in sorted(by_prefix):
        codes = sorted(by_prefix[pfx])
        ann_path = ANNOT_DIR / f"{pfx}_te_expression_annotation.txt"
        if not ann_path.exists():
            log(f"{pfx}: arquivo de anotação não encontrado, pulando")
            continue

        print()
        log(f"━━ {pfx} ({len(codes)} artigos) ━━")
        code_to_terms: dict[str, dict] = {}

        for i, code in enumerate(codes, 1):
            meta = code_metadata[code]
            t_call = time.monotonic()
            log(f"  [{i}/{len(codes)}] {code}...", end=" ")
            try:
                terms, usage = annotate_code(client, system_prompt, code, meta)
                code_to_terms[code] = terms
                dt = time.monotonic() - t_call

                cw = getattr(usage, "cache_creation_input_tokens", 0)
                cr = getattr(usage, "cache_read_input_tokens", 0)
                total["cache_write"] += cw
                total["cache_read"]  += cr
                total["input"]       += usage.input_tokens
                total["output"]      += usage.output_tokens
                total["calls"]       += 1
                done += 1

                tag = ""
                if BACKEND == "anthropic":
                    tag = f" [{'WRITE' if cw else 'HIT' if cr else 'MISS'}]"
                eta_s = (time.monotonic() - t0) / done * (total_codes - done) if done else 0
                eta = f"  ETA ~{eta_s:.0f}s" if eta_s > 5 else ""
                print(f"{dt:.1f}s | {terms}{tag}{eta}")

            except Exception as e:
                dt = time.monotonic() - t_call
                print(f"{dt:.1f}s | ERRO — {e}")
                code_to_terms[code] = {"PO_anatomy": "", "PO_dev_stage": "", "PECO": ""}

        n = update_annotation_file(ann_path, run_to_code, code_to_terms, dry_run)
        grand_updated += n
        log(f"  {n} linhas atualizadas em {ann_path.name}")
        # DEBUG
        if code_to_terms:
            sample = list(code_to_terms.items())[0]
            log(f"  [DEBUG] Amostra code_to_terms: {sample[0]} → {sample[1]}")

    total_time = time.monotonic() - t0
    avg = total_time / total["calls"] if total["calls"] else 0

    model_name = {"anthropic": ANTHROPIC_MODEL, "gemini": GEMINI_MODEL,
                  "groq": GROQ_MODEL, "ollama": OLLAMA_MODEL}.get(BACKEND, BACKEND)

    extra = ""
    if BACKEND == "anthropic":
        cost = (
            total["cache_write"] * 1.25 / 1e6
            + total["cache_read"] * 0.10 / 1e6
            + total["input"]     * 1.00 / 1e6
            + total["output"]    * 5.00 / 1e6
        )
        extra = (
            f"  Cache write  : {total['cache_write']:,} tokens\n"
            f"  Cache read   : {total['cache_read']:,} tokens\n"
            f"  Custo estim. : US$ {cost:.4f}"
        )

    print(f"""
━━━ Resumo ━━━
  Backend      : {BACKEND} ({model_name}){'  [gratuito]' if BACKEND == 'gemini' else ''}
  Chamadas     : {total['calls']}  ({avg:.1f}s/chamada em média)
  Tempo total  : {total_time:.1f}s
  Input tokens : {total['input']:,}
  Output tokens: {total['output']:,}
{extra}
  Linhas atualizadas: {grand_updated}
""")


if __name__ == "__main__":
    main()
