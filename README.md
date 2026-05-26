# Google Workspace Dispatch Automation

Agent-assisted dispatch automation for Google Workspace.

## Overview

Started this project to make my day job a little easier. Our dispatch team receives outage and maintenance tickets through Gmail, and this automation pipeline coordinates technician comms through Google Chat, maintains dispatch status in Google Sheets, and responds to client emails/dispatches crews to cell site outages. The system preserves a deterministic core built around the watcher, parser, and Sheets writer pipeline, while layering an AI agent orchestration system on top. Parsed tickets are transformed into structured action plans, validated against deterministic policy and workflow rules, then used to generate draft communications, dispatch updates, and operational actions for review and approval. The end-goal here is to build this into a fully autonomous member of the team that learns and grows over time. Shoutout to [Nous Research](https://github.com/nousresearch).


The agent is deployed as a Docker container alongside the existing pipeline. It shares the same persistent log volume and Google Workspace OAuth credentials used by the watcher, parser, and Sheets writer.

The agent runs in draft-only mode initially. Operators review drafts before any auto-send behavior is enabled. Once approved, only these reply categories are eligible for auto-send:

```text
acknowledgement
missing_info
status_followup
```

Everything else requires human approval.

## Repository Structure

```text
google-workspace-automation/
├── watcher.py            # Gmail + Chat polling (unchanged)
├── parse_tickets.py      # Regex extraction (unchanged)
├── sheets_writer.py      # Google Sheets writer (unchanged)
├── requirements.txt      # Python deps (unchanged)
├── Dockerfile            # Base image (unchanged)
├── chronos/
│   ├── Dockerfile        # Agent image — Hermes + watcher + parser + agent
│   ├── entrypoint.sh     # Container startup script
│   ├── action_schema.py  # Structured action plan data models
│   ├── policy.py         # Deterministic validation rules
│   └── triggers.py       # Main agent loop — cursor, Hermes calls, policy, drafts
└── README.md
```

## Three-Tier Architecture

```text
Tier 3: Orchestrator
  └── Routes tasks, reviews unusual replies, sends notifications

Tier 2: Agent Layer (chronos/)
  └── triggers.py — watches parsed_tickets.jsonl, calls LLM, validates, drafts
  └── action_schema.py — structured action plan models
  └── policy.py — deterministic validation (allowlist, idempotency, auto-send rules)

Tier 1: Deterministic Pipeline
  └── watcher.py → parse_tickets.py → sheets_writer.py
```

The broader system can coordinate multiple generic agents:

```text
Workspace Agent — dispatch communications (this repo)
Orchestrator — coordinates agents
```

This repository implements the Workspace Agent. It consumes parsed dispatch events from the deterministic pipeline and produces reviewed, policy-checked communication drafts.

## Agent Layer

The agent container in `chronos/` includes Hermes, the watcher, the parser, and the agent loop. The container startup script catches up on parsed tickets, starts the Gmail watcher, and runs the trigger loop in the foreground.

Primary agent flow:

```text
parsed_tickets.jsonl
  -> triggers.py reads unseen entries with a cursor
  -> Hermes produces a structured action plan
  -> action_schema.py defines the expected plan shape
  -> policy.py validates tools, required fields, sender allowlists, idempotency, and auto-send rules
  -> valid plans become draft records for human review
```

The agent starts in draft-only mode via `CHRONOS_DRAFT_ONLY=true`. In this mode it does not send external replies; it records draft plans and audit entries for review. Auto-send should only be enabled after the draft output has been reviewed in production-like traffic.

## Deterministic Pipeline

```text
watcher.py
  -> watches Gmail + Chat
  -> writes raw JSONL logs
       |
       v
parse_tickets.py
  -> reads raw logs
  -> extracts work order, site, priority, failure type, coordinates, etc.
  -> writes parsed JSONL
       |
       v
sheets_writer.py
  -> reads parsed tickets
  -> writes Google Sheets dispatch log
       |
       + status classifier
           -> calls OpenRouter API on new activity in tracked threads
           -> updates Status column
```

## Installation / Setup

1. Clone the repository.

```bash
git clone <repo-url>
cd google-workspace-automation
```

2. Create or select a Google Cloud project and enable these APIs:

```text
Gmail API
Google Chat API
Google Sheets API
Google Drive API
```

3. Create OAuth 2.0 Desktop credentials in Google Cloud Console and download the client secret file as:

```text
client_secret.json
```

4. Generate the Google Workspace OAuth token pickle.

```bash
python3 -c "from google_auth_oauthlib.flow import InstalledAppFlow; import pickle; flow=InstalledAppFlow.from_client_secrets_file('client_secret.json', ['https://www.googleapis.com/auth/gmail.readonly','https://www.googleapis.com/auth/chat.messages.readonly','https://www.googleapis.com/auth/chat.spaces.readonly','https://www.googleapis.com/auth/chat.memberships.readonly','https://www.googleapis.com/auth/drive.readonly','https://www.googleapis.com/auth/spreadsheets']); pickle.dump(flow.run_local_server(port=0), open('gws-token.pickle','wb'))"
```

5. Build the deterministic pipeline Docker image.

```bash
docker build -t dispatch-automation .
```

6. Build the agent Docker image.

```bash
docker build -f chronos/Dockerfile -t workspace-agent .
```

7. Run the deterministic pipeline container with the OAuth token, a log volume, and runtime configuration.

```bash
docker run -d \
  --name dispatch-automation \
  --restart unless-stopped \
  -v /path/to/gws-token.pickle:/app/gws-token.pickle:ro \
  -v /path/to/logs:/app/logs \
  -e GMAIL_QUERY="to:dispatcher@example.com" \
  -e SHEET_ID="your-google-sheet-id" \
  -e OPENROUTER_API_KEY="your-openrouter-api-key" \
  -e CLASSIFY_ENABLED="true" \
  dispatch-automation
```

8. Run the agent container against the same credentials and log volume.

```bash
docker run -d \
  --name workspace-agent \
  --restart unless-stopped \
  -v /path/to/gws-token.pickle:/app/gws-token.pickle:ro \
  -v /path/to/logs:/app/logs \
  -v /path/to/agent-logs:/app/chronos \
  -e PARSED_TICKETS_PATH="/app/logs/parsed_tickets.jsonl" \
  -e CHRONOS_DRAFT_ONLY="true" \
  -e CHRONOS_POLL_INTERVAL="30" \
  workspace-agent
```

## Configuration Reference

```text
Variable             Default                         Description
-------------------  ------------------------------  -----------------------------------------------
GMAIL_QUERY          is:unread                       Gmail search filter
TOKEN_FILE           /app/gws-token.pickle           Path to OAuth token pickle
LOG_DIR              /app/logs                       Output directory for JSONL logs
POLL_INTERVAL        60                              Seconds between poll cycles
SHEET_ID             unset                           Target Google spreadsheet ID; creates new if unset
SHEET_NAME           Dispatch Log                    Sheet tab name
DRY_RUN              unset                           Set to true to log without writing to Sheets
PARSED_INPUT         /app/logs/parsed_tickets.jsonl  Path to parsed ticket JSONL
OPENROUTER_API_KEY   unset                           API key for LLM status classification
CLASSIFY_ENABLED     unset                           Set to true to enable status classification
CLASSIFY_MODEL       deepseek/deepseek-v4-flash      Model for classification
CLASSIFY_INTERVAL    300                             Min seconds between checks per thread
CLASSIFY_DELAY       3                               Delay between sequential LLM calls
CHRONOS_DRAFT_ONLY   true                            Keep agent replies as drafts only
CHRONOS_POLL_INTERVAL 30                             Seconds between agent poll cycles
CHRONOS_LOG_DIR      /app/chronos                    Agent cursor, draft, and audit log directory
PARSED_TICKETS_PATH  /app/logs/parsed_tickets.jsonl  Agent input path
SHEETS_WRITER_PATH   /app/sheets_writer.py           Sheets writer path used by the agent container
```

## Pipeline Components

**watcher.py**: Polls Gmail for new messages matching `GMAIL_QUERY`. Logs message metadata and the full body of the first message in each new thread into append-only JSONL. It also polls Google Chat spaces for recent messages and writes chat activity to JSONL.

Primary outputs:

```text
/app/logs/gmail.jsonl
/app/logs/chat.jsonl
/app/logs/files.jsonl
/app/logs/shadow.log
/app/logs/shadow.err
```

**parse_tickets.py**: Watches `gmail_bodies.jsonl` for new entries. Parses email subjects and bodies with regexes to extract structured dispatch fields such as work order, site ID, priority, failure type, coordinates, sector, technology, customer ticket, address, and LSO indicators. Deduplicates by `first_message_id` and writes parsed records to `parsed_tickets.jsonl`.

**sheets_writer.py**: Reads `parsed_tickets.jsonl`. Writes new work orders as rows to the Google Sheets dispatch log. In `--watch` mode, it also checks tracked Gmail threads for new activity, sends the latest message context to an LLM for status classification, and updates the `Status` column.

**chronos/triggers.py**: Watches `parsed_tickets.jsonl`, tracks processed entries with a cursor, calls Hermes for structured action plans, validates each plan through policy, and writes drafts plus audit records.

**chronos/action_schema.py**: Defines structured data models for action plans, ticket references, reply plans, and validation results.

**chronos/policy.py**: Applies deterministic checks for allowed tools, sender allowlists, idempotency, required fields, confidence thresholds, and approved auto-send reply categories.

## Running

Run the Gmail and Chat watcher. The base Docker image starts this process by default.

```bash
python3 watcher.py
```

Run the ticket parser alongside the watcher.

```bash
python3 parse_tickets.py --watch
```

Run the Sheets writer once.

```bash
python3 sheets_writer.py
```

Run the Sheets writer continuously and enable status classification.

```bash
CLASSIFY_ENABLED=true python3 sheets_writer.py --watch
```

Run the agent loop locally after Hermes is installed.

```bash
CHRONOS_DRAFT_ONLY=true python3 chronos/triggers.py
```

For a full deployment, run the deterministic pipeline and the agent container against the same mounted `/app/logs` directory so each stage can consume the previous stage's JSONL output.

## Sheet Schema

```text
Column  Field              Description
------  -----------------  -----------------------------------------------
A       Work Order         Work order or dispatch ticket number
B       Priority           Ticket priority
C       Status             Current ticket status
D       Site ID            Telecom site identifier
E       Sector             Site sector
F       Technology         Radio/network technology
G       Failure Type       Parsed failure category
H       Customer Ticket    Customer or upstream ticket reference
I       Address            Parsed site address
J       Coordinates        Latitude and longitude
K       LSO?               Yes when LSO keywords are detected
L       First Seen (UTC)   First parsed timestamp
M       Last Update (UTC)  Most recent parsed update timestamp
N       Msg Count          Message count in the Gmail thread
O       Snippet            Ticket body snippet
P       Thread ID          Gmail thread ID used for tracking replies
```

## Status Classifier

When `sheets_writer.py` runs in `--watch` mode with `CLASSIFY_ENABLED=true`, it reads existing sheet rows, checks `gmail.jsonl` for new activity on tracked Gmail threads, and sends the latest email context to an LLM through the OpenRouter API.

The classifier updates column C with one of these statuses:

```text
New
In Progress
Resolved
Cancelled
Postponed
Escalated
```

The default model is `deepseek/deepseek-v4-flash`. The classifier is designed for low-volume dispatch workflows where sequential calls and modest rate limiting are acceptable. Free-tier models can work if `CLASSIFY_INTERVAL` and `CLASSIFY_DELAY` are configured conservatively.

## Security Notes

Do not commit OAuth credentials, token pickles, JSONL logs, `.env` files, Google Sheet IDs, API keys, deployment hostnames, customer domains, webhook URLs, or production ticket identifiers.

Use placeholders in documentation and configuration examples:

```text
dispatcher@example.com
your-google-sheet-id
your-openrouter-api-key
/path/to/logs
```

The default deployment mode is draft-only. Human review is required for all replies until production drafts have been reviewed and auto-send is explicitly approved for `acknowledgement`, `missing_info`, and `status_followup`.

## Notes

All logs are append-only JSONL. The log directory is the handoff point between processes and should be mounted as persistent storage when running in Docker.

OAuth credentials are loaded from `TOKEN_FILE`. The token pickle should be generated outside the image and bind-mounted into the container.
