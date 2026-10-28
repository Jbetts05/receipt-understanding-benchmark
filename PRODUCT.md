# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

People evaluating receipt-extraction approaches. The product should remain understandable without assuming a narrowly technical or finance-only audience.

## Product Purpose

Receipt Bench exists only to compare receipt-understanding services. It runs the same receipt through explicit standalone or assisted configurations, compares every output with the known answer, and exposes quality, latency, usage, and cost evidence. Success means a person can inspect the source receipt, golden answer, individual outputs, and aggregate results before choosing a service.

## Positioning

One self-deployable workspace combines dataset-native evaluation, parallel Azure model and document-service execution, receipt-level visual inspection, and durable evidence instead of reducing the experiment to a single benchmark score.

## Operating Context

Users select SROIE, CORD, or a custom receipt project; choose receipts and compatible extractors; launch a parallel run; inspect field-level differences and pipeline evidence; and download the run artifacts.

## Capabilities and Constraints

- SROIE and CORD use separate native schemas, prompts, few-shot examples, and scores.
- Custom projects support a complete labelled ZIP fast path as well as manual
  receipt images, a JSON Schema, an extraction prompt, and optional golden answers.
- Runs and artifacts are durable and restorable until the user explicitly deletes them.
- The hosted product uses one App Service with private Azure Storage and Key Vault networking.
- Bring-your-own model support is deferred.
- Fine-tuned and custom-trained models are out of scope; schema configuration,
  prompting, deterministic mappings, and explicit downstream assistance are allowed.
- Public SROIE seeding requires documented redistribution rights.

## Brand Commitments

The product name is **Receipt Bench**. Product copy is direct, operational, and evidence-led. The supplied evaluation-studio reference is the binding UI hierarchy target.

## Evidence on Hand

The repository includes 80 SROIE and 20 CORD samples, project schemas and prompts, 22 explicit standalone or assisted service configurations, mock and live execution, full-run artifacts, and an attached UI reference supplied by the user. Do not fabricate customer claims or benchmark results.

## Product Principles

- Show the receipt, answer, and outputs together.
- Keep quality, latency, usage, and cost as separate evidence.
- Preserve dataset-native evaluation semantics.
- Make every aggregate traceable to receipt-level results.
- Never hide OCR, normalization, or model assistance inside a service label.
- Score service limitations as measured limitations rather than excluding them.
- Support both self-deployment and a hosted demonstration.

## Current Acceptance Goal

1. Disclose that the SROIE and CORD JSON Schemas are repository-authored contracts derived from each dataset's native labels, and make the exact schema and derivation inspectable.
2. Show standalone services and assisted service-plus-model configurations as separate rows. Standalone OCR uses OCR-reference metrics; assisted extraction uses schema quality plus combined latency and cost.
3. Remove verbose rights, leakage, and attribution commentary from the working surface while keeping one discreet dataset-source link and repository attribution.
4. Make Receipt inspector navigation load and focus a valid receipt, with explicit unavailable and loading states.
5. Give SROIE and CORD equivalent service coverage and controls while preserving their separate schemas, prompts, examples, and scores.
6. Make every pinned contract inspectable: full schema, prompt, example IDs, versions, hashes, and normalizer identity.
7. Give Models & services and Configure run distinct destinations and active navigation states.
8. Prevent receipt-inspector text overlap with one horizontal scroll owner, isolated sticky columns, constrained widths, and robust wrapping.
9. Name every assistance model explicitly, including GPT-5.6 Luna for current OCR-normalizer configurations and the Content Understanding analyzer's supported internal model.
10. Make every rendered table sortable, and provide accessible definitions for Field F1, exact match, success, latency, cost, progress, coverage, and OCR metrics.
11. Remove unnecessary no-gold commentary from receipt upload.
12. State clearly that a custom project has one pinned schema and prompt shared by all receipts; do not describe this as appending schemas.
13. Provide Select all and Clear actions wherever users choose solutions.
14. Replace vague reference-only/unsupported states with explicit standalone, assisted, or custom-analyzer configuration states; reserve unavailable for genuinely unconfigured calls.
15. Allow irreversible deletion of terminal runs and custom datasets from the UI after confirmation. Protect built-in SROIE/CORD, reject deletion while work is active, and cascade deletion through durable Local/Azure records and artifacts.
16. Import complete labelled datasets as bounded, explicitly paired ZIP packages; validate the entire package before publication and freeze a content-addressed revision.
17. Keep fixed-contract native services and standalone OCR without transcripts runnable as reference evidence, without presenting them as schema-quality comparisons.
18. Report per-field matched, wrong, partial, missing, and extra counts alongside coverage, exact rate, and Field F1; report token/page/attempt totals, P50/P95 latency, and accrued or complete estimated cost separately.
19. Provide an Admin Center using the site's existing access model, with sortable deployment-wide dataset, run, and service inventories; allow guarded deletion of terminal runs and custom datasets, protect built-ins and active work, and keep Azure services read-only.

## Accessibility & Inclusion

Core navigation, run controls, tables, inspectors, and responsive drawers must remain keyboard accessible and understandable without color alone.
