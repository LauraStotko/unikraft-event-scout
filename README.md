# Unikraft Event Scout Agent

A weekly automated agent that scrapes tech events from Luma, Techmeme, CNCF, and the Linux Foundation, classifies them using Claude, and writes new relevant events directly into a Google Sheet — in the column format your team already uses.

Runs automatically every Monday at 08:00 UTC via GitHub Actions.

---

## How it works

```
Every Monday 08:00 UTC (GitHub Actions cron)
        │
        ▼
┌─────────────────────────────────────┐
│  Scrapers                           │
│  • Luma: Berlin, Munich, London,    │
│    + AI/DevOps/Cloud discover feeds │
│  • Techmeme Events calendar         │
│  • CNCF / Linux Foundation events   │
└──────────────┬──────────────────────┘
               │  raw events (name, url, card text)
               ▼
┌─────────────────────────────────────┐
│  Claude Classifier                  │
│  • Is this relevant to Unikraft?    │
│  • Extract: category, location,     │
│    start/end dates, CFP status      │
└──────────────┬──────────────────────┘
               │  structured, relevant events only
               ▼
┌─────────────────────────────────────┐
│  Google Sheets Writer               │
│  • Deduplicates by name + URL       │
│  • Appends new rows                 │
│  • Computes "Days" automatically    │
└──────────────┬──────────────────────┘
               │  (optional)
               ▼
┌─────────────────────────────────────┐
│  Slack Summary                      │
│  • Posts a list of new events       │
│    with links to the sheet          │
└─────────────────────────────────────┘
```

**Sheet columns written:**
`Name | Category | CFP Date | CFP Status | Location | Start Date | End Date | Website | Days`

---

## Setup — step by step

### 1. Fork or clone this repo into your GitHub account

### 2. Create a Google Cloud service account

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or use an existing one)
3. Enable the **Google Sheets API**: APIs & Services → Enable APIs → search "Sheets"
4. Create a service account: IAM & Admin → Service Accounts → Create
5. Give it no special roles (it only needs Sheets access)
6. Create a JSON key: click the service account → Keys → Add Key → JSON
7. Download the `.json` file — keep it secret

### 3. Share your Google Sheet with the service account

- Open your Google Sheet
- Click **Share**
- Paste the service account email (looks like `name@project.iam.gserviceaccount.com`)
- Give it **Editor** access

### 4. Add GitHub Actions secrets

Go to your repo → Settings → Secrets and variables → Actions → New repository secret

| Secret name | Value |
|-------------|-------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key from [console.anthropic.com](https://console.anthropic.com) |
| `GOOGLE_SPREADSHEET_ID` | The ID from your sheet's URL: `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The **entire contents** of your service account `.json` key file, pasted as a single string |
| `SLACK_WEBHOOK_URL` | *(Optional)* Your Slack incoming webhook URL |

### 5. Test it manually

Go to Actions → **Weekly Event Scout** → Run workflow → select `dry_run: true`

This runs the full pipeline but skips writing to the sheet — lets you see what events would be found.

Then run again with `dry_run: false` to write for real.

---

## Local development

```bash
# Clone and install
git clone https://github.com/your-org/unikraft-event-agent
cd unikraft-event-agent
pip install -r requirements.txt

# Copy and fill in your credentials
cp .env.example .env
# Edit .env with your keys

# Dry run (no sheet writes)
DRY_RUN=true python main.py

# Full run
python main.py
```

---

## Project structure

```
unikraft-event-agent/
├── main.py                    # Orchestrator — runs the full pipeline
├── requirements.txt
├── .env.example               # Template for local credentials
├── .gitignore
│
├── scrapers/
│   ├── __init__.py
│   ├── luma.py                # Scrapes lu.ma/berlin, /munich, /london + discover feeds
│   ├── techmeme.py            # Scrapes techmeme.com/events
│   └── cncf.py                # Fetches LF Events + CNCF KCD listings
│
├── agent/
│   ├── __init__.py
│   └── classifier.py          # Claude-powered relevance classifier + field extractor
│
├── sheets/
│   ├── __init__.py
│   └── client.py              # Google Sheets API client (auth, read, append, dedup)
│
└── .github/
    └── workflows/
        └── weekly-event-scout.yml   # GitHub Actions cron (every Monday 08:00 UTC)
```

---

## Customising

**Add a new scraper source:** Create a new file in `scrapers/`, implement a `scrape() -> list[dict]` function, then import and call it in `main.py`.

**Change the schedule:** Edit the `cron` expression in `.github/workflows/weekly-event-scout.yml`. Use [crontab.guru](https://crontab.guru) to build your expression.

**Change relevance criteria:** Edit the `SYSTEM_PROMPT` in `agent/classifier.py`. The prompt explains Unikraft's focus areas to Claude.

**Add more cities:** Add entries to the `LUMA_SOURCES` list in `scrapers/luma.py`.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "No Google credentials found" | Check that `GOOGLE_SERVICE_ACCOUNT_JSON` secret is set and contains valid JSON |
| "The caller does not have permission" | Make sure the service account email has Editor access on the Google Sheet |
| "ANTHROPIC_API_KEY is not set" | Add the secret in GitHub Actions or your `.env` file |
| Events not being found | Sites may have changed their HTML structure — check scraper logs in the Actions run |
| Duplicate events appearing | The deduplication matches on exact name and URL — if a site changes an event's URL, it may appear twice |
