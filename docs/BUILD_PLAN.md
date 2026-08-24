# KeepItSimple — Due Diligence Agent Build Plan

## 1. Goal

Build a lightweight due diligence agent for pre-seed and seed VC screening.

A mid-sized VC may receive thousands of pitch decks per year. The product should help an investor quickly understand:

- What in the pitch deck appears credible
- What should be challenged
- The most important early risks / red flags
- The best non-generic questions to ask the founders

The MVP should focus on **decision support before an intro meeting**, not full investment committee due diligence.

---

## 2. Core User Flow

```text
VC uploads pitch deck
        ↓
PDF stored in Supabase
        ↓
Existing Railway PDF reader parses deck
        ↓
Parsed PyMuPDF data stored in Supabase
        ↓
Claude performs focused due diligence research
        ↓
Research focuses on:
  1. TAM / market
  2. Competitors
  3. Founder profile
        ↓
Agent derives key findings / risks
        ↓
UI shows:
  - Company overview
  - Key metrics / findings
  - Up to 5 key risks / red flags
  - Up to 5 founder questions
  - Evidence links for external claims
```

---

## 3. Current State

### Existing repository

Repository:

`git@github.com:acedinesen/Due-Dilligence-Agent-Hackathon.git`

The repo already contains the hackathon documentation / agent instructions and now includes the backend foundation.

### Existing PDF service

Railway:

`https://pdfreader-production-29d1.up.railway.app`

The PDF reader is treated as a separate service.

The due diligence backend should communicate with it through one small adapter so the PDF service can be changed without rewriting the rest of the app.

### Backend base now added

The backend foundation contains:

- FastAPI API
- PDF reader adapter
- Basic diligence data models
- Supabase schema
- Simple firm-profile hook
- Railway configuration
- Environment variable template
- Source validation

The implementation intentionally avoids unnecessary multi-agent architecture.

---

## 4. MVP Architecture

Keep the backend simple.

```text
app/
├── main.py
├── models.py
├── pdf_reader.py / adapters/
└── diligence.py

supabase/
└── schema.sql

firm_profiles/
└── generic_seed.json
```

Conceptually, the backend only needs four responsibilities:

### `main.py`

API endpoints.

Primary endpoint:

```text
POST /analyze
```

### PDF reader adapter

Responsible only for:

```text
PDF → existing Railway parser → normalized parsed deck
```

### `models.py`

Defines the shared output format:

- PitchDeck
- Source
- Metric / Finding
- DiligenceReport

### `diligence.py`

Responsible for:

```text
Parsed deck + optional firm profile
        ↓
Claude research
        ↓
Structured diligence result
```

---

## 5. Evidence Rule

This is a core product requirement.

### External evidence

Every factual finding derived from external research must contain a source URL.

Example:

```json
{
  "type": "external",
  "title": "Relevant market report",
  "evidence": "The market estimate conflicts with the deck's TAM claim.",
  "url": "https://..."
}
```

### Pitch-deck evidence

Deck findings should reference the relevant page / slide.

Example:

```json
{
  "type": "deck",
  "title": "Pitch deck",
  "evidence": "The company claims a €5bn TAM.",
  "page": 7
}
```

### Agent inference

The UI and backend should distinguish:

1. What the deck says
2. What external sources say
3. What the agent concludes

The agent must not present an inference as a verified fact.

---

## 6. Initial Due Diligence Areas

For the hackathon, research only the areas that are most useful at pre-seed / seed.

### A. TAM / Market

Questions to answer:

- Is the claimed TAM credible?
- Is the company using an overly broad market definition?
- Is the market growing?
- Is the realistically serviceable market much smaller?
- Are important market assumptions unsupported?

### B. Competitors

Questions to answer:

- Are the competitors shown in the deck correct?
- Are important competitors missing?
- Are incumbents being ignored?
- Does the claimed differentiation appear real?
- Could another company easily reproduce the product advantage?

### C. Founder Profile

Questions to answer:

- Are founder backgrounds and relevant experience verifiable?
- Is there clear founder-market fit?
- Are important claims about previous companies / roles accurate?
- Is there anything material an investor should ask about?

This is not intended to become personal-background investigation. Only investment-relevant, public professional information should be considered.

---

## 7. Desired Output

The API should eventually return approximately:

```json
{
  "company_name": "Example",
  "overview": "Short investment-relevant company summary",

  "metrics": [
    {
      "name": "tam",
      "status": "questionable",
      "summary": "The deck appears to use the broader category as TAM.",
      "sources": []
    },
    {
      "name": "competitors",
      "status": "supported",
      "summary": "Most major competitors are identified, but one important incumbent is missing.",
      "sources": []
    },
    {
      "name": "founder",
      "status": "supported",
      "summary": "The founders' relevant experience is externally verifiable.",
      "sources": []
    }
  ],

  "key_findings": [
    "Up to five material findings or risks"
  ],

  "founder_questions": [
    "Up to five specific questions derived from the findings"
  ]
}
```

---

## 8. Founder Question Requirement

The questions are a key part of the product.

Avoid generic questions such as:

- "Who are your competitors?"
- "How do you plan to scale?"
- "What differentiates you?"

Questions should instead be derived from specific evidence or unresolved assumptions.

Example:

> The deck assumes a three-month enterprise sales cycle, but comparable products appear to face significantly longer procurement processes. What has the sales cycle been for each of your currently paying enterprise customers from first contact to signed contract?

The goal is:

**Give the VC questions they would not have known to ask from reading the deck alone.**

---

## 9. Supabase

Keep Supabase intentionally simple for the hackathon.

### Storage

Bucket:

```text
pitch-decks
```

Used for uploaded PDFs.

### Table

`pitch_decks`

Minimum useful fields:

```text
id
filename
storage_path
full_text
pages
parser_metadata
created_at
```

No tenant isolation is required for the hackathon.

Do not spend time building production-grade authentication.

---

## 10. Firm-Specific Layer

This is optional for the MVP.

Different investors may care about different things:

- byFounders
- Antler
- YC
- other seed funds

A lightweight firm configuration can eventually influence what the agent prioritizes.

Example:

```json
{
  "firm": "Example Seed Fund",
  "stage": ["pre-seed", "seed"],
  "focus": ["B2B SaaS", "AI"],
  "criteria": [
    "founder-market fit",
    "large market",
    "credible wedge",
    "early customer pull"
  ]
}
```

Do **not** make the MVP dependent on this feature.

First make the generic seed diligence flow work.

---

## 11. Long-Term Idea — Not Hackathon Scope

A future version could learn from a VC firm's historical deal flow:

```text
All previous pitches
        ↓
Which were rejected?
Why?
        ↓
Which reached partner meetings?
Why?
        ↓
Which were invested in?
Why?
        ↓
Firm-specific decision history
        ↓
Better future diligence suggestions
```

This could become a strong data moat.

It is **not required for the hackathon build**.

---

# Build Order

## Step 1 — Verify PDF Service Integration

Make the backend successfully send a PDF to:

`https://pdfreader-production-29d1.up.railway.app`

Confirm:

- Correct endpoint
- Request format
- Returned JSON structure
- Page / slide information is preserved

Success condition:

```text
POST /analyze or /parse
→ PDF reader
→ parsed deck returned successfully
```

Do this before working on Claude.

---

## Step 2 — Supabase Connection

Implement:

```text
Upload PDF
→ save PDF in pitch-decks bucket
→ save normalized parser result in pitch_decks table
```

Success condition:

The app can retrieve both the original PDF and its parsed representation.

---

## Step 3 — Claude Diligence Call

Implement one primary diligence flow.

Input:

- Parsed deck
- Optional firm criteria

Claude should:

1. Understand the company
2. Identify important claims
3. Research TAM
4. Research competition
5. Research founders
6. Compare research against the pitch deck
7. Return structured results

Do not create several agents unless there is a concrete reason.

---

## Step 4 — Research Tools + Sources

Give Claude access to web research.

Require every external finding to return:

```text
title
URL
short supporting evidence
```

Reject / ignore external claims without a source URL.

---

## Step 5 — Derive Up to 5 Key Findings

The output should prioritize material issues.

A finding may be:

- A red flag
- A risk
- An unsupported assumption
- A contradiction
- An important unknown

Do not force five negative findings if the research does not justify them.

---

## Step 6 — Generate Up to 5 Founder Questions

Each question should trace back to a finding or meaningful unknown.

Success condition:

A VC could realistically copy the question into an email or ask it during the founder meeting.

---

## Step 7 — Frontend

Only after the core pipeline works.

Minimum useful UI:

```text
[ Upload pitch deck ]

Company overview

TAM
Competitors
Founder

Key Risks / Findings

Questions to Ask

Sources
```

Keep it clean and fast.

---

## Step 8 — Optional Firm Profile Demo

If time allows:

Add two different investor profiles and run the same pitch deck through both.

Show that the research emphasis / findings change depending on the investor.

This is a useful demo feature but should not block the core product.

---

# What Not to Build

For the hackathon, avoid:

- Complex multi-agent orchestration
- Vector database unless genuinely required
- Full investment memo generation
- Production authentication
- Tenant isolation
- CRM integrations
- Portfolio management
- Automated investment scoring systems
- Complex weighting formulas
- Large historical VC decision database
- Perfect PDF parsing
- Too many diligence categories

The demo is successful if it does this one flow extremely well:

> **Upload a pitch deck → identify what matters → research it → surface evidence-backed risks → give the VC five sharp questions.**

---

# Immediate Next Action

The next engineering task is:

> **Verify the exact Railway PDF reader API contract and get PDF → parsed deck working end-to-end from the newly merged backend.**

Once that works, implement the first real Claude diligence call against the parsed deck.
