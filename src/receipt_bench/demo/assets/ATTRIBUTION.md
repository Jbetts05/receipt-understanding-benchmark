# CORD v2 demo asset attribution

The three receipt images in this directory are redistributed from
[`naver-clova-ix/cord-v2`](https://huggingface.co/datasets/naver-clova-ix/cord-v2)
under the **Creative Commons Attribution 4.0 International (CC BY 4.0)**
license: <https://creativecommons.org/licenses/by/4.0/>.

**Attribution:** CORD — *A Consolidated Receipt Dataset for Post-OCR Parsing*
(Park et al., NeurIPS 2019 Document Intelligence Workshop), Naver Clova /
CLOVA.ai.

## Provenance

- Upstream dataset revision:
  `7f0115a4b758a71d6473b8d085751692da2fef98`
- Split: `test`
- Rows: `80` (clean/simple), `28` (complex/itemized), and `34`
  (challenging/noisy)
- The JPEG bytes are copied unchanged from the locally integrity-verified CORD
  cache. Their SHA-256 digests are recorded in `manifest.json`.
- The canonical golden JSON is a transformation of CORD v2 native annotations
  into this repository's expense-receipt schema. The transformation is limited
  to the native fields documented in `data/LICENSES.md`.
- `labeled_fields` in `manifest.json` preserves the native-field scoring mask.
  CORD v2's `menu` arrays are complete receipt annotations, so the
  collection-level `line_items` mask is explicit: unmatched predicted items are
  reported as extras. Unlabeled canonical fields remain `null` and must not be
  treated as negative ground truth.

No SROIE image, annotation, identifier, or derivative is included in these demo
assets. The demo selection and canonical-label transformation are repository
additions; no endorsement by the CORD authors is implied.
