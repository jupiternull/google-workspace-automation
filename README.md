# Automedon

**Observe. Dispatch. Respond. Escalate.**

A read-to-act Google Workspace automation agent that shadows a team inbox and Chat spaces, then routes, replies, and escalates on your behalf.

Named for [Automedon](https://en.wikipedia.org/wiki/Automedon), Achilles' charioteer — the one who handled the driving so the hero could fight. This agent handles the operational layer so the on-call team can focus on what actually needs a person.

## Philosophy

Three phases, sequential and deliberate:

- **Phase 1 — Shadowing** (current). Read-only. Watches Gmail and Chat, logs everything to structured JSONL. No actions taken, no risk. Builds the pattern library.
- **Phase 2 — Dispatch.** Recognizes known patterns (P2 site-down tickets, escalation threads, specific sender workflows) and responds — auto-reply with ETA, file a spreadsheet row, @mention the right person in Chat.
- **Phase 3 — Multi-channel routing.** Escalate to PagerDuty, SMS, Slack, or custom webhooks when no one acknowledges within a threshold. Auditable. Overridable. Predictable.

Phase 1 must be boring before Phase 2 activates. No surprises.

## How it works

A long-lived Docker container. Each poll cycle:

1. **Polls Gmail** — fetches the last 10 messages matching your search query, logs structured metadata to `gmail.jsonl`
2. **Polls Google Chat** — fetches recent messages across all accessible spaces, logs sender, text, attachments to `chat.jsonl`
3. **Sleeps** — configurable interval, default 60s

All output is append-only JSONL written to a bind-mounted host directory.

## Prerequisites

- Docker
- A Google Cloud Platform project with the Gmail API and Google Chat API enabled
- OAuth 2.0 Desktop credentials (client ID + secret) for a service account or test user
- An authenticated token pickle

## Authentication

Generate a token pickle once outside the container:

```bash
pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
python3 -c "
import pickle
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/chat.messages.readonly',
    'https://www.googleapis.com/auth/chat.spaces.readonly',
    'https://www.googleapis.com/auth/chat.memberships.readonly',
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/spreadsheets',
]

flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)
creds = flow.run_local_server(port=0)
with open('gws-token.pickle', 'wb') as f:
    pickle.dump(creds, f)
print('Token saved to gws-token.pickle')
"
```

The token is bind-mounted at runtime — it never lives in the image.

## Quick start

```bash
# Build
docker build -t automedon .

# Run
docker run -d \
  --name automedon \
  --restart unless-stopped \
  -v /path/to/gws-token.pickle:/app/gws-token.pickle:ro \
  -v /path/to/logs:/app/logs \
  -e GMAIL_QUERY="to:dispatch@example.com" \
  automedon
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `GMAIL_QUERY` | `is:unread` | Gmail search filter (e.g. `to:team@company.com`) |
| `TOKEN_FILE` | `/app/gws-token.pickle` | Path to OAuth token inside container |
| `LOG_DIR` | `/app/logs` | Output directory for JSONL logs |
| `POLL_INTERVAL` | `60` | Seconds between poll cycles |

## Output

Three JSONL files:

**gmail.jsonl** — Inbound email metadata (sender, subject, snippet, thread ID)
**chat.jsonl** — Chat messages (space, sender hash, text, attachment flag)
**files.jsonl** — Chat attachment metadata

## License

MIT