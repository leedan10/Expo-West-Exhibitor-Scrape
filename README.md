# Expo West 2026 Exhibitor Scraper

Scrapes the [Natural Products Expo West 2026](https://expowest26.smallworldlabs.com/exhibitors) exhibitor directory and produces two Excel files:

| File | Contents |
|------|----------|
| `output/exhibitors.xlsx` | Exhibitor name, booth number, description, product categories, hall, country, company URL, social media links |
| `output/team_members.xlsx` | Exhibitor name, team member name, job title |

Output files are **never committed** to this repo. They are downloaded as GitHub Actions artifacts after each run.

---

## Running via GitHub Actions (recommended)

1. Go to the **Actions** tab in this repository.
2. Select **Expo West 2026 Exhibitor Scraper**.
3. Click **Run workflow**.
4. Optional inputs:
   - **limit** — scrape only the first N exhibitors (useful for testing; `0` = all)
   - **reset** — set to `true` to clear checkpoints and start fresh
5. After the run completes, download the **`expo-west-2026-data`** artifact from the workflow summary page.

> On failure, a **`scraper-debug-*`** artifact is also uploaded containing logs, screenshots, and checkpoint files to help diagnose the issue.

---

## Running locally

### Prerequisites

- Python 3.11+
- pip

### Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

### Run

```bash
# Full run
python scraper.py

# Test with first 10 exhibitors only
python scraper.py --limit 10

# Watch the browser in real time (debug mode)
python scraper.py --headful --limit 5

# Discard previous progress and start fresh
python scraper.py --reset

# Only discover exhibitor URLs, do not scrape detail pages
python scraper.py --list-only
```

Output files are written to `output/`. Logs are written to `logs/scraper.log`.

---

## Project structure

```
.
├── scraper.py          # Main entry point / Playwright orchestrator
├── config.py           # All configuration (URLs, selectors, rate limits)
├── extractors.py       # BeautifulSoup HTML parsers
├── checkpoint.py       # Crash-resume system (JSON-backed)
├── output.py           # Excel writer (openpyxl)
├── utils.py            # Logging, delays, retries, screenshots
├── requirements.txt    # Pinned Python dependencies
│
├── output/             # Excel files — gitignored, download as artifact
├── logs/               # scraper.log — gitignored
├── screenshots/        # Error screenshots — gitignored
├── checkpoints/        # Progress JSON — gitignored
│
└── .github/
    └── workflows/
        └── scrape.yml  # Manual GitHub Actions workflow
```

---

## Debugging

| Symptom | Fix |
|---------|-----|
| No exhibitors found on the listing page | Open `screenshots/error_listing_no_cards_*.png` to see what the browser rendered. The selectors in `config.py → SELECTORS["exhibitor_cards"]` may need updating. |
| Detail page data is empty | Check `logs/scraper.log` for ERROR lines. The page structure may have changed — inspect the exhibitor page in a browser and update selectors in `config.py → SELECTORS`. |
| Scraper stopped mid-run | Re-run `python scraper.py` — it automatically resumes from the last checkpoint. |
| Start completely fresh | Run `python scraper.py --reset` to clear all checkpoint files. |
| Rate-limit / 429 errors | Increase `MIN_DELAY_SECONDS` and `MAX_DELAY_SECONDS` in `config.py`. |
| Want to watch the browser | Run `python scraper.py --headful` locally. |

---

## Notes

- **Team member data**: The SmallWorld Labs platform may require login to view team members. The scraper attempts to extract them from public pages; if unavailable, `team_members.xlsx` will be empty or sparse.
- **Hall data**: Hall/location information is not exposed on public exhibitor pages and will be blank.
- **Rate limiting**: Default delays are 2–4 seconds per request to be respectful to the server.
