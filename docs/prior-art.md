# Prior art

No public benchmark located during planning matched the full experiment:
receipt images evaluated on one holdout across current Foundry models, Content
Understanding, and Document Intelligence while reporting extraction quality,
latency, and cost.

The implementation reuses methodological ideas from:

- [Braintrust receipt extraction](https://www.braintrust.dev/docs/cookbook/recipes/ReceiptExtraction):
  zero-shot image extraction on 100 SROIE receipts with transparent prompts and
  model-level timing. This benchmark instead makes one full-schema call per
  receipt rather than one call per field.
- [LMDX](https://aclanthology.org/2024.findings-acl.899/): OCR text and
  coordinates, JSON extraction, grounding, and receipt evaluation on CORD.
- [Donut](https://github.com/clovaai/donut): OCR-free hierarchical receipt
  parsing and the CORD JSON target format.
- [Multi-Modal Vision vs. Text-Based Parsing](https://arxiv.org/abs/2509.04469):
  direct-image versus document-to-Markdown ablation for invoice extraction.
- [Document Data Extraction Benchmark](https://github.com/theam/document-data-extraction-benchmark):
  shared field taxonomy, deterministic normalization, and provider-level
  accuracy, latency, and cost collection.
- [Azure Multimodal AI and LLM Processing Accelerator](https://github.com/Azure/multimodal-ai-llm-processing-accelerator):
  component timing, raw intermediate output retention, Document Intelligence
  grounding, and schema validation.

Marketing comparisons without disclosed datasets, prompts, and scoring code are
not used as baselines.
