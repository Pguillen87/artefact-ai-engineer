# Artefact AI Engineer challenge

## Data quality increment

The loader reads six canonical CSVs into a single immutable snapshot. It checks CSV headers and row shape, identifiers, duplicate keys, references, ISO dates, product/order states, finite nonnegative `Decimal` amounts, promotion bounds and flags, positive quantities and JSON objects (including duplicate keys). A structural error rejects the entire snapshot. No source file is modified. Run on the original challenge directory with exact approved SHA-256 verification:

```powershell
python -m artefact.report 'C:\App\Artefact\Desafio Técnico AI Engineer 2026\data' --baseline
```

The report contains table counts, hashes and field-level product findings with evidence and confidence; it does not include customer or order records. Treat reports as internal because they describe the private dataset. For synthetic input, omit `--baseline` and use the canonical filenames (`products.csv`, etc.). Run tests using `python -m unittest discover -s tests`. Tests use synthetic data; the baseline integration test is skipped when the local originals are unavailable.

The deterministic scanner currently detects selected brand discrepancies, keyboard/string count disagreements and some category/title disagreements. Its controlled vocabulary and patterns are deliberately conservative and **not exhaustive**; absence of a finding is not a guarantee of semantic correctness. Descriptive issues never invalidate independently typed price or stock. They do not correct the source, establish which field is true, or implement a seller handoff. No policy, bot, LLM, order authorization or administrative interface is implemented yet.

The source dataset and restricted documents are excluded from this public repository. Python standard library is sufficient for this increment.
