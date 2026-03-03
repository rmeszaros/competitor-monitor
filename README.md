# Competitor Monitor

Automated competitor monitoring powered by Claude AI. Fetches competitor pages daily, detects meaningful changes, and sends a weekly digest to Slack or email.

## How it works

```
Daily cron              Weekly cron
     │                       │
     ▼                       ▼
Fetch URLs          Generate digest
     │                       │
Detect change?      Claude summarises
     │               all changes
     Yes                     │
     │               Deliver via
Claude analyses      Slack / Email
 what changed
     │
Save to state file
```

## Requirements

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com)

## Setup

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd competitor-monitor
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Set your API key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Add this to `~/.zshrc` or `~/.bashrc` to make it permanent.

### 3. Create your config file

```bash
cp config.example.json config.json
```

Edit `config.json`:

```json
{
  "competitors": [
    "https://competitor.com",
    "https://competitor.com/pricing"
  ],
  "digest_day": "monday",
  "output_type": "slack",
  "slack_webhook_url": "https://hooks.slack.com/services/..."
}
```

#### Output options

| `output_type` | What it does | Extra keys required |
|---|---|---|
| `"print"` | Prints digest to stdout (default, good for testing) | — |
| `"slack"` | Posts to a Slack channel | `slack_webhook_url` |
| `"email"` | Sends via SMTP | `email_smtp_host`, `email_smtp_port`, `email_sender`, `email_password`, `email_recipient` |

**Slack webhook:** create one at [api.slack.com/apps](https://api.slack.com/apps) → your app → Incoming Webhooks.

**Gmail:** use an [App Password](https://myaccount.google.com/apppasswords) (not your regular password). Set `email_smtp_host` to `smtp.gmail.com` and `email_smtp_port` to `587`.

## Usage

```bash
# Run a check (fetch URLs, detect + store changes)
python monitor.py --check

# Force-send the digest right now
python monitor.py --digest

# Check and auto-send digest if today is the configured digest day
python monitor.py --check --digest

# Digest covering the last 14 days instead of 7
python monitor.py --check --digest --days 14
```

## Scheduling

### macOS / Linux — cron

```bash
crontab -e
```

Add these two lines (adjust paths):

```cron
# Run daily check at 8 AM
0 8 * * * cd /path/to/competitor-monitor && source .venv/bin/activate && python monitor.py --check --digest >> logs/monitor.log 2>&1
```

This single job runs every day at 8 AM. On the configured `digest_day` (default: Monday) it also sends the digest automatically.

### macOS — launchd

Create `~/Library/LaunchAgents/com.yourname.competitor-monitor.plist` and load it with `launchctl load`.

## State file

Changes are persisted in `monitor_state.json` (auto-created on first run). Each URL entry stores:

- `last_hash` — MD5 of the last-seen page content
- `last_content` — the actual text (used for diffing)
- `last_checked` — ISO timestamp
- `changes[]` — list of `{detected_at, analysis}` objects (last 50 kept)

You can safely delete `monitor_state.json` to start fresh — the tool will treat every URL as new on the next run.

## Project layout

```
competitor-monitor/
├── monitor.py            # main script
├── config.json           # your config (gitignored)
├── config.example.json   # template
├── requirements.txt
├── monitor_state.json    # auto-generated state (gitignored)
└── README.md
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ANTHROPIC_API_KEY is not set` | Run `export ANTHROPIC_API_KEY='sk-ant-...'` |
| `Missing dependency` | Run `pip install -r requirements.txt` in your venv |
| `Config file not found` | Copy `config.example.json` → `config.json` |
| Page always shows "No change" | The site may serve dynamic content; try adding `/about` or `/pricing` instead of the root |
| Slack returns 400 | Double-check your webhook URL in `config.json` |
| Gmail auth failure | Use an App Password, not your login password |
