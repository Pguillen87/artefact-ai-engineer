# Artefact AI Engineer challenge

## Data quality increment

The loader reads six canonical CSVs into a single immutable snapshot. It checks CSV headers and row shape, identifiers, duplicate keys, references, ISO dates, product/order states, finite nonnegative `Decimal` amounts, promotion bounds and flags, positive quantities and JSON objects (including duplicate keys). A structural error rejects the entire snapshot. No source file is modified. Run on the original challenge directory with exact approved SHA-256 verification:

```powershell
python -m artefact.report 'C:\App\Artefact\Desafio Técnico AI Engineer 2026\data' --baseline
```

The report contains table counts, hashes and field-level product findings with evidence and confidence; it does not include customer or order records. Treat reports as internal because they describe the private dataset. For synthetic input, omit `--baseline` and use the canonical filenames (`products.csv`, etc.). Run tests using `python -m unittest discover -s tests`. Tests use synthetic data; the baseline integration test is skipped when the local originals are unavailable.

The deterministic scanner currently detects selected brand discrepancies, keyboard/string count disagreements and some category/title disagreements. Its controlled vocabulary and patterns are deliberately conservative and **not exhaustive**; absence of a finding is not a guarantee of semantic correctness. Descriptive issues never invalidate independently typed price or stock. They do not correct the source, establish which field is true, or implement a seller handoff.

The source dataset and restricted documents are excluded from this public repository. Python standard library is sufficient for this increment.

## Commercial rules (incremental)

`artefact.commercial` implements the approved demo boundaries: metropolitan shipping is R$35 below R$500 and free at or above R$500; an unconfirmed other destination requires a quote. Installment eligibility uses per-installment floors of R$50 for 1–3, R$80 for 4–6 and R$100 for 7–12 installments. This follows the approved specific tier rules. PIX gives 5% off only when no promotion has already been applied. These functions do not infer a customer's location, promotion eligibility or historical order values.

## Read-only demo services

`Catalog` searches by name, exposes typed commercial fields and masks a description or individual specification when the scanner flags a conflict. Availability means `active` and positive stock; promotion flags describe only the source snapshot, with ambiguous active promotions explicitly reported. Unflagged fields are not guaranteed correct because scanning is not exhaustive.

`OrderService` is restricted to the challenge's fictional data: an exact normalized unique full-name match returns only selected order fields and recorded totals, never recalculated current prices. A name is not authentication and this interface must not be used for real customer data. Policies are short demo decisions attributed to ARTEFACT-11, not the restricted manual or a claim of current store policy.

## Telegram and LLM demonstration (experimental)

The optional demo uses Telegram long polling and the Responses API tool cycle: the model selects tools, the server validates and executes them, and the results are returned to the model for a natural-language answer. At most three distinct read-only tools and two model calls are allowed per turn. Conversation history is bounded, separated by private chat, memory-only and lost on restart. The application checks numerical claims and some known contradictions, but these heuristics cannot prove that every generated claim is grounded. Human evaluation remains necessary. It is a fictional-data demonstration, not authentication for real customers. The original files are mounted read-only and checked against the approved hashes.

Before a real run, independently confirm provider access, data handling and an approved spending limit. Set `ARTEFACT_SOURCE_DIR`, `OPENAI_MODEL`, `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `OPENAI_BUDGET_USD`, `OPENAI_MAX_CALLS`, `OPENAI_INPUT_USD_PER_MILLION` and `OPENAI_OUTPUT_USD_PER_MILLION`; set `ENABLE_REAL_LLM=1` only after approval. Then use `docker compose up --build`. Prices must match the selected model and service tier. Each API call reserves a conservative estimated amount and one call in the persistent `agent_state` volume before sending. It fails closed when either limit would be exceeded. Inspect aggregate calls, reserved USD and provider-reported token counts with `docker compose run --rm agent python -m artefact.usage /state/agent.sqlite`. Reservations include a margin and are retained after failures; they are estimates, not invoice data or a guarantee against all billing differences. Do not delete the volume to reset a budget without a new approved budget. Never commit credentials or raw data. User messages and relevant fictional tool results are sent to the provider; `store=false` is requested.

Tests use fake transports and do not establish real Telegram delivery or model behavior. Update IDs are claimed persistently before processing, giving at-most-once handling: a crash after the claim may lose a reply. Telegram delivery itself has no exactly-once guarantee. There is no complete rate limiting or seller messaging. Do not run a public or production bot with these limitations. Full weekday opening hours are not established in the approved sources used here.

## Local Codex feasibility demo

`python -m artefact.codex_demo` runs a host-only experiment using the already authenticated Codex CLI, without an API key or OAuth implementation. It always creates its own small synthetic dataset; it does not accept a challenge-data directory or connect Telegram. The bridge asks Codex for a schema-constrained JSON plan, validates it and executes allowlisted functions in Python, then asks Codex for an answer based on the results. This is structured-output orchestration, not the Responses API's native function-calling protocol. The Responses provider above remains available separately.

Install and sign in to the official Codex CLI first. Set `CODEX_DEMO_APPROVED=1`, a positive `CODEX_DEMO_MAX_CALLS`, and `CODEX_DEMO_STATE_PATH` to a writable local SQLite path. Run `python -m artefact.codex_demo` and enter one question per line. The persisted counter is a local call cap, not a measurement or guarantee of the plan's quota. Each data-backed answer can use two calls. The CLI runs with a temporary empty working directory, read-only sandbox, ephemeral sessions, user configuration ignored, shell and other tool features disabled, web search disabled, and no API-key or Telegram-token environment variables forwarded. It uses the local Codex authentication normally without reading or exporting credentials.

This is a constrained feasibility experiment, not a security certification or a production customer-facing service. Output checks are heuristic, Codex plan limits and access can change, the host needs the CLI and an authenticated session, and a command-disabled synthetic test does not prove all future versions immune to prompt injection. Telegram and real challenge data have not been tested through this bridge.

## Host-only Codex Telegram test

`python -m artefact.codex_telegram` is an opt-in Windows-host test path for the original fictional challenge dataset. It does not move the Codex session to Docker or alter the Responses API setup. Set `ENABLE_CODEX_TELEGRAM=1`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_ID` (one private chat ID), `ARTEFACT_DATA_DIR`, `CODEX_DEMO_STATE_PATH`, and a positive `CODEX_DEMO_MAX_CALLS` in the host process. The original six files must match their approved SHA-256 values. Use a dedicated, writable local SQLite state file outside the input directory and repository; it records update claims and aggregate Codex calls, not chat messages or secrets.

Only private messages from the configured chat reach the model. The chat restriction is an experimental channel boundary; the approved fictional order lookup remains an exact, unique full-name match and is not authentication for real people. The local Codex subprocess disables shell tools, uses a read-only sandbox and does not receive the Telegram token or `OPENAI_API_KEY` through its environment. The runtime is intentionally not enabled by default and must be independently tested with the real bot before claiming a working Telegram demonstration. Only one poller should run per bot. Update claims are at-most-once: a crash may lose a reply.
