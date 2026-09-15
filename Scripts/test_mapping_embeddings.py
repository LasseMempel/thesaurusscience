#!/usr/bin/env python3
"""
Extended SSSOM mapping analysis (fixed/extended version).

Changes vs. the original script:
  - String similarity metrics are now case-INsensitive (this was silently
    zeroing out ~27% of closeMatch string scores, mostly ADS ALL-CAPS vs.
    AAT lowercase labels — e.g. "CREMATION" vs "cremations" scored 0.0).
  - Embedding cosine similarities are clipped to [-1, 1] (float32 rounding
    was pushing some values to ~1.0009).
  - Exact-duplicate (subject_id, object_id, predicate_id) rows are dropped.
  - Per-file row counts are logged before/after the label-completeness
    dropna, so files that silently contribute zero rows are visible.
  - A random-pair NULL BASELINE is sampled from the label pool, so you can
    tell whether a model's similarity on true mappings is meaningfully
    higher than its similarity on random/unrelated pairs (important for
    e5-style models, whose embedding space is anisotropic and compresses
    all cosine similarities upward regardless of relatedness).
  - The single "combined" boxplot is replaced by a 2x2 faceted figure:
    {exactMatch, closeMatch} x {full labels, normalised labels}.
"""

from pathlib import Path
import pickle
import re

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from sentence_transformers import SentenceTransformer
from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler
from sklearn.metrics import roc_auc_score


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
MAPPING_DIR = Path("/home/mempellaenger/repos/thesaurusscience/Mappings")

FILES = [
    "dai_aat.sssom.tsv",
    "inrap_pactols_sujets_aat.sssom.tsv",
    "inrap_pactols_lieux_aat.sssom.tsv",
    "ads_eh_com_aat.sssom.tsv",
    "ads_eh_period_aat.sssom.tsv",
    "ads_eh_tbm_aat.sssom.tsv",
    "ads_eh_tmc_aat.sssom.tsv",
    "ads_eh_tmt2_aat.sssom.tsv",
    "ads_hes_scapa_aat.sssom.tsv",
    "ads_mda_obj_aat.sssom.tsv",
]

MODEL_CONFIGS = [
    {
        "name": "e5-large-instruct",
        "model_id": "intfloat/multilingual-e5-large-instruct",
        "prefix": "passage: ",
    },
    {
        "name": "m2v-bge-m3-1024d",
        "model_id": "tss-deposium/m2v-bge-m3-1024d",
        "prefix": "",
    },
]

EXCLUDED_PREDICATES = ("broadMatch", "narrowMatch", "relatedMatch")

# Case-fold BEFORE comparing. This was the actual bug: rapidfuzz compares
# characters case-sensitively, so "CREMATION" vs "cremations" (an ADS
# all-caps label vs. an AAT lowercase label) scores 0.0 even though the
# words are nearly identical.
STRING_METRICS = {
    "levenshtein":  lambda a, b: fuzz.ratio(a.lower(), b.lower()) / 100.0,
    "jaro_winkler": lambda a, b: JaroWinkler.similarity(a.lower(), b.lower()),
    "token_sort":   lambda a, b: fuzz.token_sort_ratio(a.lower(), b.lower()) / 100.0,
    "token_set":    lambda a, b: fuzz.token_set_ratio(a.lower(), b.lower()) / 100.0,
}

N_NULL_PAIRS = 2000  # size of the random-pair null baseline, per model
RNG_SEED = 42


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------
_PAREN_RE = re.compile(r"[\(\[\{].*?[\)\]\}]", re.DOTALL)


def normalise_label(label: str) -> str:
    if not isinstance(label, str):
        return label
    cleaned = _PAREN_RE.sub("", label)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or label


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------
def load_mappings() -> pd.DataFrame:
    frames = []
    for fname in FILES:
        path = MAPPING_DIR / fname
        if not path.exists():
            print(f"[warn] missing file: {path}")
            continue
        df = pd.read_csv(path, sep="\t", comment="#", dtype=str)
        df["source_file"] = fname
        frames.append(df)
        print(f"[info] {fname}: {len(df)} rows read")
    if not frames:
        raise SystemExit("No mapping files found.")
    df_all = pd.concat(frames, ignore_index=True)

    # Exact-duplicate mapping rows (same subject/object/predicate) double-
    # weight those pairs in the boxplots — drop them.
    before = len(df_all)
    df_all = df_all.drop_duplicates(subset=["subject_id", "object_id", "predicate_id"])
    dropped = before - len(df_all)
    if dropped:
        print(f"[info] dropped {dropped} exact-duplicate mapping rows")

    return df_all


def report_label_completeness(df_all: pd.DataFrame) -> pd.DataFrame:
    """Drop rows missing either label, but LOG per-file counts first so a
    file that silently contributes zero usable rows doesn't go unnoticed."""
    print("\n[info] rows per file before/after label-completeness filter:")
    before_counts = df_all["source_file"].value_counts()
    has_both = df_all["subject_label"].notna() & df_all["object_label"].notna()
    after_counts = df_all.loc[has_both, "source_file"].value_counts()
    for fname in FILES:
        b = int(before_counts.get(fname, 0))
        a = int(after_counts.get(fname, 0))
        flag = "  <-- CONTRIBUTES NOTHING" if b > 0 and a == 0 else ""
        print(f"    {fname}: {b} -> {a}{flag}")
    n_dropped = int((~has_both).sum())
    print(f"[info] dropped {n_dropped} rows missing subject_label and/or object_label\n")
    return df_all[has_both].copy()


# --------------------------------------------------------------------------
# Model loading / encoding
# --------------------------------------------------------------------------
def load_model(model_id: str):
    try:
        model = SentenceTransformer(model_id)
        return model, "sentence_transformers"
    except Exception as exc:
        print(f"[info] SentenceTransformer could not load {model_id} ({exc}); "
              f"trying model2vec …")
        from model2vec import StaticModel
        return StaticModel.from_pretrained(model_id), "model2vec"


def encode_labels(model, kind: str, labels: list[str]) -> np.ndarray:
    if kind == "sentence_transformers":
        vecs = model.encode(
            labels,
            batch_size=64,
            show_progress_bar=True,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
    elif kind == "model2vec":
        vecs = model.encode(labels)
        vecs = np.asarray(vecs, dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        vecs = vecs / norms
    else:
        raise ValueError(f"unknown model kind: {kind}")
    return vecs


# --------------------------------------------------------------------------
# Embedding cache
# --------------------------------------------------------------------------
def get_embeddings(
    labels: list[str],
    model_id: str,
    prefix: str,
    cache_path: Path,
) -> dict[str, np.ndarray]:
    cache: dict[str, np.ndarray] = {}
    if cache_path.exists():
        with cache_path.open("rb") as fh:
            cache = pickle.load(fh)
        print(f"[info] loaded {len(cache)} cached embeddings from {cache_path}")

    missing = [lab for lab in labels if lab not in cache]
    if missing:
        print(f"[info] encoding {len(missing)} new labels with {model_id} …")
        model, kind = load_model(model_id)
        prefixed = [prefix + lab for lab in missing]
        vectors = encode_labels(model, kind, prefixed)
        for lab, vec in zip(missing, vectors):
            cache[lab] = vec
        with cache_path.open("wb") as fh:
            pickle.dump(cache, fh)
        print(f"[info] cache updated -> {cache_path}")
    else:
        print("[info] all labels already in cache.")
    return cache


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def cosine_sim(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    sim = np.sum(a * b, axis=1)
    # float32 rounding on normalized vectors can push |sim| a hair past 1.0
    return np.clip(sim, -1.0, 1.0)


def embedding_similarity(
    df: pd.DataFrame,
    cache: dict[str, np.ndarray],
    subj_col: str,
    obj_col: str,
) -> np.ndarray:
    subj = np.stack([cache[l] for l in df[subj_col]])
    obj = np.stack([cache[l] for l in df[obj_col]])
    return cosine_sim(subj, obj)


def string_similarities(df: pd.DataFrame, subj_col: str, obj_col: str) -> dict[str, np.ndarray]:
    out = {}
    for name, fn in STRING_METRICS.items():
        out[name] = np.array(
            [fn(s, o) for s, o in zip(df[subj_col], df[obj_col])],
            dtype=float,
        )
    return out


def score_all(
    df: pd.DataFrame,
    cache_full: dict[str, np.ndarray],
    cache_norm: dict[str, np.ndarray],
) -> pd.DataFrame:
    df = df.copy()
    df["similarity_emb_full"] = embedding_similarity(df, cache_full, "subject_label", "object_label")
    df["similarity_emb_norm"] = embedding_similarity(df, cache_norm, "subject_label_norm", "object_label_norm")

    str_full = string_similarities(df, "subject_label", "object_label")
    str_norm = string_similarities(df, "subject_label_norm", "object_label_norm")
    for k, v in str_full.items():
        df[f"similarity_str_{k}_full"] = v
    for k, v in str_norm.items():
        df[f"similarity_str_{k}_norm"] = v

    return df


# --------------------------------------------------------------------------
# Null baseline: random, (almost certainly) unrelated label pairs
# --------------------------------------------------------------------------
def null_baseline(
    labels_full: list[str],
    labels_norm: list[str],
    cache_full: dict[str, np.ndarray],
    cache_norm: dict[str, np.ndarray],
    n_pairs: int = N_NULL_PAIRS,
    seed: int = RNG_SEED,
) -> pd.DataFrame:
    """Sample random label pairs to see where 'unrelated' sits for each
    technique. Without this, an absolute similarity score (esp. cosine
    similarity from an anisotropic sentence embedding model) is not
    interpretable on its own."""
    rng = np.random.default_rng(seed)
    idx_a = rng.integers(0, len(labels_full), n_pairs)
    idx_b = rng.integers(0, len(labels_full), n_pairs)
    keep = idx_a != idx_b
    idx_a, idx_b = idx_a[keep], idx_b[keep]

    a_full = [labels_full[i] for i in idx_a]
    b_full = [labels_full[i] for i in idx_b]
    a_norm = [labels_norm[i % len(labels_norm)] for i in idx_a]
    b_norm = [labels_norm[i % len(labels_norm)] for i in idx_b]

    df = pd.DataFrame({"a_full": a_full, "b_full": b_full, "a_norm": a_norm, "b_norm": b_norm})
    df["similarity_emb_full"] = embedding_similarity(df, cache_full, "a_full", "b_full")
    df["similarity_emb_norm"] = embedding_similarity(df, cache_norm, "a_norm", "b_norm")
    for name, fn in STRING_METRICS.items():
        df[f"similarity_str_{name}_full"] = [fn(s, o) for s, o in zip(df["a_full"], df["b_full"])]
        df[f"similarity_str_{name}_norm"] = [fn(s, o) for s, o in zip(df["a_norm"], df["b_norm"])]
    df["predicate"] = "randomPair"
    return df


def print_effect_sizes(df_used: pd.DataFrame, df_null: pd.DataFrame, model_name: str) -> None:
    sim_cols = [c for c in df_used.columns if c.startswith("similarity_")]
    print(f"\n=== [{model_name}] true-mapping mean vs. random-pair null mean (Cohen's d) ===")
    for col in sim_cols:
        true_vals = df_used[col].dropna()
        null_vals = df_null[col].dropna()
        pooled_std = np.sqrt((true_vals.std() ** 2 + null_vals.std() ** 2) / 2)
        d = (true_vals.mean() - null_vals.mean()) / pooled_std if pooled_std > 0 else float("nan")
        print(f"  {col:35s} true={true_vals.mean():.3f}  null={null_vals.mean():.3f}  d={d:.2f}")


# --------------------------------------------------------------------------
# Reporting / plotting
# --------------------------------------------------------------------------
def report(df: pd.DataFrame, model_name: str) -> pd.DataFrame:
    excluded_mask = df["predicate_id"].str.contains(
        "|".join(EXCLUDED_PREDICATES), case=False, na=False
    )
    if excluded_mask.any():
        print(f"[info] dropping {int(excluded_mask.sum())} excluded matches.")
    df_used = df[~excluded_mask].copy()
    df_used["predicate"] = df_used["predicate_id"].str.split(":").str[-1]

    sim_cols = [c for c in df_used.columns if c.startswith("similarity_")]
    print(f"\n=== [{model_name}] Mean similarity per technique × predicate ===")
    print(df_used.groupby("predicate")[sim_cols].mean().round(4).T)
    return df_used


def plot_faceted(df_used: pd.DataFrame, df_null: pd.DataFrame, model_name: str, out_path: Path) -> None:
    """3x2 grid: rows = predicate (randomPair/closeMatch/exactMatch), cols =
    label type (full/normalised). Each panel: one box per technique. The
    randomPair row anchors the scale at 'no relation' so the other two rows
    can be read as distance along an unrelated -> close -> exact spectrum."""
    df_combined = pd.concat([df_null, df_used], ignore_index=True, sort=False)
    predicates = [p for p in ["randomPair", "closeMatch", "exactMatch"]
                  if p in df_combined["predicate"].unique()]
    label_types = ["full", "norm"]
    sim_cols_all = [c for c in df_combined.columns if c.startswith("similarity_")]

    fig, axes = plt.subplots(len(predicates), len(label_types),
                              figsize=(7 * len(label_types), 4.5 * len(predicates)),
                              squeeze=False)

    for i, pred in enumerate(predicates):
        for j, ltype in enumerate(label_types):
            ax = axes[i][j]
            cols = sorted(c for c in sim_cols_all if c.endswith(f"_{ltype}"))
            sub = df_combined[df_combined["predicate"] == pred]
            long = sub.melt(value_vars=cols, var_name="technique", value_name="similarity")
            sns.boxplot(data=long, x="technique", y="similarity", order=cols, ax=ax, palette="Set2")
            ax.set_title(f"{pred} — {ltype} labels")
            ax.set_ylim(-0.2, 1.05)
            ax.axhline(0, color="grey", linewidth=0.5, linestyle="--")
            ax.set_xticklabels([c.replace("similarity_", "") for c in cols], rotation=30, ha="right")
            ax.set_xlabel("")

    fig.suptitle(f"Similarity by technique, faceted by predicate × label type — model: {model_name}\n"
                 f"(randomPair = null baseline for 'unrelated')")
    plt.tight_layout()
    plt.savefig(out_path, dpi=200)
    plt.close(fig)
    print(f"[info] faceted boxplot (with null baseline) written to {out_path}")


def plot_combined_models(results: dict, out_dir: Path, split_files: bool = False) -> None:
    """Cross-model comparison: string-similarity columns are identical
    across models (they don't depend on the embedding model), so they're
    taken once; each model's embedding column is renamed and kept side by
    side. Produces one 3x2 faceted figure with both models' emb_* columns
    plus all string techniques, or (if split_files=True) six separate
    single-panel PNGs for slide use.

    `results` maps model_name -> {"df_used": ..., "df_null": ...}.
    """
    model_names = list(results.keys())
    frames = []
    for pred_key, source_key in [("randomPair", "df_null"), (None, "df_used")]:
        # base frame: string columns from the first model (identical across models)
        base = results[model_names[0]][source_key if pred_key is None else source_key].copy()
        str_cols = [c for c in base.columns if c.startswith("similarity_str_")]
        keep_cols = str_cols + (["predicate"] if "predicate" in base.columns else [])
        merged = base[keep_cols].copy()
        if pred_key is not None:
            merged["predicate"] = pred_key
        for name in model_names:
            df_src = results[name][source_key]
            for ltype in ("full", "norm"):
                merged[f"similarity_emb_{name}_{ltype}"] = df_src[f"similarity_emb_{ltype}"].to_numpy()
        frames.append(merged)
    df_all_models = pd.concat(frames, ignore_index=True, sort=False)

    predicates = [p for p in ["randomPair", "closeMatch", "exactMatch"]
                  if p in df_all_models["predicate"].unique()]
    label_types = ["full", "norm"]

    def cols_for(ltype: str) -> list[str]:
        emb_cols = sorted(c for c in df_all_models.columns
                           if c.startswith("similarity_emb_") and c.endswith(f"_{ltype}"))
        str_cols = sorted(c for c in df_all_models.columns
                           if c.startswith("similarity_str_") and c.endswith(f"_{ltype}"))
        return emb_cols + str_cols

    if not split_files:
        fig, axes = plt.subplots(len(predicates), len(label_types),
                                  figsize=(8.5 * len(label_types), 4.5 * len(predicates)),
                                  squeeze=False)
        for i, pred in enumerate(predicates):
            for j, ltype in enumerate(label_types):
                ax = axes[i][j]
                cols = cols_for(ltype)
                sub = df_all_models[df_all_models["predicate"] == pred]
                long = sub.melt(value_vars=cols, var_name="technique", value_name="similarity")
                sns.boxplot(data=long, x="technique", y="similarity", order=cols, ax=ax, palette="Set2")
                ax.set_title(f"{pred} — {ltype} labels")
                ax.set_ylim(-0.2, 1.05)
                ax.axhline(0, color="grey", linewidth=0.5, linestyle="--")
                ax.set_xticklabels([c.replace("similarity_", "") for c in cols], rotation=30, ha="right")
                ax.set_xlabel("")
        fig.suptitle("Cross-model similarity comparison — embeddings vs. string techniques\n"
                     "(randomPair = null baseline for 'unrelated')")
        plt.tight_layout()
        out_path = out_dir / "boxplot__cross_model__faceted.png"
        plt.savefig(out_path, dpi=200)
        plt.close(fig)
        print(f"[info] cross-model faceted boxplot written to {out_path}")
    else:
        for pred in predicates:
            for ltype in label_types:
                cols = cols_for(ltype)
                sub = df_all_models[df_all_models["predicate"] == pred]
                long = sub.melt(value_vars=cols, var_name="technique", value_name="similarity")
                fig, ax = plt.subplots(figsize=(8, 5))
                sns.boxplot(data=long, x="technique", y="similarity", order=cols, ax=ax, palette="Set2")
                ax.set_title(f"{pred} — {ltype} labels (cross-model)")
                ax.set_ylim(-0.2, 1.05)
                ax.axhline(0, color="grey", linewidth=0.5, linestyle="--")
                ax.set_xticklabels([c.replace("similarity_", "") for c in cols], rotation=30, ha="right")
                ax.set_xlabel("")
                plt.tight_layout()
                out_path = out_dir / f"boxplot__cross_model__{pred}__{ltype}.png"
                plt.savefig(out_path, dpi=200)
                plt.close(fig)
                print(f"[info] cross-model boxplot written to {out_path}")


def _auc_and_d(true_vals: np.ndarray, null_vals: np.ndarray) -> tuple[float, float]:
    y = np.concatenate([np.ones(len(true_vals)), np.zeros(len(null_vals))])
    scores = np.concatenate([true_vals, null_vals])
    auc = roc_auc_score(y, scores)
    pooled_std = np.sqrt((true_vals.std() ** 2 + null_vals.std() ** 2) / 2)
    d = (true_vals.mean() - null_vals.mean()) / pooled_std if pooled_std > 0 else float("nan")
    return auc, d


def sensitivity_table(results: dict) -> pd.DataFrame:
    """Per-technique sensitivity to semantic distance, expressed on a scale-
    free basis (AUC-ROC of 'is this a true mapping vs. a random pair', plus
    Cohen's d as a secondary effect-size number). This is what answers 'how
    much does this technique actually move between random -> close ->
    exact', independent of the technique's own absolute value range —
    e5's compressed plateau and Jaro-Winkler's upward-shifted scale are
    both handled correctly because AUC is rank-based, not value-based.
    """
    model_names = list(results.keys())
    rows = []

    # String techniques are identical across models -> compute once.
    base_used = results[model_names[0]]["df_used"]
    base_null = results[model_names[0]]["df_null"]
    str_cols = [c for c in base_used.columns if c.startswith("similarity_str_")]
    for col in str_cols:
        null_vals = base_null[col].dropna().to_numpy()
        for pred in ["closeMatch", "exactMatch"]:  # add "relatedMatch" here once available
            true_vals = base_used.loc[base_used["predicate"] == pred, col].dropna().to_numpy()
            if len(true_vals) == 0 or len(null_vals) == 0:
                continue
            auc, d = _auc_and_d(true_vals, null_vals)
            rows.append({"technique": col.replace("similarity_str_", ""), "predicate": pred,
                         "auc": auc, "cohens_d": d})

    # Embedding techniques are model-specific.
    for name in model_names:
        used, null = results[name]["df_used"], results[name]["df_null"]
        for ltype in ("full", "norm"):
            col = f"similarity_emb_{ltype}"
            null_vals = null[col].dropna().to_numpy()
            for pred in ["closeMatch", "exactMatch"]:
                true_vals = used.loc[used["predicate"] == pred, col].dropna().to_numpy()
                if len(true_vals) == 0 or len(null_vals) == 0:
                    continue
                auc, d = _auc_and_d(true_vals, null_vals)
                rows.append({"technique": f"emb_{name}_{ltype}", "predicate": pred,
                             "auc": auc, "cohens_d": d})

    return pd.DataFrame(rows)


def plot_sensitivity_bars(sens_df: pd.DataFrame, out_path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(15, 6))
    for ax, metric, title in zip(
        axes,
        ["auc", "cohens_d"],
        ["AUC-ROC vs. random-pair null (rank-based, scale-free)", "Cohen's d (effect size)"],
    ):
        pivot = sens_df.pivot(index="technique", columns="predicate", values=metric)
        if "exactMatch" in pivot.columns:
            pivot = pivot.sort_values("exactMatch", ascending=False)
        pivot.plot(kind="bar", ax=ax)
        if metric == "auc":
            ax.axhline(0.5, color="grey", linestyle="--", linewidth=1, label="chance (0.5)")
            ax.set_ylim(0.4, 1.0)
        ax.set_title(title)
        ax.set_xlabel("")
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right", rotation_mode="anchor")
    plt.tight_layout()
    plt.savefig(out_path, dpi=200)
    plt.close(fig)
    print(f"[info] sensitivity bar chart written to {out_path}")


def write_csv(df_used: pd.DataFrame, out_path: Path) -> None:
    keep = [
        "source_file", "subject_id", "subject_label", "object_id", "object_label",
        "subject_label_norm", "object_label_norm",
        "predicate_id", "predicate", "mapping_justification",
    ] + [c for c in df_used.columns if c.startswith("similarity_")]
    keep = [c for c in keep if c in df_used.columns]  # tolerate missing optional cols
    df_out = df_used[keep].sort_values("similarity_emb_full", ascending=False)
    df_out.to_csv(out_path, index=False, float_format="%.6f")
    print(f"[info] sorted mapping pairs written to {out_path}")


# --------------------------------------------------------------------------
def main() -> None:
    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)

    df_all = load_mappings()
    print(f"\n[info] total mappings loaded: {len(df_all)}")
    df_all = report_label_completeness(df_all)

    df_all["subject_label_norm"] = df_all["subject_label"].map(normalise_label)
    df_all["object_label_norm"] = df_all["object_label"].map(normalise_label)

    labels_full = pd.unique(
        pd.concat([df_all["subject_label"], df_all["object_label"]]).dropna()
    ).tolist()
    labels_norm = pd.unique(
        pd.concat([df_all["subject_label_norm"], df_all["object_label_norm"]]).dropna()
    ).tolist()
    print(f"[info] unique full labels: {len(labels_full)}; normalised: {len(labels_norm)}")

    results = {}
    for cfg in MODEL_CONFIGS:
        name = cfg["name"]
        model_id = cfg["model_id"]
        prefix = cfg["prefix"]
        print(f"\n{'=' * 72}\n[model] {name}  ({model_id})\n{'=' * 72}")

        cache_full = get_embeddings(
            labels_full, model_id, prefix,
            out_dir / f"label_embeddings__{name}__full.pkl",
        )
        cache_norm = get_embeddings(
            labels_norm, model_id, prefix,
            out_dir / f"label_embeddings__{name}__norm.pkl",
        )

        df_scored = score_all(df_all, cache_full, cache_norm)
        df_used = report(df_scored, name)

        df_null = null_baseline(labels_full, labels_norm, cache_full, cache_norm)
        print_effect_sizes(df_used, df_null, name)

        write_csv(df_used, out_dir / f"mapping_similarities__{name}.csv")
        plot_faceted(df_used, df_null, name, out_dir / f"boxplot__{name}__faceted.png")

        results[name] = {"df_used": df_used, "df_null": df_null}

    # Cross-model comparison: both embedding models + string techniques
    # + random-pair null baseline, in one set of figures.
    plot_combined_models(results, out_dir, split_files=False)
    plot_combined_models(results, out_dir, split_files=True)

    sens_df = sensitivity_table(results)
    sens_df.to_csv(out_dir / "sensitivity_auc_d.csv", index=False)
    print("\n=== sensitivity (AUC / Cohen's d) vs. random-pair null ===")
    print(sens_df.pivot(index="technique", columns="predicate", values="auc").round(3))
    plot_sensitivity_bars(sens_df, out_dir / "sensitivity_bars.png")


if __name__ == "__main__":
    main()