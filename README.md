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

Test sources are included. This publication-preparation pass did not run tests, QA, a container build or provider requests.

## Technical decisions

- The challenge inputs are structured CSV tables. The application validates them and stores a versioned SQLite catalog snapshot. A conversation uses the catalog version pinned when that conversation starts; later imports apply to new conversations and do not change an existing conversation.
- Deterministic Python services query catalog, order and demonstration-policy data. The model interprets requests and selects from bounded tools; Python validates requests and calculates commercial values.
- Optional generated final wording passes limited structural, selected-term and evidence-reference checks before use. These checks do not prove semantic correctness or factual grounding. The application falls back to deterministic Python output when those checks reject the wording or the provider is unavailable.
- Decimal arithmetic is used for monetary calculations. Catalog and promotion answers reflect the snapshot selected for the conversation; they do not establish current live inventory or promotion eligibility.
- SQLite and named Docker volumes keep provider settings, chat history and bounded conversation context local to this prototype. Provider keys are not sent to the browser.
- Docker Compose separates the administration interface from the agent API. The administration port is bound to loopback, and the agent API is reachable only on the Compose network.
- The application uses ordinary Python modules, SQLite and Docker Compose. A vector database was not selected because the supplied product, order and promotion records are structured and require exact filtering. Demonstration-policy replies use configured rules; this repository does not contain or retrieve the private policy manual.

## Known limitations and submission status

This is an evaluation prototype, not a production customer-support service.

- The challenge requires three to five conversation examples, including at least one non-trivial example. They are not included in this review copy, so the submission remains incomplete until reviewed, sanitized examples are added.
- This candidate has not been executed or accepted in a fresh test run. Live-provider answer quality and full end-to-end conversation behavior remain unverified here.
- Product discovery still routes accessories such as strings, cables, pedals and amplifiers poorly. Installment eligibility is not always calculated, and mixed-payment intent can be lost in follow-up turns.
- Catalog and promotion answers do not establish current inventory or live eligibility.
- Demonstration rules have not been checked against the private policy manual in this review. Do not present them as verified current store policy.
- Demonstration contact records are inconsistent; confirm the official channel with the store operator. Order lookup uses a matching contact detail as a demo safeguard; it does not prove identity.
- The existing public ancestry contains store contact details. This candidate adds no real customer data or contact details, and does not rewrite already-published history.
- The administrator password must be set locally before startup. Loopback binding and demo-oriented authentication are not a production security boundary.
- No public deployment is configured. No open-source license is declared in this repository.

## Use of AI assistants

OpenAI Codex assisted with implementation, source review and documentation. The project owner supplied a NotebookLM-generated summary of challenge submission requirements; the challenge brief was checked separately for deliverable details. Human decisions remain the responsibility of the project owner. Generated code and summaries are not treated as verification unless independently reviewed and executed.

## Example conversations

No conversation transcript is included in this review branch. Add three to five reviewed, sanitized examples before treating the challenge submission as complete. Do not use real customer names, contact details, order records or provider transcripts without review and authorization.
