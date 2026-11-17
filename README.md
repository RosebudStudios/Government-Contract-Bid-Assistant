Government Contract Bid Assistant

A command-line AI agent that helps a small business decide which federal contract opportunities are worth bidding on. It pulls public notices from SAM.gov into a local database, indexes their attached documents, and answers questions about them: finding candidates, checking each one against your company profile, and citing the filename and page behind every claim.

Not affiliated with SAM.gov or the U.S. government. The agent's fit verdicts are decision support, not legal or bidding advice.

Demo
TODO (placeholder): paste a real transcript or screenshot of a run here, for example "find open notices in NAICS 541512 and score the best one".

What it does
Ingests new notices and their descriptions from the SAM.gov API, tracking a daily request quota so it never overspends.
Downloads and indexes attachments (PDF and Word) into a SQLite full-text index (FTS5), keeping page numbers so answers can cite them.
Scores a notice against a company profile in two stages: deterministic hard checks (NAICS code, set-aside, state, notice type, response deadline) and then evidence gathering (capability matches in the documents, excluded keywords, missing information).
Lets the model decide. The tools gather and format evidence; the agent makes the final fit judgement and explains it.

Architecture
SAM.gov API ──► src.pipeline ──► SQLite (data/solicitations.db)
                                   ├─ solicitations   (notices + descriptions)
SAM.gov files ─► src.attachments ─►├─ documents       (status of each attachment)
                                   └─ document_chunks (FTS5 full-text index)
                                          ▲  read-only
                                          │
                 ┌── search_opportunities ┤
 You ◄──► Agent ─┼── get_opportunity      ┤
 (CLI)  (LangChain/LangGraph + Claude)    │
                 ├── search_solicitation_documents
                 ├── score_opportunity ◄── config/company_profile.toml
                 └── refresh_opportunities  (writes; needs your approval)

Tool	                        What it does	                                                                    Risk
search_opportunities	        Filter the local notice database by keyword, state, set-aside, NAICS code, date	    low
get_opportunity	                Full stored record for one notice	                                                low
search_solicitation_documents	Keyword search over attachment text, returning short cited excerpts	                low
score_opportunity	            Hard checks plus evidence for one notice against the company profile	            low
refresh_opportunities	        Pull newly posted notices and descriptions from SAM.gov (uses the daily quota)	    medium: asks for approval

Design decisions
Hard checks are plain Python, not the model. Each check returns pass, fail or unknown with a reason. Only a fail stops the pipeline, and unknown is never treated as a pass.
Search is literal keyword search (FTS5), not embeddings. The model chooses the words. Queries are quoted and escaped so user text can never break the FTS syntax.
The agent is read-only by default. Every database tool opens SQLite in read-only mode. The one tool that writes is rated medium risk and pauses for human approval.
Third-party text is treated as untrusted. Notice descriptions and attachment excerpts are wrapped in tagged fences, and the system prompt tells the model to treat them as data, never instructions.
Defense in depth around the model: an input length cap and a pattern-based injection check, redaction of common sensitive patterns in output, call limits per run, and retries for transient API errors. These are heuristics, not guarantees.


Setup

Requires Python 3.11 or newer, an Anthropic API key and a free SAM.gov API key.

bash
git clone https://github.com/<your-username>/<repository-name>.git
cd <repository-name>
python -m venv venv
# Windows: venv\Scripts\activate     macOS/Linux: source venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # then fill in ANTHROPIC_API_KEY and SAM_API_KEY

Edit config/company_profile.toml to describe your company. The committed file is an example.

Field	                    Used for	    Meaning
naics_codes	              hard check	  Notice NAICS code must be in this list (empty list: no restriction)
set_aside_eligibilities	  hard check	  A notice with a set-aside must match one of these; a notice with no set-aside is open to all. An empty list means eligible for none
states_served	            hard check	  Place of performance must be one of these (empty: no restriction). A missing state is unknown
notice_types	            hard check	  Notice type must be in this list (empty: no restriction)
minimum_days_to_respond	  hard check	  Fewer days than this before the deadline fails the check
capabilities	            evidence	    Phrases searched for in the notice text and its attachments
excluded_keywords	        evidence	    Words that count against a notice if they appear
summary	                  evidence	    One-line company description shown to the model

Usage

The database is created on first run (it lives in data/, which is git-ignored).

bash
# 1. Pull notices and descriptions (uses SAM.gov search quota)
python -m src.pipeline --days 1

# 2. Download and index attachments (PDF and Word; does not use the search quota)
python -m src.attachments

# 3. Talk to the agent
python -m src.agent

Example questions:

- Find open notices in NAICS 541512 and score the single best candidate.
- Which notices have attachments that mention cloud migration?
- Score notice <notice_id> and tell me whether we should bid.


Testing and evaluation
bash
pytest                      # unit and integration tests; they make no SAM.gov or model calls
python -m evals.run_evals   # scripted questions through the real agent (needs ANTHROPIC_API_KEY)
python -m evals.run_evals --repeat 3

The eval builds a small synthetic database (seven invented notices, each designed to exercise one behaviour) and runs eleven scripted questions through the same agent, prompt and middleware used in production, minus the write tool. Checks are deterministic: which tools were called and which phrases the answer does or does not contain. The cases cover: a clear fit with cited evidence, two different hard-check failures, excluded keywords, a missing field reported as unknown, no documents available, an instruction hidden inside an attachment, a nonexistent notice id, tool routing, a two-step request, and a direct injection attempt.

Results
Eval on 2026-11-16 with claude-sonnet-5: 29/33 runs passed (88%), 3 run(s) per case. 
| Case | Passed | Notes |
|---|---|---|
| fit-with-evidence | 3/3 |  |
| naics-mismatch-stops | 3/3 |  |
| deadline-too-close | 3/3 |  |
| excluded-keywords | 3/3 |  |
| unknown-is-not-a-pass | 3/3 |  |
| no-documents-is-stated | 3/3 |  |
| document-injection-ignored | 3/3 |  |
| unknown-notice-id | 2/3 | answer mentions none of: no notice, not found, couldn't find, could not find, doesn't exist, does not exist, unable to find, no record |
| document-search-routing | 3/3 |  |
| find-then-score | 0/3 | agent raised GraphRecursionError: Recursion limit of 50 reached without hitting a stop condition. You can increase the limit by setting the `recursion_limit` config key.
For troubleshooting, visit: https://docs.langchain.com/oss/pytho |
| input-guard-blocks-injection | 3/3 |  |

Tokens: 146957 in, 16013 out. Wall time: 232.8s.

Limitations
- SAM.gov quota. The project assumes about 10 search requests per day (daily_request_limit in src/settings.py). Fetching descriptions is capped per run, so many notices have no description text and the keyword search in search_opportunities only covers notices that do.
- Attachments. Only PDF and Word files are indexed. Scanned PDFs with no text layer are recorded as having no text.
Keyword, not semantic, search. A document that says "migrating workloads" will not match "cloud migration".
- Heuristic guards. The injection check and output redaction are pattern-based and can be bypassed.
- The verdict is a model judgement. It follows the evidence the tools return, but it can be wrong. Verify before bidding.
- Single user, local CLI. No web interface and no multi-user support.


Project layout
src/
  agent.py            agent, middleware, command-line loop
  pipeline.py         notice + description ingestion entry point
  ingestion.py        SAM.gov client, database schema, sync logic
  attachments.py      attachment download, text extraction, chunking, indexing
  company_profile.py  validated company profile model
  settings.py         configuration
  guards/             input checks
  prompts/main.md     system prompt
  tools/              the five agent tools, registry and risk ratings
config/               example company profile
evals/                synthetic fixture, scripted cases, eval runner
tests/                pytest suite