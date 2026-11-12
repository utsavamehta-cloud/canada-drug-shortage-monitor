"""
Health Product Shortages Canada (HPSC) MCP server.

Lets Claude (or any MCP client) search Canada's mandatory drug shortage and
discontinuation database, check a watchlist of drugs, flag Tier 3 reports, and
look for same-ingredient alternatives using Health Canada's Drug Product
Database (DPD).

Data sources
  - HPSC public API (account required): search, shortage and discontinuation
    report endpoints, auth-token header, 1000 requests/hour per account.
  - Health Canada DPD API (no account): https://health-products.canada.ca/api/drug/

Information only. Not clinical advice. Always confirm with a pharmacist and the
source report before acting.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time
from collections import Counter, defaultdict
from typing import Any, Iterable

import httpx
try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Base URL from the site's "Public web API examples" page (Apr 2026).
# Override with HPSC_BASE_URL if it changes again.
DEFAULT_HPSC_BASE = "https://healthproductshortages.ca/api/v1"
# Per the same page: when extracting multiple reports, sort by id ascending
# and use a limit of 100. The whole database should come from the site's
# monthly data extract, not the API.
SCAN_PAGE_SIZE = 100
DEFAULT_DPD_BASE = "https://health-products.canada.ca/api/drug"

VALID_STATUSES = {
    "resolved",
    "anticipated_shortage",
    "active_confirmed",
    "avoided_shortage",
    "discontinued",
}
VALID_ORDERBY = {"id", "company_name", "brand_name", "status", "type", "updated_date"}
ACTIVE_STATUSES = ("active_confirmed", "anticipated_shortage")

DISCLAIMER = (
    "Information only, from manufacturer reports to Health Canada. "
    "Not clinical advice. Confirm with a pharmacist and the source report."
)


class HPSCError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# HPSC API client
# ---------------------------------------------------------------------------


class HPSCClient:
    """Thin client for the HPSC public API with token caching and retries."""

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        token_ttl_seconds: int = 3000,
    ) -> None:
        self.email = email or os.environ.get("HPSC_EMAIL", "")
        self.password = password or os.environ.get("HPSC_PASSWORD", "")
        self.base_url = (base_url or os.environ.get("HPSC_BASE_URL") or DEFAULT_HPSC_BASE).rstrip("/")
        self._http = httpx.Client(timeout=30.0, transport=transport, follow_redirects=True)
        self._token: str | None = None
        self._token_time = 0.0
        self._token_ttl = token_ttl_seconds
        self._cache: dict[str, tuple[float, Any]] = {}
        self.cache_seconds = 600  # reports change slowly; protects the rate limit

    # -- auth ---------------------------------------------------------------
    def _login(self) -> str:
        if not self.email or not self.password:
            raise HPSCError(
                "Missing HPSC_EMAIL / HPSC_PASSWORD. Create a free account on "
                "healthproductshortages.ca and set them in your MCP config."
            )
        r = self._http.post(
            f"{self.base_url}/login",
            data={"email": self.email, "password": self.password},
        )
        if r.status_code == 403:
            raise HPSCError("HPSC account not verified. Confirm your email first.")
        if r.status_code != 200:
            raise HPSCError(f"HPSC login failed ({r.status_code}): {_error_text(r)}")
        token = r.headers.get("auth-token")
        if not token:
            raise HPSCError("HPSC login succeeded but no auth-token header was returned.")
        self._token, self._token_time = token, time.time()
        return token

    def _get_token(self) -> str:
        if self._token and time.time() - self._token_time < self._token_ttl:
            return self._token
        return self._login()

    # -- requests -----------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.cache_seconds:
            return hit[1]

        for attempt in range(3):
            r = self._http.get(
                f"{self.base_url}/{path}",
                params=params,
                headers={"auth-token": self._get_token()},
            )
            if r.status_code == 403 and attempt == 0:
                self._token = None  # token expired; log in again once
                continue
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise HPSCError("HPSC rate limit reached (1000 requests/hour). Try again later.")
            if r.status_code == 404:
                raise HPSCError("Report not found.")
            if r.status_code != 200:
                raise HPSCError(f"HPSC request failed ({r.status_code}): {_error_text(r)}")
            data = r.json()
            self._cache[key] = (time.time(), data)
            return data
        raise HPSCError("HPSC request failed after retries.")

    def search(
        self,
        term: str | None = None,
        din: str | None = None,
        report_id: str | None = None,
        status: str | None = None,
        orderby: str = "updated_date",
        order: str = "desc",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict], int | None]:
        params: dict[str, Any] = {"orderby": orderby, "order": order, "limit": limit, "offset": offset}
        if term:
            params["term"] = term
        if din:
            params["din"] = _clean_din(din)
        if report_id:
            params["report_id"] = report_id
        if status:
            params["filter_status"] = status
        data = self._get("search", params)
        return _extract_list(data)

    def shortage(self, report_id: str | int) -> dict:
        return _unwrap(self._get(f"shortages/{report_id}"))

    def discontinuance(self, report_id: str | int) -> dict:
        return _unwrap(self._get(f"discontinuances/{report_id}"))


# ---------------------------------------------------------------------------
# DPD API client (public, no account)
# ---------------------------------------------------------------------------


class DPDClient:
    def __init__(self, base_url: str | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = (base_url or os.environ.get("DPD_BASE_URL") or DEFAULT_DPD_BASE).rstrip("/")
        self._http = httpx.Client(timeout=30.0, transport=transport, follow_redirects=True)

    def _get(self, endpoint: str, **params: Any) -> list[dict]:
        params.setdefault("lang", "en")
        params.setdefault("type", "json")
        r = self._http.get(f"{self.base_url}/{endpoint}/", params=params)
        if r.status_code != 200:
            raise HPSCError(f"DPD request failed ({r.status_code}).")
        data = r.json()
        if isinstance(data, dict):
            data = data.get("result", [data])
        return data if isinstance(data, list) else [data]

    def product_by_din(self, din: str) -> dict | None:
        rows = self._get("drugproduct", din=_clean_din(din))
        return rows[0] if rows else None

    def products_by_brand(self, brand: str) -> list[dict]:
        return self._get("drugproduct", brandname=brand)

    def product(self, drug_code: int | str) -> dict | None:
        rows = self._get("drugproduct", id=drug_code)
        return rows[0] if rows else None

    def ingredients(self, drug_code: int | str) -> list[dict]:
        return self._get("activeingredient", id=drug_code)

    def products_with_ingredient(self, name: str) -> list[dict]:
        return self._get("activeingredient", ingredientname=name)

    def form(self, drug_code: int | str) -> list[dict]:
        return self._get("form", id=drug_code)

    def route(self, drug_code: int | str) -> list[dict]:
        return self._get("route", id=drug_code)

    def status(self, drug_code: int | str) -> list[dict]:
        return self._get("status", id=drug_code)

    def atc(self, drug_code: int | str) -> list[dict]:
        return self._get("therapeuticclass", id=drug_code)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_text(r: httpx.Response) -> str:
    try:
        err = r.json().get("error", {})
        return err.get("en") or str(err)
    except Exception:
        return r.text[:200]


def _clean_din(din: str) -> str:
    digits = "".join(ch for ch in str(din) if ch.isdigit())
    return digits.zfill(8) if digits else str(din)


def _extract_list(data: Any) -> tuple[list[dict], int | None]:
    """The list response shape is not fully documented, so accept common shapes."""
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict):
        for key in ("data", "results", "items", "reports"):
            if isinstance(data.get(key), list):
                total = (data.get("total") or data.get("total_count") or data.get("count")
                         or data.get("total_results"))
                return data[key], total
    return [], None


def _unwrap(data: Any) -> dict:
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data if isinstance(data, dict) else {}


def _pick(d: dict, *keys: str, default: Any = None) -> Any:
    """Return the first present value. Supports nested 'a.b' and en/fr dicts."""
    for k in keys:
        cur: Any = d
        for part in k.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if cur not in (None, "", []):
            if isinstance(cur, dict) and ("en" in cur or "en_name" in cur):
                return cur.get("en") or cur.get("en_name")
            return cur
    return default


def is_tier3(report: dict) -> bool:
    if "tier_3" in report:
        return bool(report["tier_3"])
    for k, v in report.items():
        if "tier" in k.lower() and v not in (None, False, 0, "0", "", "No", "no", "false"):
            return True
    return False


def summarize(report: dict) -> dict:
    """Normalize a raw report into a compact, predictable shape for Claude."""
    rid = _pick(report, "id")
    rtype = str(_pick(report, "type.label", "type", default="")).lower()
    kind = "discontinuation" if "discon" in rtype else "shortage"
    path = "discontinuance" if kind == "discontinuation" else "shortage"
    return {
        "report_id": rid,
        "type": kind,
        "status": _pick(report, "status"),
        "tier_3": is_tier3(report),
        "brand_name": _pick(report, "en_drug_brand_name", "drug.brand_name", "brand_name"),
        "common_name": _pick(report, "en_drug_common_name"),
        "ingredients": _pick(report, "en_ingredients", "ingredients"),
        "din": _pick(report, "din", "drug.din"),
        "strength": _pick(report, "drug_strength", "strength"),
        "dosage_form": _pick(report, "drug_dosage_form", "dosage_form"),
        "route": _dedupe_lines(_pick(report, "drug_route", "route")),
        "company": _pick(report, "company_name", "company.name"),
        "reason": _pick(report, "shortage_reason.en_reason", "discontinuation_reason.en_reason",
                        "shortage_reason", "reason"),
        "comments": _pick(report, "en_comments"),
        "anticipated_start": _pick(report, "anticipated_start_date"),
        "actual_start": _pick(report, "actual_start_date"),
        "estimated_end": _pick(report, "estimated_end_date"),
        "end_date_unknown": _pick(report, "unknown_estimated_end_date"),
        "actual_end": _pick(report, "actual_end_date"),
        "discontinuation_date": _pick(report, "discontinuation_date"),
        "late_submission": _pick(report, "late_submission"),
        "created": _pick(report, "created_date"),
        "updated": _pick(report, "updated_date"),
        "atc": _pick(report, "atc_number"),
        "atc_description": _pick(report, "atc_description"),
        # /shortage/<id> confirmed on the live site; /discontinuance/<id> assumed by analogy.
        "url": f"https://healthproductshortages.ca/{path}/{rid}" if rid else None,
    }



# ---------------------------------------------------------------------------
# Data-quality flags
# ---------------------------------------------------------------------------

_SUPPLY_RE = re.compile(
    r"(\d[\d,]*)\s*(btls?|bls|bottles?|blisters?|units?|vials?|packs?|boxes|box|cartons?|tubes?|cs|cases?|ea)\b[^.]*?\bavail\w*",
    re.IGNORECASE,
)
OPEN_STATUSES = ("active_confirmed", "anticipated_shortage")
FRESHNESS_ORDER = ["Updated in last 14 days", "End date still ahead", "No end date given",
                   "No update in 12+ months", "Past estimated end date"]
CHECK_FIRST = {"No update in 12+ months", "Past estimated end date"}


def _today() -> dt.date:
    return dt.date.today()


def _as_date(v: Any) -> dt.date | None:
    if not v or not isinstance(v, str):
        return None
    try:
        return dt.date.fromisoformat(v[:10])
    except ValueError:
        return None


def quality_flags(summary: dict, today: dt.date | None = None) -> dict:
    """Plain-language data-quality signals for one report."""
    today = today or _today()
    status = str(summary.get("status") or "").lower()
    open_ = status in OPEN_STATUSES
    end = _as_date(summary.get("estimated_end"))
    upd = _as_date(summary.get("updated"))
    days_since_update = (today - upd).days if upd else None
    past_due_days = (today - end).days if (open_ and end and end < today) else 0
    comments = summary.get("comments") or ""
    m = _SUPPLY_RE.search(comments)
    partial = m.group(0) if m else None

    # Factual labels only: each one states a date fact, never a judgement.
    if not open_:
        label = "Closed report"
    elif past_due_days > 0:
        label = "Past estimated end date"
    elif days_since_update is not None and days_since_update > 365:
        label = "No update in 12+ months"
    elif summary.get("end_date_unknown") or not end:
        label = "No end date given"
    elif days_since_update is not None and days_since_update <= 14:
        label = "Updated in last 14 days"
    else:
        label = "End date still ahead"
    return {
        "freshness": label,
        "check_with_supplier": label in CHECK_FIRST,
        "end_date_passed_days": past_due_days,
        "days_since_update": days_since_update,
        "partial_supply": partial,
    }


def annotate(reports: list[dict], today: dt.date | None = None) -> list[dict]:
    """Add quality flags and duplicate counts (open reports for the same DIN)."""
    open_by_din: Counter = Counter(
        r.get("din") for r in reports
        if r.get("din") and str(r.get("status") or "").lower() in OPEN_STATUSES
    )
    for r in reports:
        q = quality_flags(r, today)
        n = open_by_din.get(r.get("din"), 0)
        q["open_reports_for_same_din"] = n if str(r.get("status") or "").lower() in OPEN_STATUSES else None
        r["quality"] = q
    return reports


def _dedupe_lines(v: Any) -> Any:
    if not isinstance(v, str):
        return v
    seen = []
    for part in v.replace("\r", "").split("\n"):
        part = part.strip()
        if part and part not in seen:
            seen.append(part)
    return ", ".join(seen)


def _is_active(summary: dict) -> bool:
    s = str(summary.get("status") or "").lower()
    return any(word in s for word in ("active", "anticipated", "actual", "confirmed", "to_be", "discontinued"))


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("health-product-shortages-canada")
_hpsc: HPSCClient | None = None
_dpd: DPDClient | None = None


def hpsc() -> HPSCClient:
    global _hpsc
    if _hpsc is None:
        _hpsc = HPSCClient()
    return _hpsc


def dpd() -> DPDClient:
    global _dpd
    if _dpd is None:
        _dpd = DPDClient()
    return _dpd


def _fail(e: Exception) -> dict:
    return {"error": str(e), "note": DISCLAIMER}


@mcp.tool()
def search_shortages(
    query: str | None = None,
    din: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Search Canadian drug shortage and discontinuation reports.

    Args:
        query: Brand name, ingredient, company or ATC text (e.g. "amoxicillin").
        din: 8-digit Drug Identification Number, if known.
        status: One of resolved, anticipated_shortage, active_confirmed,
            avoided_shortage, discontinued. Leave empty for all.
        limit: Max reports to return (1-100).
        offset: For paging through results.
    """
    if status and status not in VALID_STATUSES:
        return {"error": f"status must be one of {sorted(VALID_STATUSES)}"}
    if not query and not din:
        return {"error": "Give a query or a DIN."}
    try:
        rows, total = hpsc().search(term=query, din=din, status=status,
                                    limit=max(1, min(limit, 100)), offset=offset)
    except HPSCError as e:
        return _fail(e)
    reports = annotate([summarize(r) for r in rows])
    return {"total": total, "returned": len(reports), "reports": reports, "note": DISCLAIMER}


@mcp.tool()
def get_report(report_id: str, report_type: str = "shortage") -> dict:
    """Get the full detail of one report.

    Args:
        report_id: The report number shown on healthproductshortages.ca.
        report_type: "shortage" or "discontinuation".
    """
    try:
        raw = hpsc().discontinuance(report_id) if report_type.startswith("discon") else hpsc().shortage(report_id)
    except HPSCError as e:
        return _fail(e)
    return {"summary": annotate([summarize(raw)])[0], "raw": raw, "note": DISCLAIMER}


@mcp.tool()
def check_watchlist(items: list[str], include_anticipated: bool = True) -> dict:
    """Check a list of drugs (DINs or names) for current shortages or discontinuations.

    Use this for a pharmacy formulary, a clinic's top drugs, or a patient's med list.

    Args:
        items: DINs (8 digits) and/or drug names, up to 50.
        include_anticipated: Also flag anticipated shortages, not just actual ones.
    """
    items = [i.strip() for i in items if i and i.strip()][:50]
    out = []
    for item in items:
        is_din = item.replace(" ", "").isdigit()
        try:
            rows, _ = hpsc().search(din=item if is_din else None, term=None if is_din else item,
                                    limit=50, orderby="updated_date", order="desc")
        except HPSCError as e:
            out.append({"item": item, "error": str(e)})
            continue
        current = []
        for s in (summarize(r) for r in rows):
            st = str(s["status"] or "").lower()
            if "resolved" in st or "avoided" in st or "reversed" in st:
                continue
            if "anticipated" in st and not include_anticipated:
                continue
            current.append(s)
        annotate(current)
        if not current:
            flag = "OK"
        elif any(c["tier_3"] for c in current):
            flag = "TIER 3"
        elif all(c["quality"]["check_with_supplier"] for c in current):
            flag = "AFFECTED (reports past their end date or not updated in 12+ months; confirm with supplier)"
        else:
            flag = "AFFECTED"
        out.append({"item": item, "flag": flag, "current_reports": current})
    affected = [o for o in out if o.get("flag") in ("AFFECTED", "TIER 3")]
    return {
        "checked": len(out),
        "affected": len(affected),
        "tier_3": sum(1 for o in out if o.get("flag") == "TIER 3"),
        "results": out,
        "note": DISCLAIMER,
    }


@mcp.tool()
def list_tier3(query: str | None = None, max_pages: int = 20, include_discontinued: bool = False) -> dict:
    """List current Tier 3 (highest-impact) shortages.

    The API has no Tier 3 filter, so this pages through active and anticipated
    reports (100 per page, sorted by report ID ascending, as the API docs
    require for multi-report extraction) and keeps the Tier 3 ones.

    Args:
        query: Optional text to narrow the scan (a drug, ingredient or company).
        max_pages: Safety cap on pages of 100 per status (1-50). Results are
            cached for 10 minutes, so repeat calls are cheap.
        include_discontinued: Also scan discontinued reports (a long, mostly
            historical list).
    """
    max_pages = max(1, min(max_pages, 50))
    statuses = ACTIVE_STATUSES + (("discontinued",) if include_discontinued else ())
    found: dict[Any, dict] = {}
    truncated = False
    try:
        for status in statuses:
            rows_seen, complete = _scan(query, status, max_pages)
            truncated = truncated or not complete
            for r in rows_seen:
                s = summarize(r)
                if s["tier_3"]:
                    found[s["report_id"]] = s
    except HPSCError as e:
        return _fail(e)
    out = {"tier_3_reports": list(found.values()), "count": len(found), "note": DISCLAIMER}
    if truncated:
        out["warning"] = "Stopped at max_pages before the end of the list; raise max_pages or add a query."
    return out


@mcp.tool()
def data_quality_scan(max_pages: int = 80, include_anticipated: bool = True, top_n: int = 10) -> dict:
    """Scan ALL open shortage reports and return summary data-quality numbers
    (no report list): how many are past their estimated end date, not updated
    in a long time, duplicated for the same DIN, or mention partial supply,
    plus the companies with the most overdue reports.

    Uses about 1 API request per 100 reports (sorted by ID ascending, as the
    API docs require). Results are cached for 10 minutes.

    Args:
        max_pages: Safety cap on pages of 100 per status (1-200).
        include_anticipated: Include anticipated shortages as well as actual ones.
        top_n: How many companies and examples to list.
    """
    max_pages = max(1, min(max_pages, 200))
    statuses = OPEN_STATUSES if include_anticipated else ("active_confirmed",)
    reports: list[dict] = []
    complete = True
    try:
        for st in statuses:
            rows, done = _scan(None, st, max_pages)
            complete = complete and done
            reports.extend(summarize(r) for r in rows)
    except HPSCError as e:
        return _fail(e)
    return {**quality_stats(reports, top_n=top_n), "complete_scan": complete,
            "scanned_on": _today().isoformat(), "note": DISCLAIMER}


def quality_stats(reports: list[dict], today: dt.date | None = None, top_n: int = 10) -> dict:
    today = today or _today()
    annotate(reports, today)
    n = len(reports)
    pct = lambda k: round(100 * k / n, 1) if n else 0.0
    by_status = Counter(str(r.get("status")) for r in reports)
    conf = Counter(r["quality"]["freshness"] for r in reports)
    past = [r for r in reports if r["quality"]["end_date_passed_days"] > 0]
    buckets = Counter()
    for r in past:
        d = r["quality"]["end_date_passed_days"]
        buckets["1-30 days"] += d <= 30
        buckets["31-180 days"] += 30 < d <= 180
        buckets["181-365 days"] += 180 < d <= 365
        buckets["over 1 year"] += d > 365
    upd = [r["quality"]["days_since_update"] for r in reports if r["quality"]["days_since_update"] is not None]
    not_upd = {k: sum(1 for d in upd if d > k) for k in (90, 180, 365)}
    din_counts = Counter(r.get("din") for r in reports if r.get("din"))
    dup_dins = {d: c for d, c in din_counts.items() if c > 1}
    extra_reports = sum(c - 1 for c in dup_dins.values())
    partial = sum(1 for r in reports if r["quality"]["partial_supply"])
    late = sum(1 for r in reports if r.get("late_submission") is True)
    no_end = sum(1 for r in reports if r.get("end_date_unknown") or not r.get("estimated_end"))
    tier3 = sum(1 for r in reports if r.get("tier_3"))

    per_co: dict[str, Counter] = defaultdict(Counter)
    for r in reports:
        c = per_co[r.get("company") or "UNKNOWN"]
        c["open"] += 1
        c["past_due"] += r["quality"]["end_date_passed_days"] > 0
        c["no_update_12m"] += (r["quality"]["days_since_update"] or 0) > 365
    companies = sorted(
        ({"company": k, "open_reports": v["open"], "past_end_date": v["past_due"],
          "past_end_date_pct": round(100 * v["past_due"] / v["open"], 1),
          "no_update_12m": v["no_update_12m"]} for k, v in per_co.items()),
        key=lambda x: (-x["past_end_date"], -x["open_reports"]))[:top_n]
    examples = sorted(past, key=lambda r: -r["quality"]["end_date_passed_days"])[:top_n]
    return {
        "open_reports": n,
        "by_status": dict(by_status),
        "tier_3_open": tier3,
        "freshness_labels": dict(conf),
        "past_estimated_end_date": {"count": len(past), "pct": pct(len(past)), "how_late": dict(buckets)},
        "no_end_date_given": {"count": no_end, "pct": pct(no_end)},
        "not_updated_in": {f"over_{k}_days": {"count": v, "pct": pct(v)} for k, v in not_upd.items()},
        "duplicates": {"dins_with_multiple_open_reports": len(dup_dins),
                       "extra_reports": extra_reports, "pct_of_open_reports_that_are_extra": pct(extra_reports),
                       "worst": sorted(dup_dins.items(), key=lambda x: -x[1])[:top_n]},
        "partial_supply_mentioned": {"count": partial, "pct": pct(partial)},
        "late_submissions": {"count": late, "pct": pct(late)},
        "companies_most_past_due": companies,
        "most_overdue_examples": [
            {"report_id": r["report_id"], "brand_name": r["brand_name"], "din": r["din"], "company": r["company"],
             "status": r["status"], "estimated_end": (r.get("estimated_end") or "")[:10],
             "updated": (r.get("updated") or "")[:10], "end_date_passed_days": r["quality"]["end_date_passed_days"]}
            for r in examples],
    }


@mcp.tool()
def build_dashboard(output_dir: str | None = None, author: str = "", author_url: str = "", repo_url: str = "") -> dict:
    """Build the Canadian Drug Shortage Monitor dashboard (a single HTML file)
    from today's live data and save it on this computer.

    Args:
        output_dir: Folder to write into. Default: Documents/hpsc-dashboard in the user's home.
        author: Name shown in the credits (optional).
        author_url: Link for the author name, e.g. a LinkedIn profile (optional).
        repo_url: GitHub repository link shown in the footer (optional).
    """
    from . import dashboard
    from pathlib import Path
    out = Path(output_dir) if output_dir else Path.home() / "Documents" / "hpsc-dashboard"
    try:
        result = dashboard.build(out, out / "history.json", author=author, author_url=author_url, repo_url=repo_url)
    except HPSCError as e:
        return _fail(e)
    result["open_in_browser"] = "Double-click index.html in that folder."
    return result


def _scan(term: str | None, status: str | None, max_pages: int) -> tuple[list[dict], bool]:
    """Page through search results the way the API docs ask (id asc, limit 100)."""
    rows_all: list[dict] = []
    for p in range(max_pages):
        rows, _ = hpsc().search(term=term, status=status, orderby="id", order="asc",
                                limit=SCAN_PAGE_SIZE, offset=p * SCAN_PAGE_SIZE)
        rows_all.extend(rows)
        if len(rows) < SCAN_PAGE_SIZE:
            return rows_all, True
    return rows_all, False


@mcp.tool()
def recent_updates(status: str = "active_confirmed", limit: int = 25) -> dict:
    """Most recently updated reports, newest first. Good for a daily digest.

    Args:
        status: resolved, anticipated_shortage, active_confirmed, avoided_shortage or discontinued.
        limit: 1-100.
    """
    if status not in VALID_STATUSES:
        return {"error": f"status must be one of {sorted(VALID_STATUSES)}"}
    try:
        rows, total = hpsc().search(status=status, limit=max(1, min(limit, 100)))
    except HPSCError as e:
        return _fail(e)
    return {"total": total, "reports": annotate([summarize(r) for r in rows]), "note": DISCLAIMER}


@mcp.tool()
def lookup_drug(din: str | None = None, brand_name: str | None = None) -> dict:
    """Look up a drug in Health Canada's Drug Product Database (no account needed).

    Returns ingredients, strength, form, route, ATC class and marketing status.
    """
    try:
        if din:
            products = [p for p in [dpd().product_by_din(din)] if p]
        elif brand_name:
            products = dpd().products_by_brand(brand_name)[:10]
        else:
            return {"error": "Give a DIN or brand name."}
        results = [_describe_product(p) for p in products]
    except (HPSCError, httpx.HTTPError) as e:
        return _fail(e)
    return {"products": results, "note": DISCLAIMER}


@mcp.tool()
def find_alternatives(din: str, max_candidates: int = 15, check_shortages: bool = True) -> dict:
    """Find other marketed Canadian products with the SAME active ingredient(s),
    form and route as a DIN, and check whether each one is also in shortage.

    This is a same-ingredient (generic/brand interchange) search, not a
    therapeutic substitution recommendation.

    Args:
        din: DIN of the product that is short.
        max_candidates: Cap on alternative products to examine (1-40).
        check_shortages: Also check each alternative against HPSC.
    """
    max_candidates = max(1, min(max_candidates, 40))
    try:
        base = dpd().product_by_din(din)
        if not base:
            return {"error": f"DIN {din} not found in the Drug Product Database."}
        base_info = _describe_product(base)
        base_ings = {(_norm(i["name"]), _norm(i["strength"])) for i in base_info["ingredients"]}
        base_names = {n for n, _ in base_ings}
        if not base_names:
            return {"error": "No active ingredients listed for this product."}

        # Candidate drug codes: products containing the first ingredient.
        first = sorted(base_names)[0]
        codes = []
        for row in dpd().products_with_ingredient(first):
            code = row.get("drug_code")
            if code and code != base.get("drug_code") and code not in codes:
                codes.append(code)

        candidates = []
        for code in codes:
            if len(candidates) >= max_candidates:
                break
            prod = dpd().product(code)
            if not prod:
                continue
            info = _describe_product(prod)
            ings = {(_norm(i["name"]), _norm(i["strength"])) for i in info["ingredients"]}
            if {n for n, _ in ings} != base_names:
                continue
            if _norm(info["form"]) != _norm(base_info["form"]) or _norm(info["route"]) != _norm(base_info["route"]):
                continue
            if "marketed" not in str(info["status"]).lower():
                continue
            info["same_strength"] = ings == base_ings
            candidates.append(info)

        if check_shortages:
            for c in candidates:
                try:
                    rows, _ = hpsc().search(din=c["din"], limit=20)
                    current = [summarize(r) for r in rows
                               if not any(w in str(r.get("status", "")).lower()
                                          for w in ("resolved", "avoided", "reversed"))]
                    c["shortage_status"] = "IN SHORTAGE" if current else "no current report"
                    c["shortage_reports"] = current
                except HPSCError as e:
                    c["shortage_status"] = f"unknown ({e})"

        candidates.sort(key=lambda c: (c.get("shortage_status") != "no current report", not c["same_strength"]))
    except (HPSCError, httpx.HTTPError) as e:
        return _fail(e)

    return {
        "product": base_info,
        "alternatives": candidates,
        "note": DISCLAIMER + " Same-ingredient matches only; interchangeability, "
                "provincial formulary coverage and patient factors must be checked by a pharmacist.",
    }


def _norm(v: Any) -> str:
    return " ".join(str(v or "").upper().split())


def _describe_product(p: dict) -> dict:
    code = p.get("drug_code")
    ings = dpd().ingredients(code) if code else []
    forms = dpd().form(code) if code else []
    routes = dpd().route(code) if code else []
    statuses = dpd().status(code) if code else []
    atcs = dpd().atc(code) if code else []
    return {
        "din": p.get("drug_identification_number"),
        "brand_name": p.get("brand_name"),
        "company": p.get("company_name"),
        "drug_code": code,
        "ingredients": [
            {"name": i.get("ingredient_name"), "strength": f"{i.get('strength', '')} {i.get('strength_unit', '')}".strip()}
            for i in ings
        ],
        "form": ", ".join(f.get("pharmaceutical_form_name", "") for f in forms),
        "route": ", ".join(r.get("route_of_administration_name", "") for r in routes),
        "status": statuses[0].get("status") if statuses else None,
        "atc": atcs[0].get("tc_atc_number") if atcs else None,
        "atc_description": atcs[0].get("tc_atc") if atcs else None,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
