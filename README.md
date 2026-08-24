# Keepitsimple — Due Diligence Agent

Hackathon MVP for pre-seed / seed VC screening.

A VC uploads a pitch deck. The system parses it, stores it, runs focused diligence, surfaces the most important red flags / risks, and proposes five useful questions for the founder meeting.

## MVP flow

```text
Pitch deck PDF
    ↓
Existing PDF reader on Railway
    ↓
Parsed deck (PyMuPDF output)
    ↓
Supabase
  - original PDF in `pitch-decks` bucket
  - parsed text/pages in `pitch_decks` table
    ↓
Claude diligence agent
    ↓
3 core checks
  - TAM: is the market claim credible?
  - Competitors: is the competitive picture credible?
  - Founder: anything material to flag?
    ↓
Top 5 findings / risks
    ↓
5 founder questions
```

## Evidence rule

Every externally researched factual statement must carry a source URL.

Deck evidence must carry a deck page number.

The API models enforce this so the UI can always show where a finding came from.

## Base API

### `GET /health`
Railway health check.

### `POST /parse`
Sends a PDF to the existing PDF-reader Railway service and returns normalized parsed text.

### `POST /analyze?firm=generic_seed`
1. parses the PDF,
2. stores it in Supabase if configured,
3. runs the diligence agent,
4. returns the report.

The current agent is a mock intentionally. It proves the plumbing without inventing web research.

## Existing PDF reader

Configured by default as:

```env
PDF_READER_URL=https://pdfreader-production-29d1.up.railway.app
PDF_READER_PATH=/parse
```

`PDF_READER_PATH` is still configurable because only the Railway base URL has been confirmed here. If the service returns a different JSON shape, change only `app/adapters/pdf_reader.py::_normalize()`.

## Supabase

Run:

`supabase/schema.sql`

It creates only what the MVP needs:

- private `pitch-decks` bucket
- `pitch_decks` table with filename, storage path, full parsed text, pages and parser metadata

No tenant model or real application auth is included.

## Firm-specific criteria

This is optional, not core architecture.

A firm can be represented by one JSON file in `firm_profiles/`. For now there is only `generic_seed.json`.

The same analysis should still work without a firm profile:

```text
POST /analyze
```

Later you can add `byfounders.json`, `antler.json`, etc. without changing the pipeline.

## What is intentionally NOT in the base

- multi-agent orchestration
- vector database / RAG
- historical VC decision-learning layer
- tenant isolation
- authentication system
- scoring formulas
- background job infrastructure

Those may become useful later, but they are not necessary to prove the hackathon product.

## Next code task

Replace `MockDiligenceAgent` with a Claude implementation that returns exactly the `DiligenceReport` model and uses research tools for external validation.

The Claude agent should focus first on TAM, competitors and founder profile, then derive the five highest-value findings and five non-generic founder questions from those findings.
