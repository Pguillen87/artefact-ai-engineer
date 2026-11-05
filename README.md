# Emporio da Musica AI Support Agent

A local Python prototype for the Artefact AI Engineer challenge. It helps the fictional Emporio da Musica store answer catalog, order and demonstration-policy questions through a browser chat. Customer-facing replies are in Brazilian Portuguese. This README is in English as a submission consistency choice.

## Run locally

Requirements: Docker Desktop with Linux containers and Docker Compose v2.

Set a unique local administrator password of at least 16 characters before startup. Compose has a demo fallback if the variable is omitted; do not rely on that fallback.

```powershell
$env:ARTEFACT_ADMIN_PASSWORD = Read-Host 'Choose a unique local password (16+ characters)'
docker compose up -d --build
```

Open http://localhost:8080. The administration interface binds to loopback by default. Keep this prototype local; it is not configured for public or multi-user deployment.

On a new installation, import the six challenge CSV files through the administration interface:

- categories.csv
- customers.csv
- orders.csv
- order_items.csv
- products.csv
- promotions.csv

The supplied source data is not included in this repository. Configure Gemini or OpenAI and provide a provider key through the administration interface. Saving a key does not itself make a model request; the explicit connection test does. Provider credentials are stored in the local configuration volume. Do not commit API keys, environment files, databases, backups or source datasets.

Useful local commands:

```powershell
docker compose logs -f admin agent
docker compose down
```

Named Docker volumes retain application data after containers stop. Treat those volumes as sensitive local state.

For development, install the pinned Python dependencies and run the existing test suite with:

```powershell
pip install -r requirements.txt
python -m unittest discover -s tests
```

Test sources are included. The latest provider-backed conversational retest and its limits are documented in [docs/qa/retest17-2026-11-04.md](docs/qa/retest17-2026-11-04.md). After that retest, the code added a deterministic catalog route for product installment questions; its Shelby SU-21S 12x regression passes locally. This checkout has not run a new provider-backed conversational QA after that code change. The full local unit suite now passes: 568 tests, 7 skipped, 0 failures. The GitHub Actions workflow for the previous PR head failed; a new workflow will run for this update.

## Technical decisions

- The challenge inputs are structured CSV tables. The application validates them and stores a versioned SQLite catalog snapshot. A conversation uses the catalog version pinned when that conversation starts; later imports apply to new conversations and do not change an existing conversation.
- Deterministic Python services query catalog, order and demonstration-policy data. The model interprets requests and selects from bounded tools; Python validates requests and calculates commercial values.
- Optional generated final wording passes limited structural, selected-term and evidence-reference checks before use. These checks do not prove semantic correctness or factual grounding. The application falls back to deterministic Python output when those checks reject the wording or the provider is unavailable.
- Decimal arithmetic is used for monetary calculations. Catalog and promotion answers reflect the snapshot selected for the conversation; they do not establish current live inventory or promotion eligibility.
- SQLite and named Docker volumes keep provider settings, chat history and bounded conversation context local to this prototype. Provider keys are not sent to the browser.
- Docker Compose separates the administration interface from the agent API. The administration port is bound to loopback, and the agent API is reachable only on the Compose network.
- The application uses ordinary Python modules, SQLite and Docker Compose. A vector database was not selected because the supplied product, order and promotion records are structured and require exact filtering. Demonstration-policy replies use configured rules; this repository does not contain or retrieve the private policy manual.

## Validation, known limitations and submission status

This is an evaluation prototype, not a production customer-support service.

- The published conversational retest covered 17 cases across 21 turns: 16 passed and one installment case failed. `INSTALL-001` returned a generic fallback for a request to pay R$ 159.90 in 12 installments; the expected maximum is three installments of R$ 53.30. See the [full retest report](docs/qa/retest17-2026-11-04.md). This retest predates the deterministic installment route described above.
- All retest requests returned HTTP 200, which confirms transport response only, not answer correctness. The report records 11 model calls, four privacy-gated order lookups followed by checks of the requested historical records, and the exact candidate data snapshot.
- The challenge asks for three to five conversation examples, including a non-trivial case. Five reviewed, sanitized examples are included in [docs/examples/agent-conversations.md](docs/examples/agent-conversations.md); no order conversation is included.
- The complete local unit suite passed after the installment-route and CI fixes: 568 tests, 7 skipped, 0 failures. This does not replace the separate provider-backed conversational retest. The known limitations file preserves the earlier 107-test selection and its baseline comparison.
- Catalog and promotion answers do not establish current inventory or live eligibility.
- The restricted policy manual was not consulted during the latest retest. Demonstration rules are not represented as verified current store policy.
- Demonstration contact records are inconsistent; confirm the official channel with the store operator. Order lookup uses a matching contact detail as a demo safeguard; it does not prove identity.
- The existing public ancestry contains store contact details. This candidate adds no real customer data or contact details, and does not rewrite already-published history.
- The administrator password must be set locally before startup. Loopback binding and demo-oriented authentication are not a production security boundary.
- No public deployment is configured. No open-source license is declared in this repository.

For the retest's interpretation, remaining risks and suggested improvements, see [docs/known-limitations.md](docs/known-limitations.md).

## Use of AI assistants

Codex Luna, used through Hermes and Codex, supported development across the project. Specialized Hermes profiles contributed domain-specific guidance and review while preparing the Spec-Driven Development (SDD) specifications; Codex Luna then assisted with implementation. The project owner made the product and architecture decisions and reviewed the resulting work. AI-generated code and summaries were not treated as verification by themselves.

## Example conversations

Five reviewed and sanitized conversation examples are available in [docs/examples/agent-conversations.md](docs/examples/agent-conversations.md). They are literal examples from the QA clone, not rewritten demonstrations. They contain no customer names, contact details, order IDs or tracking codes. The full QA context and the one failing case are documented in [docs/qa/retest17-2026-11-04.md](docs/qa/retest17-2026-11-04.md).
