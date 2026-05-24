# Google Workspace Dispatch Automation

Email-to-Sheets dispatch logging for telecom on-call operations.

## Overview

On-call telecom dispatch teams often receive tickets through Gmail, coordinate follow-up in Google Chat, and manually maintain dispatch status in a spreadsheet. This project automates the low-volume dispatch logging path in Google Workspace.

It watches Gmail and Google Chat, writes append-only JSONL activity logs, extracts structured ticket fields from inbound email, writes a dispatch log to Google Sheets, and can use an LLM to classify ticket status from new email replies.

## Pipeline Architecture

```text
watcher.py
  -> watches Gmail + Chat
  -> writes raw JSONL logs
       |
       v
parse_tickets.py
  -> reads raw logs
  -> extracts WOT, site, priority, failure type, coordinates, etc.
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
cd automedon
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

5. Build the Docker image.

```bash
docker build -t dispatch-automation .
```

6. Run the container with the OAuth token, a log volume, and runtime configuration.

```bash
docker run -d \
  --name dispatch-automation \
  --restart unless-stopped \
  -v /path/to/gws-token.pickle:/app/gws-token.pickle:ro \
  -v /path/to/logs:/app/logs \
  -e GMAIL_QUERY="is:unread" \
  -e SHEET_ID="your-google-sheet-id" \
  -e OPENROUTER_API_KEY="your-openrouter-api-key" \
  -e CLASSIFY_ENABLED="true" \
  dispatch-automation
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

**parse_tickets.py**: Watches `gmail_bodies.jsonl` for new entries. Parses email subjects and bodies with regexes to extract structured dispatch fields such as WOT number, site ID, priority, failure type, coordinates, sector, technology, customer ticket, address, and LSO indicators. Deduplicates by `first_message_id` and writes parsed records to `parsed_tickets.jsonl`.

**sheets_writer.py**: Reads `parsed_tickets.jsonl`. Writes new WOTs as rows to the Google Sheets dispatch log. In `--watch` mode, it also checks tracked Gmail threads for new activity, sends the latest message context to an LLM for status classification, and updates the `Status` column.

## Running

Run the Gmail and Chat watcher. The Docker image starts this process by default.

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

For a full deployment, run all three long-lived processes in the same container or process supervisor against the same mounted `/app/logs` directory so each stage can consume the previous stage's JSONL output.

## Sheet Schema

```text
Column  Field              Description
------  -----------------  -----------------------------------------------
A       WOT                Work order or dispatch ticket number
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

The default model is `deepseek/deepseek-v4-flash`. The classifier is designed for low-volume telecom dispatch workflows where sequential calls and modest rate limiting are acceptable. Free-tier models can work if `CLASSIFY_INTERVAL` and `CLASSIFY_DELAY` are configured conservatively.

## Notes

All logs are append-only JSONL. The log directory is the handoff point between processes and should be mounted as persistent storage when running in Docker.

OAuth credentials are loaded from `TOKEN_FILE`. The token pickle should be generated outside the image and bind-mounted into the container.
