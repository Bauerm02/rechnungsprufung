# Review Notes

## Corrected In This Pass

- `PRIVAT_RECHNUNG` now routes to a dedicated non-payable hold family: `PRIVATE_REVIEW_HOLD`.
- `MAHNUNG` is now explicitly non-payable and has `stamp_allowed = false` in Phase 1.
- The RuleEngine now defaults conservatively:
  - `DUPLIKAT` remains a terminal override.
  - validation errors force a non-payable hold outcome unless a future explicit exception is implemented.
  - missing or unclear extracted status falls back to `UNGUELTIG` hold.
- VAT normalization now includes OCR correction logic and Austrian `ATU` handling rules from the legacy Zap behavior.
- Payability is now explicit in `RuleDecision` via `payable_outcome`.
- Empty placeholder directories left behind by the interrupted scaffold are removed where they were clearly unused.

## Intentionally Stubbed

- live Dropbox integration
- live OCR provider integration
- live LLM integration
- live notification sending
- live Google Sheets mirroring
- live VAT/VIES lookups
- file moves
- file deletes
- XML publishing
- full legacy rule implementation

## Must Be Implemented In Phase 2

- deterministic migration of the legacy validation/routing rules into Python services
- explicit handling for authority, reverse-charge, Redlinghofer, reminder, and private-invoice rules
- full duplicate-detection workflow with document registry writes during processing
- audit event coverage across the real processing pipeline
- artifact planning for stamped PDFs and XML payloads
- richer notification drafting and routing decisions
- integration tests around the pipeline orchestration
