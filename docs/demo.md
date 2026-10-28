# Receipt Bench guide

Receipt Bench is the interactive product surface for the repository. It
serves the React application and FastAPI API from one origin and executes
receipt extraction work through durable project, run, task, result, and artifact
contracts.

## Local launch

From a clean checkout:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
receipt-bench demo
```

The local profile:

- binds to loopback only;
- opens <http://127.0.0.1:8000>;
- uses filesystem-backed durable state under `.receipt-bench/`;
- runs deterministic mock extractors;
- requires no `.env`, Docker, Node.js, or Azure account;
- seeds permitted built-in projects before starting its queue worker.

Use `receipt-bench demo --no-open` on a headless machine.

## Built-in projects

### SROIE

- 80 fixed source receipts.
- Dataset-native schema: `company`, `address`, `date`, `total`.
- Two prompt examples and two byte-duplicate counterparts are not scored.
- 76 unique images are scored.
- The project owner confirmed redistribution permission on 2026-08-30. Hosted
  seeding is enabled when that confirmation is recorded in
  `SROIE_RIGHTS_EVIDENCE`.
- The UI displays the trainval/training-leakage caveat.

### CORD v2

- 20 pinned test-split receipts.
- Dataset-native normalized `gt_parse` schema.
- Two prompt examples are not scored; 18 receipts are scored.
- Menu items are matched independent of order and unmatched predictions are
  extras.
- CC BY 4.0 attribution is displayed in the UI and downloads.

The projects use different schemas, prompts, examples, normalization, and
aggregates. Their quality scores are never combined.

## Evaluation workspace

The primary screen follows an evaluation-studio layout:

1. grouped navigation for projects, extractors, evaluations, and reports;
2. run/dataset context and completion status;
3. compact quality, exactness, failure, latency, and cost KPIs;
4. extractor performance table;
5. receipt image, golden answer, and all solution outputs side by side;
6. synchronized field states: matched, partial, wrong, missing, and extra;
7. raw response, parsed answer, OCR/normalizer stages, tokens/pages, retries,
   latency, and estimated cost.

Reference-only solutions remain inspectable but do not receive comparable
quality metrics.

## Custom projects

Release 1 accepts:

- project name and instructions;
- multiple JPEG/PNG receipts;
- a Draft 2020-12 JSON Schema;
- optional golden JSON per receipt;
- compatible direct and OCR-normalizer solutions.

The server validates file signatures, image dimensions, body sizes, schema
depth/nodes, and gold values. Browser validation is only an early convenience.

Without gold, the product shows outputs, schema compliance, agreement, and
telemetry but never claims accuracy.

## Durable execution

Each selected `(receipt, solution)` pair becomes a durable task. Raw output is
persisted before parsing. Queue visibility is renewed during long calls, retry
budgets are task-level, poison failures become terminal results, and one fenced
finalizer produces aggregate/download artifacts after every pair is terminal.

SSE reads durable events and can reconnect with a cursor; it does not depend on
the process that ran the task.

## Azure-hosted profile

`azd up` provisions one Linux App Service plus private Storage and Key Vault:

- App Service serves the UI/API and runs one bounded queue worker.
- Blob stores immutable inputs and outputs.
- Queue dispatches extraction tasks.
- Table stores project/run/task/result indexes.
- Storage and Key Vault public network access are disabled.
- App Service reaches them through VNet Integration, private endpoints, and
  private DNS.
- The browser receives no Blob URL, SAS, storage key, or provider credential.
- The app managed identity accesses Storage/Key Vault; AI adapters read the
  existing Foundry key from Key Vault.

Built-in seeding occurs inside the VNet-integrated app under a renewable,
fenced lease. The operator laptop never uploads through a disabled public
Storage endpoint.

## Hosted-risk notice

By explicit owner decision, the planned public research deployment has no
authentication, CAPTCHA, visitor quota, application spending stop, automatic
retention deletion, or operator kill switch. Anonymous visitors can trigger
paid calls, and uploaded data plus run artifacts are retained indefinitely.
