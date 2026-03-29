# Invoice Automation

Phase 1 scaffold for a Python-based invoice automation system migrating away from Zapier.

## Scope

This repository currently provides:

- project structure and package boundaries
- typed domain enums and schemas
- decision tables for status, XML, and routing policy
- local SQLite-ready persistence models and repositories
- duplicate-detection interfaces plus a DB-backed repository
- audit-log persistence scaffolding
- stubbed ports and mock adapters for OCR, LLM, notifications, storage, and VAT validation
- validation and rule-engine scaffolding
- unit tests for enums, schemas, policy tables, and duplicate detection

## Non-Goals In This Phase

The following are intentionally disabled or stubbed:

- live Dropbox integration
- live OCR providers
- live LLM providers
- live email sending
- live Google Sheets mirroring
- live VIES checks
- file moves
- file deletes
- XML publishing

All side effects must remain disabled by default.

## Quick Start

1. Create a virtual environment with Python 3.11+.
2. Install the package with development dependencies.
3. Run the test suite.

```powershell
python -m pip install -e .[dev]
pytest
```

## Layout

```text
src/invoice_automation/
  domain/             Domain enums, schemas, and value objects
  application/        Policy tables and orchestration scaffolding
  duplicate_detection Duplicate repository contracts and service
  audit_log/          Audit logging service scaffolding
  validation/         Normalization and validation scaffolding
  rule_engine/        Phase 1 decision scaffolding
  ports/              External service interfaces
  adapters/mocks/     Mock adapters with safe defaults
  infrastructure/db/  SQLAlchemy tables, session helpers, repositories
```

## Current Status Semantics

- `GUELTIG`: potentially payable, subject to later validation expansion
- `UNGUELTIG`: terminal hold status
- `MAHNUNG`: terminal manual-review hold status with no XML and no Phase 1 stamping
- `PRIVAT_RECHNUNG`: terminal private-review hold status with no XML
- `DUPLIKAT`: terminal duplicate status

## Database

The database is the single system of record. Google Sheets is treated only as a legacy reference or optional mirror in later phases.
