# Artefact AI Engineer challenge

## Data quality increment

The loader reads six canonical CSVs into a single immutable snapshot. It checks CSV headers and row shape, identifiers, duplicate keys, references, ISO dates, product/order states, finite nonnegative `Decimal` amounts, promotion bounds and flags, positive quantities and JSON objects (including duplicate keys). A structural error rejects the entire snapshot. No source file is modified. Run on the original challenge directory with exact approved SHA-256 verification:

```powershell
python -m artefact.report 'C:\App\Artefact\Desafio Técnico AI Engineer 2026\data' --baseline
```

The report contains table counts, hashes and field-level product findings with evidence and confidence; it does not include customer or order records. Treat reports as internal because they describe the private dataset. For synthetic input, omit `--baseline` and use the canonical filenames (`products.csv`, etc.). Run tests using `python -m unittest discover -s tests`. Tests use synthetic data; the baseline integration test is skipped when the local originals are unavailable.

The deterministic scanner currently detects selected brand discrepancies, keyboard/string count disagreements and some category/title disagreements. Its controlled vocabulary and patterns are deliberately conservative and **not exhaustive**; absence of a finding is not a guarantee of semantic correctness. Descriptive issues never invalidate independently typed price or stock. They do not correct the source, establish which field is true, or implement a seller handoff. No policy, bot, LLM, order authorization or administrative interface is implemented yet.

The source dataset and restricted documents are excluded from this public repository. Python standard library is sufficient for this increment.

## Commercial rules (incremental)

`artefact.commercial` implements the approved demo boundaries: metropolitan shipping is R$35 below R$500 and free at or above R$500; an unconfirmed other destination requires a quote. Installment eligibility uses per-installment floors of R$50 for 1–3, R$80 for 4–6 and R$100 for 7–12 installments. This follows the approved specific tier rules. PIX gives 5% off only when no promotion has already been applied. These functions do not infer a customer's location, promotion eligibility or historical order values.

## Read-only demo services

`Catalog` searches by name, exposes typed commercial fields and masks a description or individual specification when the scanner flags a conflict. Availability means `active` and positive stock; promotion flags describe only the source snapshot, with ambiguous active promotions explicitly reported. Unflagged fields are not guaranteed correct because scanning is not exhaustive.

`OrderService` is restricted to the challenge's fictional data: an exact normalized unique full-name match returns only selected order fields and recorded totals, never recalculated current prices. A name is not authentication and this interface must not be used for real customer data. Policies are short demo decisions attributed to ARTEFACT-11, not the restricted manual or a claim of current store policy.

## Telegram and LLM demonstration (experimental)

The optional demo uses Telegram long polling and the OpenAI Responses API for tool selection. Tool names and arguments are validated in application code; results are rendered directly from deterministic services, not model-written claims. The current presentation is structured JSON, not polished conversational copy. It is a fictional-data demonstration, not authentication for real customers. Only private Telegram chats are handled. The original files are mounted read-only and checked against the approved hashes.

Before a real run, independently confirm provider access, data handling and the authorized spending limit. Set `ARTEFACT_SOURCE_DIR`, `OPENAI_MODEL`, `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, and explicitly set `ENABLE_REAL_LLM=1` only after that approval. Then use `docker compose up --build`; omit the flag to keep the real LLM disabled. The official model documentation lists `gpt-6-luna` with function calling; account access has not been verified. Never commit credentials or raw data. User messages are sent to the configured provider when the demo is enabled; responses are requested with `store=false`.

Tests use fake transports and do not establish real Telegram delivery or model behavior. Deduplication and polling offsets are in memory and do not survive restart; there is no durable exactly-once guarantee. There is no automatic budget enforcement, complete rate limiting, or seller messaging. Do not run a public or production bot with these limitations. Full weekday opening hours are not established in the approved sources used here.
