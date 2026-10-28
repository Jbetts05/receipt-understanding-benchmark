# Pricing snapshot

`configs/pricing.yaml` records USD retail rates captured from the official
[Azure Retail Prices API](https://prices.azure.com/api/retail/prices) on
2026-08-31 for Sweden Central and Global Standard processing. Grok 4.6 is
confirmed as a Foundry Model sold by Azure in Microsoft Learn; its token rates
come from xAI's official model rate table because the Azure Retail Prices API
has not yet published a Grok 4.6 meter.

| Component | Input | Output | Page or transaction |
|-----------|------:|-------:|--------------------:|
| GPT-5.6 Sol | $5.00/M tokens | $30.00/M tokens | - |
| GPT-5.6 Terra | $2.00/M tokens | $12.00/M tokens | - |
| GPT-5.6 Luna | $0.20/M tokens | $1.20/M tokens | - |
| GPT-5.4 Mini | $0.75/M tokens | $4.50/M tokens | - |
| GPT-5.2 | $1.75/M tokens | $14.00/M tokens | - |
| text-embedding-3-large | $0.13/M tokens | - | - |
| Grok 4.6 (<200K prompt tokens) | $2.00/M tokens | $6.00/M tokens | - |
| Grok 4.6 (>=200K prompt tokens) | $4.00/M tokens | $12.00/M tokens | - |
| Mistral Large 3 | $0.50/M tokens | $1.50/M tokens | - |
| Mistral Document AI 2512 | - | - | $3.00/1K pages |
| Mistral OCR 4.0 | - | - | $4.00/1K pages |
| Document Intelligence prebuilt | - | - | $10.00/1K pages |
| Azure Vision Image Analysis Group 1 | - | - | $1.00/1K transactions |
| Content Understanding Read | - | - | $1.00/1K pages |
| Content Understanding Layout | - | - | $5.00/1K pages |
| Content Understanding contextualization | - | - | $1.00/M tokens |

Content Understanding custom extraction additionally incurs its configured
completion-model input and output token rates. Content Understanding
`prebuilt-receipt` additionally incurs GPT-5.2 completion and
text-embedding-3-large usage reported by the analyzer.

Standalone OCR rows include only their OCR service charge and latency. Assisted
OCR and native-service + Luna rows include every recorded upstream component
plus GPT-5.6 Luna input/output usage; no downstream assistance is omitted from
the row total.

The Grok threshold is evaluated from each request's recorded input-token count;
when it reaches 200,000 tokens, the higher input and output rates apply to the
whole request. Receipt prompts in this benchmark are normally well below that
threshold.

The hosted runtime validates pricing coverage at startup. Every configured
solution, downstream normalizer, and disclosed internal model must have a
non-empty rate entry, so future runs cannot silently start with an unpriced
configuration. Active reports show cost accrued by completed calls; terminal
reports show the full estimated run cost.

Rates can change. Every published run includes this snapshot timestamp and must
not be compared against a later run without accounting for price changes.
