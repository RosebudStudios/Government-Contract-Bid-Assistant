# Government Contract Bid Assistant

A command-line AI agent that helps a small business decide which federal contract opportunities are worth bidding on. It pulls public notices from SAM.gov into a local database, indexes their attached documents, and answers questions about them: finding candidates, checking each one against your company profile, and citing the filename and page behind every claim.

Not affiliated with SAM.gov or the U.S. government. The agent's fit verdicts are decision support, not legal or bidding advice.

## Demo

Run against a real SAM.gov snapshot with the example profile in `config/company_profile.toml` (an eight-person IT firm that holds no security clearance). The notice clears every automated hard check, but the agent searches its attachments and finds a disqualifier the hard checks cannot see.

```text
You: find open notices in NAICS 541512 and score the best one
Here is the result for NAICS 541512, open notices only.

Title: AMC/A6 Information Technology Needs Commercial Solutions Opening (CSO) - Amendment 04
notice_id: 8249a1893a3442b688e32f912753a261

Verdict: Weak fit

Hard checks (from score_opportunity): all pass
- naics_code: pass, company serves 541512
- set_aside: pass, none required, open to all
- states_served: pass, no state restriction on either side
- type: pass, company eligible for Combined Synopsis/Solicitation
- deadline: pass, 1562 days remaining (2031-02-26)

Capability evidence
- Title/description keyword hits: none
- Attachment hit: "AMC A6 AOI_Final_Amendment 01.pdf", page 5, a cybersecurity/RMF compliance section, which aligns loosely with the company's cloud/security past work for VA and DHS.

Disqualifier found: a Secret clearance is required. Multiple documents confirm this:
- "AMC A6 CSO_Final.pdf" p.2 and "AMC A6 CSO_Final_Amendment 01.pdf" p.2: "All respondees require a Secret Clearance. If a Facilities Clearance is required, the level will be specified in the AOI."
- "AMC A6 AOI_Final.pdf" p.5, "AMC A6 AOI_Final_Amendment 01.pdf" p.5, and "AMC A6 AOI_Final_Amendment 04.pdf" p.6: personnel must hold a Tier 3 Security Investigation with SECRET eligibility and be enrolled in Continuous Vetting within 30 days of award, and a SECRET facility clearance is required.
- Work is performed primarily at government-furnished facilities on AMC installations.

The company profile states the company holds no security clearance. No bonding requirement or CMMC mention was found in the indexed documents (searches for "bonding" and "CMMC" returned no matches).

Because all hard checks pass but the solicitation requires a Secret personnel clearance and a Secret facility clearance that the company does not currently hold, this caps the verdict at Weak fit rather than Possible fit, per the evaluation rule for unmet disqualifiers.

What would change the verdict: securing a teaming partner or subcontractor that already holds an active Secret facility clearance and can sponsor cleared personnel, or the company itself obtaining the facility clearance and getting key staff through Tier 3/SECRET investigations before any proposal submission.

Next step: if the company can identify a cleared teaming partner, re-run score_opportunity after noting that partnership; otherwise this notice is not pursuable as-is.
```

## What it does

- Ingests new notices and their descriptions from the SAM.gov API, tracking a daily request quota so it never overspends.
- Downloads and indexes attachments (PDF and Word) into a SQLite full-text index (FTS5), keeping page numbers so answers can cite them.
- Shortlists open notices by running the hard checks over all of them in plain code, then ranks the survivors by unknown fields, preferred notice type and capability matches.
- Scores a notice against a company profile in two stages: deterministic hard checks (NAICS code, set-aside, state, notice type, response deadline) and then evidence gathering (capability matches in the documents, excluded keywords, missing information).
- Lets the model decide. The tools gather and format evidence; the agent makes the final fit judgement and explains it.

## Architecture

```text
Data in    SAM.gov API ───► src.pipeline ──────┐
                                               ├──► SQLite (data/solicitations.db)
           SAM.gov files ─► src.attachments ───┘      solicitations      notices + descriptions
                                                      documents          status of each attachment
                                                      document_chunks    FTS5 full-text index

Agent      You ◄──► Agent (LangChain / LangGraph + Claude)
                      ├─ shortlist_opportunities         read-only; shortlist and score
                      ├─ search_opportunities            also read
                      ├─ get_opportunity                 config/company_profile.toml
                      ├─ search_solicitation_documents
                      ├─ score_opportunity
                      └─ refresh_opportunities           writes via the pipeline; needs your approval
```

| Tool | Risk | What it does |
|---|---|---|
| `shortlist_opportunities` | Low | Runs the hard checks over every open notice in plain code and returns only the ones that clear them, with unknown fields flagged |
| `search_opportunities` | Low | Filters the local notice database by keyword, state, set-aside, NAICS code and date |
| `get_opportunity` | Low | Full stored record for one notice |
| `search_solicitation_documents` | Low | Keyword search over attachment text, returning short cited excerpts |
| `score_opportunity` | Low | Hard checks plus evidence for one notice against the company profile |
| `refresh_opportunities` | Medium (asks for approval) | Pulls newly posted notices and descriptions from SAM.gov (uses the daily quota) |

## Design decisions

- **Hard checks are plain Python, not the model.** Each check returns pass, fail or unknown with a reason. Only a fail stops the pipeline, and unknown is never treated as a pass.
- **Search is literal keyword search (FTS5), not embeddings.** The model chooses the words. Queries are quoted and escaped so user text can never break the FTS syntax.
- **The agent is read-only by default.** Every database tool opens SQLite in read-only mode. The one tool that writes is rated medium risk and pauses for human approval.
- **Third-party text is treated as untrusted.** Notice descriptions and attachment excerpts are wrapped in tagged fences, and the system prompt tells the model to treat them as data, never instructions.
- **Defense in depth around the model:** an input length cap and a pattern-based injection check, redaction of common sensitive patterns in output, call limits per run, and retries for transient API errors. These are heuristics, not guarantees.

## Setup

Requires Python 3.11 or newer, an Anthropic API key and a free SAM.gov API key.

```bash
git clone https://github.com/RosebudStudios/Government-Contract-Bid-Assistant.git
cd Government-Contract-Bid-Assistant
python -m venv venv
# Windows: venv\Scripts\activate     macOS/Linux: source venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # Windows PowerShell: Copy-Item .env.example .env
# then fill in ANTHROPIC_API_KEY and SAM_API_KEY
```

Edit `config/company_profile.toml` to describe your company. The committed file is an example, loosened so the demo has candidates (it allows any state and two set-asides).

| Field | Used for | Meaning |
|---|---|---|
| `naics_codes` | hard check | Notice NAICS code must be in this list (empty list: no restriction) |
| `set_aside_eligibilities` | hard check | A notice with a set-aside must match one of these; a notice with no set-aside is open to all. An empty list means eligible for none |
| `states_served` | hard check | Place of performance must be one of these (empty: no restriction). A missing state is unknown |
| `notice_types` | hard check, ranking | Notice type must be in this list (empty: no restriction). The order is a preference: earlier types rank higher in the shortlist |
| `minimum_days_to_respond` | hard check | Fewer days than this before the deadline fails the check |
| `capabilities` | evidence, ranking | Phrases searched for in the notice text and its attachments |
| `excluded_keywords` | evidence | Words that count against a notice if they appear |
| `summary` | evidence | One-line company description shown to the model. Anything the agent should treat as a limit (for example, "holds no security clearance") belongs here |

## Usage

The database is created on first run (it lives in `data/`, which is git-ignored).

```bash
# 1. Pull notices and descriptions (uses SAM.gov search quota)
python -m src.pipeline --days 1

# 2. Download and index attachments (PDF and Word; does not use the search quota)
python -m src.attachments

# 3. Talk to the agent
python -m src.agent
```

Example questions:

- Find open notices in NAICS 541512 and score the single best candidate.
- Which notices have attachments that mention cloud migration?
- Score notice `<notice_id>` and tell me whether we should bid.

## Testing and evaluation

```bash
pytest                      # unit and integration tests; they make no SAM.gov or model calls
python -m evals.run_evals   # scripted questions through the real agent (needs ANTHROPIC_API_KEY)
python -m evals.run_evals --repeat 3
```

The eval builds a small synthetic database (eight invented notices, each designed to exercise one behaviour) and runs thirteen scripted questions through the same agent, prompt and middleware used in production, minus the write tool. Checks are deterministic: which tools were called and which phrases the answer does or does not contain. The cases cover: a clear fit with cited evidence, two different hard-check failures, excluded keywords, a missing field reported as unknown, no documents available, an instruction hidden inside an attachment, a nonexistent notice id, tool routing, a two-step request, a shortlist that must drop failing notices, a direct injection attempt, and a Top Secret clearance found only in an attachment.

### Results

Eval on 2026-11-17 with claude-sonnet-5: 39/39 runs passed (100%) (3 per case)

| Case | Passed | Notes |
|---|---|---|
| fit-with-evidence | 3/3 |  |
| naics-mismatch-stops | 3/3 |  |
| deadline-too-close | 3/3 |  |
| excluded-keywords | 3/3 |  |
| unknown-is-not-a-pass | 3/3 |  |
| no-documents-is-stated | 3/3 |  |
| document-injection-ignored | 3/3 |  |
| unknown-notice-id | 3/3 |  |
| document-search-routing | 3/3 |  |
| find-then-score | 3/3 |  |
| disqualifier-found-in-attachment | 3/3 |  |
| shortlist-drops-failures | 3/3 |  |
| input-guard-blocks-injection | 3/3 |  |

Tokens: 352898 in, 29238 out. Wall time: 349.9s.

## Limitations

- **SAM.gov quota.** The project assumes about 10 search requests per day (`daily_request_limit` in `src/settings.py`). Fetching descriptions is capped per run, so many notices have no description text and the keyword search in `search_opportunities` only covers notices that do.
- **Attachments.** Only PDF and Word files are indexed. Spreadsheets (for example Q&A matrices) are recorded as unsupported, and scanned PDFs with no text layer are recorded as having no text.
- **Keyword, not semantic, search.** A document that says "migrating workloads" will not match "cloud migration".
- **Heuristic guards.** The injection check and output redaction are pattern-based and can be bypassed.
- **The verdict is a model judgement.** It follows the evidence the tools return, but it can be wrong. Verify before bidding.
- **Single user, local CLI.** No web interface and no multi-user support.

## Project layout

```text
src/
  agent.py            agent, middleware, command-line loop
  pipeline.py         notice + description ingestion entry point
  ingestion.py        SAM.gov client, database schema, sync logic
  attachments.py      attachment download, text extraction, chunking, indexing
  company_profile.py  validated company profile model
  settings.py         configuration
  guards/             input checks
  prompts/main.md     system prompt
  tools/              the six agent tools, registry and risk ratings
config/               example company profile
evals/                synthetic fixture, scripted cases, eval runner
tests/                pytest suite
```

## License

MIT. See `LICENSE`.
