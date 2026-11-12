# Canadian Drug Shortage Monitor

Open tools that make Canada's official drug shortage data easier to use and easier to trust.

- **A public dashboard**, rebuilt every day: what's short right now, with duplicate reports merged and a report-freshness label on every report. **Live site:** https://utsavamehta-cloud.github.io/canada-drug-shortage-monitor/
- **A Claude MCP server**, so pharmacists, analysts and developers can ask about shortages in plain language from Claude.

## Why

Drug companies in Canada must report every shortage to Health Canada's reporting site, [Health Product Shortages Canada](https://healthproductshortages.ca). The data is valuable, but much of it goes stale. A full scan of all 1,742 open reports on 12 November 2026 found:

- 51% not updated in over a year
- 61% with no estimated end date
- 10% duplicates (158 products with 2 to 4 open reports each)
- 106 reports past their own estimated end date, 71 of them by more than a year

An old report is not necessarily wrong, since some shortages really do last a long time. But pharmacists can't tell from the report alone, so they double-check by phone. This project labels every report with plain date facts (when it was last updated, whether its end date has passed) so you can see at a glance which ones to confirm with your supplier.

> Information only, from manufacturer reports to Health Canada. Not medical advice. Independent project, not affiliated with Health Canada or the operator of the reporting site.

## The dashboard

Two views plus a method page:

| View | For | What it shows |
| --- | --- | --- |
| What's short now | Pharmacists, clinics, long-term care | Every product with an open report (merged by DIN), Tier 3 first, with search and filters by status, report freshness and therapeutic group, plus what changed in the last 7 days |
| Report freshness | Health Canada, provinces, associations, researchers | Time since each open report was last updated, by therapeutic group and by company, with a daily trend line |

Report freshness labels. Each one states a date fact from the report itself, never a judgement:

| Label | Rule (checked in this order) |
| --- | --- |
| Past estimated end date | Report still open, but the end date the company gave has passed |
| No update in 12+ months | Report still open and not changed in over a year |
| No end date given | No estimated end date |
| Updated in last 14 days | Changed in the last 14 days |
| End date still ahead | Everything else: the end date is in the future |

The first two are flagged `check_with_supplier`. They are prompts to confirm, not findings of fault.

Build it yourself:

```
pip install .
hpsc-dashboard --out site --history data/history.json
```

This writes `site/index.html` (one self-contained file) and `site/data.json`. It needs `HPSC_EMAIL` and `HPSC_PASSWORD` for a free account on healthproductshortages.ca.

### Daily auto-publishing (GitHub Pages)

`.github/workflows/daily-dashboard.yml` rebuilds the dashboard every day at 14:00 UTC, saves one trend point to `data/history.json`, and publishes the site. To turn it on in your copy of the repo:

1. **Settings > Secrets and variables > Actions > Secrets:** add `HPSC_EMAIL` and `HPSC_PASSWORD`.
2. **Settings > Secrets and variables > Actions > Variables (optional):** add `SITE_AUTHOR` (your name) and `SITE_AUTHOR_URL` (e.g. your LinkedIn profile).
3. **Settings > Pages > Build and deployment > Source:** choose **GitHub Actions**.
4. **Actions tab > Daily dashboard > Run workflow** to publish the first version now.

## The MCP server

Lets Claude answer questions like:

- "Is amoxicillin 500 mg in shortage right now? When is it expected back?"
- "Check these 30 DINs from our formulary and tell me what's affected."
- "List all current Tier 3 shortages."
- "DIN 02242705 is short. What other marketed products have the same ingredient, and are any of those short too?"

It connects to two official sources:

| Source | What it gives | Account? |
|---|---|---|
| Health Product Shortages Canada API (healthproductshortages.ca) | Shortage and discontinuation reports, status, reason, dates, Tier 3 | Yes, free. 1,000 requests/hour |
| Health Canada Drug Product Database API (health-products.canada.ca/api/drug) | Ingredients, strength, form, route, ATC, marketing status | No |

### Tools

| Tool | What it does |
|---|---|
| `search_shortages` | Search by name, ingredient, company or DIN, with optional status filter |
| `get_report` | Full detail of one shortage or discontinuation report |
| `check_watchlist` | Check up to 50 DINs or names at once; flags OK / AFFECTED / TIER 3, and notes when a drug's only reports are past their end date or not updated in 12+ months |
| `list_tier3` | Current Tier 3 (highest-impact) reports |
| `recent_updates` | Newest updated reports by status, for a daily digest |
| `lookup_drug` | Drug details from the Drug Product Database |
| `find_alternatives` | Same-ingredient, same-form, same-route marketed products, each checked for shortages |
| `data_quality_scan` | Scans every open report and returns summary numbers: past end date, not updated, duplicates, partial supply, companies with the most overdue reports |
| `build_dashboard` | Builds the dashboard from live data and saves it on your computer |

Every report carries a `quality` block: the freshness label, a `check_with_supplier` flag, days past the estimated end date, days since the last update, any partial-supply note ("100 BTL available"), and how many open reports exist for the same DIN.

### Setup (about 10 minutes)

1. **Create an account** on https://healthproductshortages.ca and verify your email. Accounts that existed before the January 2026 site move must reset their password first.
2. **Install** (needs Python 3.10+): `pip install .`
3. **Add to Claude Desktop** (Settings > Developer or Extensions > Edit config):
   ```json
   {
     "mcpServers": {
       "health-product-shortages-canada": {
         "command": "hpsc-mcp",
         "env": {
           "HPSC_EMAIL": "you@example.com",
           "HPSC_PASSWORD": "your-password"
         }
       }
     }
   }
   ```
   If Claude can't find `hpsc-mcp`, use the full path to `python` with `"args": ["-m", "hpsc_mcp.server"]`.
4. Fully quit and reopen Claude Desktop, then ask: "Search Canadian drug shortages for amoxicillin."

Quick login test: `curl.exe "https://healthproductshortages.ca/api/v1/login" -D - -X POST --data-urlencode "email=YOU" --data-urlencode "password=PASS"` should return `200` and an `auth-token` header.

## Tests

```
pip install -e ".[test]"
pytest
```

Tests use simulated responses and need no network or account.

## Notes and limits

- The API base is `https://healthproductshortages.ca/api/v1` (set `HPSC_BASE_URL` to override). Multi-report pulls sort by report ID ascending with 100 per page, as the API docs require. Results are cached for 10 minutes.
- For the whole database, the reporting site recommends its monthly data extract; this project only pulls open reports plus the 100 most recently resolved.
- `find_alternatives` only matches the same active ingredients, form and route. It does not suggest therapeutic substitutes and does not check provincial formulary coverage. Controlled drugs cannot be therapeutically substituted by pharmacists.
- Report links use `healthproductshortages.ca/shortage/<id>` (confirmed); discontinuation links assume `/discontinuance/<id>`.
- Check the reporting site's terms of use before republishing its data or using it commercially.

## License

MIT. Data © its respective sources; see healthproductshortages.ca.
