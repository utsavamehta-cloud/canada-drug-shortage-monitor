"""
Build the Canadian Drug Shortage Monitor: a single-file static dashboard.

    hpsc-dashboard --out site            # writes site/index.html and site/data.json
    hpsc-dashboard --out site --history data/history.json

Needs HPSC_EMAIL / HPSC_PASSWORD (same as the MCP server).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import server as s

ATC_GROUPS = {
    "A": "Digestive & metabolism",
    "B": "Blood",
    "C": "Heart & blood vessels",
    "D": "Skin",
    "G": "Genito-urinary & hormones",
    "H": "Systemic hormones",
    "J": "Anti-infectives",
    "L": "Cancer & immune",
    "M": "Muscles & bones",
    "N": "Nervous system",
    "P": "Antiparasitics",
    "R": "Respiratory",
    "S": "Eyes & ears",
    "V": "Various",
}
CONF_ORDER = ["Probably outdated", "Overdue for update", "No end date given",
              "Current per report", "Recently updated"]
CONF_RANK = {c: i for i, c in enumerate(CONF_ORDER)}
STATUS_LABEL = {"active_confirmed": "Actual shortage", "anticipated_shortage": "Anticipated"}


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def collect(max_pages: int = 100) -> tuple[list[dict], list[dict], bool]:
    """Open reports (actual + anticipated) and the 100 most recently updated resolved ones."""
    open_reports: list[dict] = []
    complete = True
    for st in s.OPEN_STATUSES:
        rows, done = s._scan(None, st, max_pages)
        complete = complete and done
        open_reports.extend(s.summarize(r) for r in rows)
    resolved_rows, _ = s.hpsc().search(status="resolved", orderby="updated_date", order="desc", limit=100)
    resolved = [s.summarize(r) for r in resolved_rows]
    return open_reports, resolved, complete


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

def _d(v: Any) -> str | None:
    return v[:10] if isinstance(v, str) and len(v) >= 10 else None


def _atc_group(code: Any) -> str:
    c = (code or "").strip()[:1].upper()
    return c if c in ATC_GROUPS else "?"


def build_payload(open_reports: list[dict], resolved: list[dict], today: dt.date,
                  history: list[dict] | None = None, complete: bool = True) -> dict:
    s.annotate(open_reports, today)
    stats = s.quality_stats(open_reports, today, top_n=15)

    # One row per DIN (duplicates merged), newest report first.
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in open_reports:
        groups[r.get("din") or f"report-{r['report_id']}"].append(r)
    rows = []
    for key, reps in groups.items():
        reps.sort(key=lambda r: r.get("updated") or "", reverse=True)
        lead = reps[0]
        statuses = {r.get("status") for r in reps}
        rows.append({
            "din": lead.get("din") or "",
            "brand": lead.get("brand_name") or "",
            "name": (lead.get("common_name") or "").title(),
            "strength": (lead.get("strength") or "").replace("\n", " / "),
            "form": (lead.get("dosage_form") or "").capitalize(),
            "route": (lead.get("route") or "").capitalize(),
            "company": lead.get("company") or "",
            "status": "active_confirmed" if "active_confirmed" in statuses else "anticipated_shortage",
            "tier3": any(r.get("tier_3") for r in reps),
            "conf": lead["quality"]["confidence"],
            "est_end": _d(lead.get("estimated_end")),
            "updated": _d(lead.get("updated")),
            "started": _d(lead.get("actual_start") or lead.get("anticipated_start")),
            "reason": lead.get("reason") or "",
            "supply": next((r["quality"]["partial_supply"] for r in reps if r["quality"]["partial_supply"]), None),
            "reports": [r["report_id"] for r in reps],
            "late": any(r.get("late_submission") for r in reps),
            "atc": lead.get("atc") or "",
            "atcg": _atc_group(lead.get("atc")),
            "days_upd": lead["quality"]["days_since_update"],
        })
    rows.sort(key=lambda x: (x["tier3"], x["updated"] or ""), reverse=True)  # Tier 3 first, then newest

    # Company freshness table (companies with 10+ open reports).
    per_co: dict[str, list[dict]] = defaultdict(list)
    for r in open_reports:
        per_co[r.get("company") or "Unknown"].append(r)
    companies = []
    for co, reps in per_co.items():
        n = len(reps)
        if n < 10:
            continue
        days = [r["quality"]["days_since_update"] for r in reps if r["quality"]["days_since_update"] is not None]
        companies.append({
            "company": co, "open": n,
            "outdated_pct": round(100 * sum(r["quality"]["confidence"] == "Probably outdated" for r in reps) / n, 1),
            "past_end_pct": round(100 * sum(r["quality"]["end_date_passed_days"] > 0 for r in reps) / n, 1),
            "no_end_pct": round(100 * sum(bool(r.get("end_date_unknown") or not r.get("estimated_end")) for r in reps) / n, 1),
            "median_days": int(statistics.median(days)) if days else None,
            "late_pct": round(100 * sum(bool(r.get("late_submission")) for r in reps) / n, 1),
        })
    companies.sort(key=lambda c: (-c["outdated_pct"], -c["open"]))

    # Therapeutic groups.
    atc = Counter()
    atc_out = Counter()
    for r in open_reports:
        g = _atc_group(r.get("atc"))
        atc[g] += 1
        atc_out[g] += r["quality"]["confidence"] == "Probably outdated"
    atc_rows = sorted(({"code": g, "name": ATC_GROUPS.get(g, "Unclassified"), "open": n,
                        "outdated": atc_out[g]} for g, n in atc.items()), key=lambda x: -x["open"])

    # What changed in the last 7 days.
    week_ago = (today - dt.timedelta(days=7)).isoformat()
    new = [r for r in open_reports if (_d(r.get("created")) or "") >= week_ago]
    changed = [r for r in open_reports if (_d(r.get("updated")) or "") >= week_ago and r not in new]
    ended = [r for r in resolved if (_d(r.get("updated")) or "") >= week_ago]

    def brief(r: dict) -> dict:
        return {"id": r["report_id"], "brand": r.get("brand_name") or "", "din": r.get("din") or "",
                "company": r.get("company") or "", "status": r.get("status"), "tier3": bool(r.get("tier_3")),
                "updated": _d(r.get("updated")), "est_end": _d(r.get("estimated_end")),
                "strength": (r.get("strength") or "").replace("\n", " / ")}

    changes = {
        "new": sorted(map(brief, new), key=lambda x: x["updated"] or "", reverse=True),
        "updated": sorted(map(brief, changed), key=lambda x: x["updated"] or "", reverse=True),
        "resolved": sorted(map(brief, ended), key=lambda x: x["updated"] or "", reverse=True),
    }

    snapshot = {
        "date": today.isoformat(),
        "open": stats["open_reports"],
        "outdated_pct": round(100 * stats["confidence_labels"].get("Probably outdated", 0) / max(1, stats["open_reports"]), 1),
        "no_end_pct": stats["no_end_date_given"]["pct"],
        "dup_pct": stats["duplicates"]["pct_of_open_reports_that_are_extra"],
        "tier3": stats["tier_3_open"],
    }
    hist = [h for h in (history or []) if h.get("date") != snapshot["date"]] + [snapshot]
    hist.sort(key=lambda h: h["date"])

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "as_of": today.isoformat(),
        "complete": complete,
        "kpi": {
            "open_reports": stats["open_reports"],
            "products": len(rows),
            "actual": stats["by_status"].get("active_confirmed", 0),
            "anticipated": stats["by_status"].get("anticipated_shortage", 0),
            "tier3": stats["tier_3_open"],
            "changed_7d": len(new) + len(changed),
            "resolved_7d": len(ended),
        },
        "quality": {
            "labels": [{"label": c, "count": stats["confidence_labels"].get(c, 0)} for c in reversed(CONF_ORDER)],
            "not_updated": stats["not_updated_in"],
            "no_end": stats["no_end_date_given"],
            "past_end": stats["past_estimated_end_date"],
            "duplicates": {k: v for k, v in stats["duplicates"].items() if k != "worst"},
            "late": stats["late_submissions"],
            "partial": stats["partial_supply_mentioned"],
        },
        "companies": companies,
        "atc": atc_rows,
        "changes": changes,
        "history": hist,
        "rows": rows,
    }, hist


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def render_html(payload: dict, author: str = "", author_url: str = "", repo_url: str = "") -> str:
    tpl = (Path(__file__).parent / "dashboard_template.html").read_text(encoding="utf-8")
    meta = {"author": author, "author_url": author_url, "repo_url": repo_url}
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    return tpl.replace("/*__DATA__*/null", data).replace("/*__META__*/null", json.dumps(meta))


def build(out_dir: str | os.PathLike, history_path: str | os.PathLike | None = None,
          today: dt.date | None = None, author: str = "", author_url: str = "", repo_url: str = "") -> dict:
    today = today or dt.date.today()
    open_reports, resolved, complete = collect()
    history = []
    if history_path and Path(history_path).exists():
        history = json.loads(Path(history_path).read_text(encoding="utf-8"))
    payload, hist = build_payload(open_reports, resolved, today, history, complete)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render_html(payload, author, author_url, repo_url), encoding="utf-8")
    (out / "data.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if history_path:
        Path(history_path).parent.mkdir(parents=True, exist_ok=True)
        Path(history_path).write_text(json.dumps(hist, indent=1), encoding="utf-8")
    return {"index_html": str((out / "index.html").resolve()), "kpi": payload["kpi"], "complete": complete}


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the Canadian Drug Shortage Monitor")
    ap.add_argument("--out", default="site")
    ap.add_argument("--history", default=None, help="JSON file that keeps one summary row per day")
    ap.add_argument("--author", default=os.environ.get("SITE_AUTHOR", ""))
    ap.add_argument("--author-url", default=os.environ.get("SITE_AUTHOR_URL", ""))
    ap.add_argument("--repo-url", default=os.environ.get("SITE_REPO_URL", ""))
    a = ap.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    print(json.dumps(build(a.out, a.history, author=a.author, author_url=a.author_url, repo_url=a.repo_url), indent=1))


if __name__ == "__main__":
    main()
