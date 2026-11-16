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

from . import dpd_extract
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
FRESH_ORDER = s.FRESHNESS_ORDER  # fresh -> check first
AGE_BUCKETS = [("Under 3 months", 0, 90), ("3 to 6 months", 91, 180), ("6 to 12 months", 181, 365), ("Over 1 year", 366, 10**6)]
STATUS_LABEL = {"active_confirmed": "Actual shortage", "anticipated_shortage": "Anticipated"}
# How much of the same-ingredient supply is reported short. Lower rank sorts first.
IMPACT_RANK = {"none_left": 0, "most": 1, "some": 2, "unknown": 3}
log = logging.getLogger(__name__)


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


def collect_products() -> list[dict] | None:
    """Marketed products from Health Canada's DPD extract, or None if it can't be read.

    The dashboard still builds without it; the same-ingredient views are just left out.
    """
    try:
        return dpd_extract.load(dpd_extract.fetch())
    except Exception as e:  # network, format change, anything: never block the daily build
        log.warning("DPD extract not available, building without same-ingredient data: %s", e)
        return None


# ---------------------------------------------------------------------------
# Same-ingredient groups
# ---------------------------------------------------------------------------

def impact_of(total: int, short: int) -> str:
    """none_left: every marketed same-ingredient product has an open report
    (including when there is only one). most: half or more. some: under half."""
    if total <= 0:
        return "unknown"
    if short >= total:
        return "none_left"
    return "most" if short * 2 >= total else "some"


def add_groups(rows: list[dict], products: list[dict] | None) -> dict | None:
    """Attach to every row the group of marketed products with the same
    ingredient(s), strength, form and route. Returns the groups that contain at
    least one product with an open report, keyed by a short id."""
    if not products:
        for r in rows:
            r.update({"g": None, "gn": 0, "gs": 0, "impact": "unknown"})
        return None
    by_din: dict[str, dict] = {}
    members: dict[str, list[dict]] = defaultdict(list)
    for p in products:
        if p["din"] in by_din:
            continue
        by_din[p["din"]] = p
        members[p["key"]].append(p)
    short_dins = {dpd_extract.din8(r["din"]) for r in rows if r["din"]}
    groups: dict[str, dict] = {}
    ids: dict[str, str] = {}
    for r in rows:
        p = by_din.get(dpd_extract.din8(r["din"])) if r["din"] else None
        if not p:
            r.update({"g": None, "gn": 0, "gs": 0, "impact": "unknown"})
            continue
        key = p["key"]
        if key not in ids:
            ids[key] = str(len(ids) + 1)
            mem = sorted(members[key], key=lambda m: (m["din"] in short_dins, m["company"], m["brand"]))
            groups[ids[key]] = {
                "label": p["label"],
                "n": len(mem),
                "s": sum(m["din"] in short_dins for m in mem),
                "m": [[m["din"], m["brand"], m["company"]] for m in mem],
                # Injectable, inhaled, patch or biologic: same ingredient is not always the same presentation.
                "check": any(m.get("check") for m in mem),
            }
        g = groups[ids[key]]
        g["impact"] = impact_of(g["n"], g["s"])
        r.update({"g": ids[key], "gn": g["n"], "gs": g["s"], "impact": g["impact"], "check": g["check"]})
    return groups


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

def _d(v: Any) -> str | None:
    return v[:10] if isinstance(v, str) and len(v) >= 10 else None


def _atc_group(code: Any) -> str:
    c = (code or "").strip()[:1].upper()
    return c if c in ATC_GROUPS else "?"


def build_payload(open_reports: list[dict], resolved: list[dict], today: dt.date,
                  history: list[dict] | None = None, complete: bool = True,
                  products: list[dict] | None = None) -> dict:
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
            "fresh": lead["quality"]["freshness"],
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
    groups = add_groups(rows, products)
    # Tier 3 first, then the products with the fewest same-ingredient options left, then newest.
    rows.sort(key=lambda x: x["updated"] or "", reverse=True)
    rows.sort(key=lambda x: (not x["tier3"], IMPACT_RANK[x["impact"]]))

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
            "no_update_pct": round(100 * sum((r["quality"]["days_since_update"] or 0) > 365 for r in reps) / n, 1),
            "past_end_pct": round(100 * sum(r["quality"]["end_date_passed_days"] > 0 for r in reps) / n, 1),
            "no_end_pct": round(100 * sum(bool(r.get("end_date_unknown") or not r.get("estimated_end")) for r in reps) / n, 1),
            "median_days": int(statistics.median(days)) if days else None,
            "late_pct": round(100 * sum(bool(r.get("late_submission")) for r in reps) / n, 1),
        })
    companies.sort(key=lambda c: (-c["no_update_pct"], -c["open"]))

    # Therapeutic groups.
    atc = Counter()
    atc_out = Counter()
    for r in open_reports:
        g = _atc_group(r.get("atc"))
        atc[g] += 1
        atc_out[g] += (r["quality"]["days_since_update"] or 0) > 365
    atc_rows = sorted(({"code": g, "name": ATC_GROUPS.get(g, "Unclassified"), "open": n,
                        "no_update": atc_out[g]} for g, n in atc.items()), key=lambda x: -x["open"])

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
        "no_update_12m_pct": stats["not_updated_in"]["over_365_days"]["pct"],
        "no_end_pct": stats["no_end_date_given"]["pct"],
        "dup_pct": stats["duplicates"]["pct_of_open_reports_that_are_extra"],
        "tier3": stats["tier_3_open"],
    }
    old = []
    for h in history or []:
        h = dict(h)
        if "no_update_12m_pct" not in h and "outdated_pct" in h:  # older builds used a different name
            h["no_update_12m_pct"] = h.pop("outdated_pct")
        if h.get("date") != snapshot["date"]:
            old.append(h)
    hist = old + [snapshot]
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
            "none_left": sum(1 for r in rows if r["impact"] == "none_left"),
            "matched": sum(1 for r in rows if r["impact"] != "unknown"),
        },
        "dpd": {"marketed_products": len({p["din"] for p in products})} if products else None,
        "groups": groups or {},
        "quality": {
            "labels": [{"label": c, "count": stats["freshness_labels"].get(c, 0)} for c in FRESH_ORDER],
            "age_buckets": [{"label": name, "count": sum(1 for r in open_reports
                             if r["quality"]["days_since_update"] is not None and lo <= r["quality"]["days_since_update"] <= hi)}
                            for name, lo, hi in AGE_BUCKETS],
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
          today: dt.date | None = None, author: str = "", author_url: str = "", repo_url: str = "",
          products: list[dict] | None = None, use_dpd: bool = True) -> dict:
    today = today or dt.date.today()
    open_reports, resolved, complete = collect()
    if products is None and use_dpd:
        products = collect_products()
    history = []
    if history_path and Path(history_path).exists():
        history = json.loads(Path(history_path).read_text(encoding="utf-8"))
    payload, hist = build_payload(open_reports, resolved, today, history, complete, products)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render_html(payload, author, author_url, repo_url), encoding="utf-8")
    (out / "data.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if history_path:
        Path(history_path).parent.mkdir(parents=True, exist_ok=True)
        Path(history_path).write_text(json.dumps(hist, indent=1), encoding="utf-8")
    return {"index_html": str((out / "index.html").resolve()), "kpi": payload["kpi"], "complete": complete,
            "same_ingredient_data": bool(products)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the Canadian Drug Shortage Monitor")
    ap.add_argument("--out", default="site")
    ap.add_argument("--history", default=None, help="JSON file that keeps one summary row per day")
    ap.add_argument("--author", default=os.environ.get("SITE_AUTHOR", ""))
    ap.add_argument("--author-url", default=os.environ.get("SITE_AUTHOR_URL", ""))
    ap.add_argument("--repo-url", default=os.environ.get("SITE_REPO_URL", ""))
    ap.add_argument("--no-dpd", action="store_true",
                    help="Skip Health Canada's Drug Product Database extract (no same-ingredient views)")
    a = ap.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    print(json.dumps(build(a.out, a.history, author=a.author, author_url=a.author_url, repo_url=a.repo_url,
                           use_dpd=not a.no_dpd), indent=1))


if __name__ == "__main__":
    main()
