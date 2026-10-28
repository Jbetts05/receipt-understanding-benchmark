# Bring your own labelled dataset

Receipt Bench supports a fast path for an existing, authoritative labelled
receipt dataset. The import creates one immutable dataset revision; it does not
train or fine-tune any model.

## ZIP contract

The ZIP must contain only files declared by a root `dataset.json`:

```text
dataset.json
schema.json
images/
  receipt-001.jpg
answers/
  receipt-001.json
ocr/                         # optional; include for every sample or none
  receipt-001.txt
```

Example `dataset.json`:

```json
{
  "format_version": "1.0",
  "name": "EMEA expense receipts",
  "description": "Authoritative labelled employee expense receipts.",
  "schema": "schema.json",
  "prompt": "Extract the labelled receipt fields and return strict JSON.",
  "selected_solution_ids": [
    "gpt-5.6-luna",
    "content-understanding-custom",
    "mistral-ocr-4.0"
  ],
  "samples": [
    {
      "id": "receipt-001",
      "title": "Cafe receipt",
      "image": "images/receipt-001.jpg",
      "answer": "answers/receipt-001.json",
      "ocr_text": "ocr/receipt-001.txt"
    }
  ]
}
```

`selected_solution_ids` is optional. Omitting it selects every runnable
out-of-box configuration. `ocr_text` is also optional, but OCR references must
be supplied for every sample or omitted for every sample.

## Validation

Before publishing any project, the server validates:

- normalized relative ZIP paths, unique case-insensitive filenames, no links,
  encryption, traversal, or undeclared files;
- compressed and expanded size, archive entry count, sample count, individual
  image and JSON limits;
- unique sample IDs and duplicate image content;
- JPEG/PNG signatures, dimensions, and pixel count;
- JSON Schema validity and configured structured-output limits;
- every answer against the shared schema;
- every requested solution ID;
- optional UTF-8 OCR references.

Failed preflight creates no project. A persistence failure triggers project-wide
rollback. A successful import is marked `frozen` and has a stable revision hash.

## Comparison semantics

- Direct multimodal, OCR + GPT-5.6 Luna, native + GPT-5.6 Luna, and Content
  Understanding schema-configured paths target the imported schema.
- Content Understanding schema configuration creates a no-training analyzer
  from supported schema fields on first use.
- Fixed-contract Content Understanding and Document Intelligence prebuilt
  outputs remain inspectable reference evidence for arbitrary schemas; they are
  not force-mapped or presented as schema accuracy.
- Standalone OCR always reports output, latency, and cost. It receives OCR
  quality metrics only when full-page transcripts were imported.
- Custom-trained, fine-tuned, template-trained, and neural-trained models are
  outside the product scope.
