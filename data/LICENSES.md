# Dataset Licensing and Provenance

Receipt Bench's built-in source set is **80 SROIE + 20 CORD v2**
receipts, acquired and normalized by `receipt_bench.datasets`. Raw source images
are never committed from `data/raw/` (see `.gitignore`). The repository contains
three unchanged CC BY 4.0 CORD preview images under
`src/receipt_bench/demo/assets/cord-v2/`; hosted seeding downloads the complete
fixed CORD selection from its pinned source.
Deterministic source-id/selection metadata under `data/manifests/` is committed,
but the generated normalized ground-truth manifest is not.

The generated `data/manifests/benchmark.jsonl` contains normalized native
labels and is also excluded from Git because SROIE's original annotations are
not openly licensed for redistribution.

## SROIE (80 samples)

- **Source mirror:** [`zzzDavid/ICDAR-2019-SROIE`](https://github.com/zzzDavid/ICDAR-2019-SROIE),
  pinned to commit `27be4271b251c256f695acbade9a801bffe85994`.
- **Selected ids:** deterministic, zero-padded `000` through `079` (80 of the
  mirror's `data/` folder).
- **Split correction:** the pinned mirror commit's `data/` folder (626 image/
  key/box triples) is the SROIE **trainval-style labeled data** redistributed
  by the mirror maintainer, **not** the official ICDAR 2019 SROIE 400-image
  held-out test split (which was never publicly released with ground truth).
  `receipt_bench.datasets.config.SroieSourceConfig.split` and every generated
  `ReceiptSample.split` for SROIE are therefore documented as `"trainval"`,
  never `"test"`.
  - **Leakage risk:** because this trainval-style data has been publicly
    mirrored for years, it may already appear in the training or evaluation
    data of other public OCR/LLM models. Any benchmark run against these 80
    samples must not be represented as evaluation against an untouched,
    held-out test set -- treat results with that leakage risk in mind.
- **Provenance and licensing caveats (read before using or redistributing):**
  - The official SROIE (ICDAR 2019 Scanned Receipts OCR and Information
    Extraction) dataset is distributed by the ICDAR 2019 SROIE task organizers
    under **gated, research-only terms**: it requires registration/agreement
    with the task organizers, and is not an openly-licensed public dataset.
  - `zzzDavid/ICDAR-2019-SROIE` is an independent, third-party GitHub mirror of
    that data. **The mirror repository's own GitHub license (if any) governs
    only the mirror maintainer's contribution (e.g. any wrapper code) and does
    not, and cannot, relicense the underlying receipt images or annotations.**
    The original SROIE research-only restriction still applies to the receipt
    images and their transcriptions themselves, regardless of where they are
    mirrored from.
  - The project owner explicitly confirmed on 2026-08-30 that they hold
    redistribution permission for this experiment. Hosted deployment records
    that confirmation through `SROIE_RIGHTS_EVIDENCE` before enabling SROIE
    seeding. This statement does not generally relicense SROIE; downstream
    operators remain responsible for ensuring their use and audience are within
    their own authorization.
  - Source downloads remain in the `.gitignore`-excluded
    `data/raw/images/sroie/<commit>/` cache and are not committed to this
    repository.
  - The SROIE project extracts the four native key fields exactly:
    `company`, `address`, `date`, and `total`.
  - Two receipts are prompt examples. Their two byte-identical counterparts are
    also excluded to prevent prompt-image leakage, leaving 76 unique scored
    images.
  - The box CSV remains the OCR reference for examples/pipeline inspection; it
    is not golden key-value output.

## CORD v2 (20 samples)

- **Source:** [`naver-clova-ix/cord-v2`](https://huggingface.co/datasets/naver-clova-ix/cord-v2)
  on Hugging Face, pinned to revision `7f0115a4b758a71d6473b8d085751692da2fef98`.
- **License:** **CC-BY-4.0** (per the dataset's Hugging Face card). Attribution:
  CORD -- A Consolidated Receipt Dataset for Post-OCR Parsing (Park et al.,
  NeurIPS 2019 Document Intelligence Workshop), Naver Clova / CLOVA.ai.
- **Selection:** 20 of the `test` split's 100 rows, selected as
  `sorted(random.Random(20260828).sample(range(100), 20))` ->
  `[3, 11, 15, 26, 28, 34, 35, 42, 46, 47, 49, 58, 63, 68, 72, 73, 74, 80, 85, 98]`
  (also committed in `configs/datasets.yaml` and `data/manifests/cord_selection.json`,
  and re-validated against this exact formula whenever the config is loaded).
- **Fetch method:** only the Hugging Face `datasets-server` `rows` API is used
  (`.../rows?dataset=naver-clova-ix%2Fcord-v2&config=default&split=test&offset={index}&length=1`,
  one request per selected index), and only its returned signed image URL is
  downloaded, immediately (those URLs are time-limited). **Parquet files are
  never downloaded.**
- **Native evaluation contract:** the project uses the selected rows' complete
  normalized `gt_parse` answer: `menu`, optional `sub_total`, and `total`.
  `menu` is normalized to an array while preserving every observed native
  field. CORD amounts are
  Indonesian Rupiah (IDR) with no minor unit; monetary strings use `.`/`,` as
  thousands separators only for normalized comparison, while the extracted
  project-native answer retains strings and quantities (`cnt`) may be decimal.
- CORD's `menu` array is complete. Unmatched predicted entries and actual-only
  object fields are reported as extras.
- Two deterministic CORD receipts are prompt examples and excluded, leaving 18
  scored receipts. Rare schema fields remain represented in the scored set.
- The 20-receipt benchmark selection remains in the `.gitignore`-excluded
  `data/raw/images/cord/<revision>/` cache layout. Exactly three unchanged CORD
  JPEGs (test rows 80, 28, and 34) are copied into tracked preview assets under
  CC BY 4.0. No SROIE artifact is bundled.
  See `src/receipt_bench/demo/assets/ATTRIBUTION.md`.

## General acquisition notes

- Every download is atomic (write-to-temp-then-replace), verified over HTTP
  (non-200 responses fail explicitly) and by SHA256, and idempotently
  re-verified from local disk on repeat runs (no redundant re-download) unless
  explicitly forced. A `.sha256` integrity sidecar is persisted alongside every
  cached file on first download and is required to match on every later reuse;
  a cached file with a missing, malformed, or mismatched sidecar always fails
  explicitly (with guidance to re-run with `force=True`) rather than being
  silently trusted.
- **Cache provenance keying:** each dataset's cache root is keyed by its
  pinned source identifier, not just the dataset name --
  `data/raw/images/sroie/<commit>/` and `data/raw/images/cord/<revision>/`.
  This guarantees that changing a config pin (a new SROIE commit or CORD
  revision) always resolves to a brand-new, empty cache path and therefore
  always fetches and integrity-verifies fresh bytes for the new pin, rather
  than risking a silent reuse of bytes downloaded under a previous pin merely
  because their SHA256 sidecars are still internally self-consistent. Stale
  caches from superseded pins are simply orphaned directories and may be
  deleted manually; they are never read again once the config pin changes.
- Acquisition and normalization never fall back silently: a malformed or
  missing response, an unparsable date/money string, or a config that fails
  the deterministic-selection check raises an explicit error and aborts the
  run rather than skipping or guessing.
