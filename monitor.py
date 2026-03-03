#!/usr/bin/env python3
"""
monitor.py — Automated competitor monitoring tool powered by Claude AI.

Workflow:
  1. Fetch each competitor URL and extract visible page text.
  2. Compare against the last-seen snapshot to detect changes.
  3. Ask Claude to analyse what actually changed (skipping noise).
  4. Accumulate changes in a local state file.
  5. On digest day (or with --digest), generate a weekly summary and
     deliver it via Slack webhook or email.

Usage:
  python monitor.py --check               # daily: fetch + detect changes
  python monitor.py --digest              # force-send today's digest
  python monitor.py --check --digest      # check + auto-send on digest day
  python monitor.py --check --days 14     # digest covering last 14 days
"""

import os
import sys
import json
import hashlib
import smtplib
import argparse
import datetime
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─── Dependency check ────────────────────────────────────────────────────────
# Friendly error if the user hasn't installed requirements yet.

try:
    import requests
    from bs4 import BeautifulSoup
    import anthropic
except ImportError as exc:
    print(f"Missing dependency: {exc}")
    print("Run:  pip install -r requirements.txt")
    sys.exit(1)

# ─── Constants ────────────────────────────────────────────────────────────────

MODEL               = "claude-opus-4-6"
DEFAULT_CONFIG_PATH = "config.json"
DEFAULT_STATE_PATH  = "monitor_state.json"
MAX_PAGE_CHARS      = 50_000   # cap per-page content to control token usage
MAX_CHANGES_STORED  = 50       # per URL — prevents unbounded state file growth

# ─── API client ──────────────────────────────────────────────────────────────

def build_client() -> anthropic.Anthropic:
    """
    Create the Anthropic client.
    Exits early with a helpful message when ANTHROPIC_API_KEY is not set,
    rather than crashing with an obscure auth error later.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "Error: ANTHROPIC_API_KEY is not set.\n"
            "  export ANTHROPIC_API_KEY='sk-ant-...'\n"
            "  Get a key at https://console.anthropic.com"
        )
        sys.exit(1)
    return anthropic.Anthropic(api_key=api_key)

# ─── State management ────────────────────────────────────────────────────────

def load_state(path: str) -> dict:
    """
    Load the monitoring state from disk.

    The state is a dict keyed by URL.  Each entry holds:
      - last_hash:    MD5 of the last-seen page text
      - last_content: the last-seen page text (used for diffing)
      - last_checked: ISO timestamp of the last successful fetch
      - changes:      list of {detected_at, analysis} dicts
    """
    p = Path(path)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {}


def save_state(state: dict, path: str) -> None:
    """Persist monitoring state to disk as pretty-printed JSON."""
    with open(path, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ─── Web fetching ─────────────────────────────────────────────────────────────

def fetch_page_text(url: str, timeout: int = 15) -> str | None:
    """
    Download a URL and return its visible text content.

    Strips scripts, styles, nav and footer elements — we only want the
    substantive copy that represents real product/company changes.
    Returns None on any network or HTTP error so callers can skip gracefully.
    """
    try:
        headers = {
            "User-Agent": (
                "CompetitorMonitor/1.0 (+https://github.com/your-org/competitor-monitor)"
            )
        }
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove noise elements before extracting text.
        for tag in soup(["script", "style", "nav", "footer", "head", "noscript"]):
            tag.decompose()

        # Collapse whitespace so hashing and diffs are stable.
        text = " ".join(soup.get_text(separator=" ").split())

        # Hard cap so a single large page doesn't blow up the token budget.
        return text[:MAX_PAGE_CHARS]

    except requests.RequestException as exc:
        print(f"  [warn] Could not fetch {url}: {exc}")
        return None


def content_hash(text: str) -> str:
    """MD5 hex digest used purely for change detection (not cryptographic)."""
    return hashlib.md5(text.encode()).hexdigest()

# ─── Claude helpers ──────────────────────────────────────────────────────────

def analyze_change(
    url: str,
    old_text: str,
    new_text: str,
    client: anthropic.Anthropic,
) -> str:
    """
    Ask Claude to identify business-relevant changes between two page snapshots.

    Uses streaming with .get_final_message() to avoid HTTP timeouts on
    large page content, while keeping the calling code simple.
    """
    prompt = f"""You are a competitive intelligence analyst monitoring a competitor's website.

URL: {url}

PREVIOUS VERSION (truncated to 8 000 chars):
{old_text[:8_000]}

CURRENT VERSION (truncated to 8 000 chars):
{new_text[:8_000]}

Compare the two versions and identify meaningful business changes. Focus on:
- New or changed product features, pricing, or plans
- Messaging, positioning, or value-proposition shifts
- New hires, leadership changes, or notable job postings
- Press releases, blog posts, case studies, or announcements
- Changes to integrations, partnerships, or supported platforms

Respond with 2–5 concise bullet points covering only substantive changes.
If the differences are trivial (formatting, dates, rotating ads), respond with:
"No significant changes detected."
"""

    with client.messages.stream(
        model=MODEL,
        max_tokens=1_024,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()

    return message.content[0].text


def generate_digest(
    state: dict,
    since: datetime.datetime,
    client: anthropic.Anthropic,
) -> str:
    """
    Synthesise all accumulated changes into a formatted weekly digest.

    Uses adaptive thinking so Claude can reason about patterns across multiple
    competitors before writing the final summary.
    Streaming + get_final_message() handles the potentially long output safely.
    """
    # Collect only changes newer than the cutoff.
    recent: list[dict] = []
    for url, data in state.items():
        for change in data.get("changes", []):
            detected_at = datetime.datetime.fromisoformat(change["detected_at"])
            if detected_at >= since:
                recent.append({
                    "url": url,
                    "detected_at": change["detected_at"],
                    "analysis": change["analysis"],
                })

    if not recent:
        return "No competitor changes were detected in the monitored period."

    # Format the raw change reports for Claude.
    changes_block = "\n\n".join(
        f"### {c['url']}\nDetected: {c['detected_at']}\n{c['analysis']}"
        for c in recent
    )

    since_label = since.strftime("%B %d, %Y")
    prompt = f"""You are preparing a weekly competitor intelligence digest for a product and marketing team.

Period covered: since {since_label}

RAW CHANGE REPORTS:

{changes_block}

Write a polished digest in clean Markdown with these sections:
1. **Executive Summary** — 2–3 sentences highlighting the most important signals.
2. **Competitor Breakdown** — one subsection per competitor with key takeaways.
3. **Action Items** — bullet list of urgent things the team should respond to.

Be concise, professional, and action-oriented. Skip competitors with no meaningful changes.
"""

    with client.messages.stream(
        model=MODEL,
        max_tokens=4_096,
        thinking={"type": "adaptive"},  # let Claude reason before writing
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()

    # Return only the text block — adaptive thinking may also emit a thinking block.
    for block in message.content:
        if block.type == "text":
            return block.text

    return "Digest generation produced no text output."

# ─── Delivery: Slack & email ─────────────────────────────────────────────────

def send_slack(digest: str, webhook_url: str) -> None:
    """
    Post the digest to Slack via an Incoming Webhook URL.
    Set up a webhook at: https://api.slack.com/apps → Incoming Webhooks.
    """
    payload = {
        "text": f"*📊 Weekly Competitor Intelligence Digest*\n\n{digest}"
    }
    resp = requests.post(webhook_url, json=payload, timeout=10)
    resp.raise_for_status()
    print("  Digest posted to Slack.")


def send_email(digest: str, cfg: dict) -> None:
    """
    Send the digest as a plain-text email via SMTP with STARTTLS.

    Required config keys: email_sender, email_password,
                          email_recipient, email_smtp_host.
    Optional:             email_smtp_port (default 587).
    """
    subject = f"Competitor Digest — {datetime.date.today():%B %d, %Y}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["email_sender"]
    msg["To"]      = cfg["email_recipient"]
    msg.attach(MIMEText(digest, "plain"))

    host = cfg["email_smtp_host"]
    port = int(cfg.get("email_smtp_port", 587))

    with smtplib.SMTP(host, port) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(cfg["email_sender"], cfg["email_password"])
        smtp.sendmail(cfg["email_sender"], cfg["email_recipient"], msg.as_string())

    print(f"  Digest emailed to {cfg['email_recipient']}.")


def deliver_digest(digest: str, cfg: dict) -> None:
    """
    Route the digest to the configured output.

    output_type: "slack"  → posts via Slack Incoming Webhook
                 "email"  → sends via SMTP
                 anything else (or omitted) → prints to stdout (good for testing)
    """
    output_type = cfg.get("output_type", "print").lower()

    if output_type == "slack":
        webhook = cfg.get("slack_webhook_url") or os.environ.get("SLACK_WEBHOOK_URL")
        if not webhook:
            print(
                "Error: slack_webhook_url not set in config.json "
                "or SLACK_WEBHOOK_URL environment variable."
            )
            return
        send_slack(digest, webhook)

    elif output_type == "email":
        required = ["email_sender", "email_recipient", "email_password", "email_smtp_host"]
        missing = [k for k in required if not cfg.get(k)]
        if missing:
            print(f"Error: missing email config keys: {', '.join(missing)}")
            return
        send_email(digest, cfg)

    else:
        # Print mode — useful during development / CI.
        print("\n" + "=" * 60)
        print("WEEKLY COMPETITOR DIGEST")
        print("=" * 60)
        print(digest)
        print("=" * 60)

# ─── Core check loop ─────────────────────────────────────────────────────────

def run_check(config: dict, state: dict, client: anthropic.Anthropic) -> dict:
    """
    Fetch every configured URL, detect changes, and store Claude's analysis.

    Returns the updated state dict (caller is responsible for saving to disk).
    """
    urls: list[str] = config.get("competitors", [])
    if not urls:
        print("No competitor URLs found in config.json — add some under 'competitors'.")
        return state

    now = datetime.datetime.utcnow().isoformat()

    for url in urls:
        print(f"\nChecking: {url}")
        new_text = fetch_page_text(url)
        if new_text is None:
            continue

        new_hash = content_hash(new_text)
        entry = state.setdefault(
            url,
            {"last_hash": None, "last_content": "", "last_checked": None, "changes": []},
        )

        # No change since last visit.
        if entry["last_hash"] == new_hash:
            print("  No change detected.")
            entry["last_checked"] = now
            continue

        # Page changed — ask Claude what's meaningful.
        print("  Change detected — analysing with Claude...")
        try:
            analysis = analyze_change(url, entry.get("last_content", ""), new_text, client)
        except anthropic.AuthenticationError:
            # Re-raise auth errors — they affect all URLs and should abort the run.
            raise
        except anthropic.APIError as exc:
            print(f"  [error] Claude API error: {exc}")
            analysis = f"Analysis unavailable due to API error: {exc}"

        print(f"  {analysis[:200]}{'…' if len(analysis) > 200 else ''}")

        # Persist change and update snapshot.
        entry["changes"].append({"detected_at": now, "analysis": analysis})
        entry["changes"]       = entry["changes"][-MAX_CHANGES_STORED:]
        entry["last_hash"]     = new_hash
        entry["last_content"]  = new_text
        entry["last_checked"]  = now

    return state


def is_digest_day(config: dict) -> bool:
    """Return True if today matches the configured digest day (default: Monday)."""
    digest_day = config.get("digest_day", "monday").lower()
    return datetime.date.today().strftime("%A").lower() == digest_day

# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Competitor monitoring tool — daily checks + weekly digest via Claude AI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python monitor.py --check\n"
            "  python monitor.py --digest\n"
            "  python monitor.py --check --digest\n"
        ),
    )
    parser.add_argument("--check",  action="store_true", help="Fetch URLs and store any changes")
    parser.add_argument("--digest", action="store_true", help="Generate and deliver weekly digest now")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, metavar="FILE",
                        help=f"Config file path (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--state",  default=DEFAULT_STATE_PATH, metavar="FILE",
                        help=f"State file path (default: {DEFAULT_STATE_PATH})")
    parser.add_argument("--days",   type=int, default=7,
                        help="How many days back the digest should cover (default: 7)")
    args = parser.parse_args()

    if not args.check and not args.digest:
        parser.print_help()
        sys.exit(0)

    # ── Load config ──────────────────────────────────────────────────────────
    config_path = Path(args.config)
    if not config_path.exists():
        print(
            f"Config file not found: {args.config}\n"
            "  Copy config.example.json → config.json and fill in your settings."
        )
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    # ── Build Claude client (exits if key missing) ────────────────────────────
    client = build_client()

    # ── Load persisted state ─────────────────────────────────────────────────
    state = load_state(args.state)

    # ── Daily check ──────────────────────────────────────────────────────────
    if args.check:
        print(f"\n{'─'*60}")
        print(f"Competitor check  |  {datetime.datetime.utcnow():%Y-%m-%d %H:%M} UTC")
        print(f"{'─'*60}")
        try:
            state = run_check(config, state, client)
        except anthropic.AuthenticationError:
            print("\nError: Invalid API key — check your ANTHROPIC_API_KEY.")
            sys.exit(1)
        save_state(state, args.state)
        print(f"\nCheck complete. State saved to {args.state}")

    # ── Weekly digest ─────────────────────────────────────────────────────────
    # Send automatically when --check runs on digest day, or immediately when
    # --digest is passed explicitly.
    should_digest = args.digest or (args.check and is_digest_day(config))
    if should_digest:
        print(f"\n{'─'*60}")
        print("Generating weekly digest...")
        print(f"{'─'*60}")
        since = datetime.datetime.utcnow() - datetime.timedelta(days=args.days)
        try:
            digest = generate_digest(state, since, client)
        except anthropic.AuthenticationError:
            print("\nError: Invalid API key — check your ANTHROPIC_API_KEY.")
            sys.exit(1)
        deliver_digest(digest, config)


if __name__ == "__main__":
    main()
