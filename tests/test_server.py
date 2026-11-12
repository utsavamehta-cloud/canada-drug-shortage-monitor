"""Offline tests using simulated API responses (no network needed)."""

import json

import httpx
import pytest

from hpsc_mcp import server as s

# --- simulated HPSC data ----------------------------------------------------
REPORTS = [
    {"id": 101, "din": "02242705", "company_name": "PHARMA A", "status": "active_confirmed",
     "type": {"label": "shortage"}, "en_drug_brand_name": "AMOXIL", "tier_3": False,
     "updated_date": "2026-11-01", "estimated_end_date": "2027-01-15",
     "shortage_reason": {"en_reason": "Demand increase for the drug."}},
    {"id": 102, "din": "02242705", "company_name": "PHARMA A", "status": "resolved",
     "type": {"label": "shortage"}, "en_drug_brand_name": "AMOXIL", "tier_3": False},
    {"id": 103, "din": "00000003", "company_name": "PHARMA B", "status": "anticipated_shortage",
     "type": {"label": "shortage"}, "en_drug_brand_name": "CRITICALDRUG", "tier_3": True},
    {"id": 104, "din": "00000004", "company_name": "PHARMA C", "status": "discontinued",
     "type": {"label": "discontinuance"}, "en_drug_brand_name": "OLDDRUG", "tier_3": False},
]


def hpsc_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/login"):
        body = dict(x.split("=") for x in request.content.decode().split("&"))
        if body.get("password") != "good":
            return httpx.Response(400, json={"error": {"en": "bad credentials", "fr": ""}})
        return httpx.Response(200, json={"user": {"id": 1}}, headers={"auth-token": "TOKEN"})
    if request.headers.get("auth-token") != "TOKEN":
        return httpx.Response(403, json={"error": {"en": "invalid token", "fr": ""}})
    if path.endswith("/search"):
        q = request.url.params
        rows = REPORTS
        if q.get("din"):
            rows = [r for r in rows if r["din"] == q["din"]]
        if q.get("term"):
            rows = [r for r in rows if q["term"].lower() in r["en_drug_brand_name"].lower()]
        if q.get("filter_status"):
            rows = [r for r in rows if r["status"] == q["filter_status"]]
        off, lim = int(q.get("offset", 0)), int(q.get("limit", 20))
        return httpx.Response(200, json={"data": rows[off:off + lim], "total": len(rows)})
    if "/shortages/" in path:
        rid = int(path.rsplit("/", 1)[1])
        for r in REPORTS:
            if r["id"] == rid:
                return httpx.Response(200, json=r)
        return httpx.Response(404, json={"error": {"en": "not found", "fr": ""}})
    return httpx.Response(404)


# --- simulated DPD data -----------------------------------------------------
PRODUCTS = {
    1: {"drug_code": 1, "drug_identification_number": "02242705", "brand_name": "AMOXIL", "company_name": "PHARMA A"},
    2: {"drug_code": 2, "drug_identification_number": "02000002", "brand_name": "GENERIC-AMOX", "company_name": "PHARMA X"},
    3: {"drug_code": 3, "drug_identification_number": "02000003", "brand_name": "AMOX-CLAV", "company_name": "PHARMA Y"},
    4: {"drug_code": 4, "drug_identification_number": "02000004", "brand_name": "AMOX-250", "company_name": "PHARMA Z"},
}
INGS = {
    1: [{"drug_code": 1, "ingredient_name": "AMOXICILLIN", "strength": "500", "strength_unit": "MG"}],
    2: [{"drug_code": 2, "ingredient_name": "AMOXICILLIN", "strength": "500", "strength_unit": "MG"}],
    3: [{"drug_code": 3, "ingredient_name": "AMOXICILLIN", "strength": "500", "strength_unit": "MG"},
        {"drug_code": 3, "ingredient_name": "CLAVULANIC ACID", "strength": "125", "strength_unit": "MG"}],
    4: [{"drug_code": 4, "ingredient_name": "AMOXICILLIN", "strength": "250", "strength_unit": "MG"}],
}


def dpd_handler(request: httpx.Request) -> httpx.Response:
    ep = request.url.path.strip("/").split("/")[-1]
    q = request.url.params
    if ep == "drugproduct":
        if q.get("din"):
            rows = [p for p in PRODUCTS.values() if p["drug_identification_number"] == q["din"]]
        else:
            rows = [PRODUCTS[int(q["id"])]] if int(q["id"]) in PRODUCTS else []
        return httpx.Response(200, content=json.dumps(rows))
    if ep == "activeingredient":
        if q.get("ingredientname"):
            name = q["ingredientname"].upper()
            rows = [i for lst in INGS.values() for i in lst if i["ingredient_name"] == name]
        else:
            rows = INGS.get(int(q["id"]), [])
        return httpx.Response(200, content=json.dumps(rows))
    code = int(q["id"])
    if ep == "form":
        return httpx.Response(200, content=json.dumps([{"drug_code": code, "pharmaceutical_form_name": "CAPSULE"}]))
    if ep == "route":
        return httpx.Response(200, content=json.dumps([{"drug_code": code, "route_of_administration_name": "ORAL"}]))
    if ep == "status":
        return httpx.Response(200, content=json.dumps([{"drug_code": code, "status": "MARKETED"}]))
    if ep == "therapeuticclass":
        return httpx.Response(200, content=json.dumps([{"drug_code": code, "tc_atc_number": "J01CA04", "tc_atc": "AMOXICILLIN"}]))
    return httpx.Response(404)


@pytest.fixture(autouse=True)
def clients():
    s._hpsc = s.HPSCClient("me@x.com", "good", "https://api.test/v1", transport=httpx.MockTransport(hpsc_handler))
    s._dpd = s.DPDClient("https://dpd.test/api/drug", transport=httpx.MockTransport(dpd_handler))
    yield
    s._hpsc = s._dpd = None


def test_search_by_term():
    out = s.search_shortages(query="amoxil")
    assert out["returned"] == 2
    assert out["reports"][0]["brand_name"] == "AMOXIL"
    assert out["reports"][0]["url"].endswith("/shortage/101")


def test_search_validates_status():
    assert "error" in s.search_shortages(query="x", status="bogus")


def test_bad_login_is_reported():
    s._hpsc = s.HPSCClient("me@x.com", "bad", "https://api.test/v1", transport=httpx.MockTransport(hpsc_handler))
    out = s.search_shortages(query="amoxil")
    assert "login failed" in out["error"]


def test_expired_token_relogs_in():
    s.hpsc()._token, s.hpsc()._token_time = "STALE", 9e12
    out = s.search_shortages(query="amoxil")
    assert out["returned"] == 2


def test_get_report():
    out = s.get_report("101")
    assert out["summary"]["reason"] == "Demand increase for the drug."
    assert "error" in s.get_report("999")


def test_watchlist_flags():
    out = s.check_watchlist(["02242705", "CRITICALDRUG", "NOTHINGHERE"])
    flags = {r["item"]: r["flag"] for r in out["results"]}
    assert flags == {"02242705": "AFFECTED", "CRITICALDRUG": "TIER 3", "NOTHINGHERE": "OK"}
    assert out["affected"] == 2 and out["tier_3"] == 1
    # resolved report is excluded
    assert [r["report_id"] for r in out["results"][0]["current_reports"]] == [101]


def test_watchlist_excluding_anticipated():
    out = s.check_watchlist(["CRITICALDRUG"], include_anticipated=False)
    assert out["results"][0]["flag"] == "OK"


def test_list_tier3():
    out = s.list_tier3()
    assert [r["report_id"] for r in out["tier_3_reports"]] == [103]


def test_discontinuation_type_and_url():
    out = s.recent_updates(status="discontinued")
    r = out["reports"][0]
    assert r["type"] == "discontinuation" and "/discontinuance/104" in r["url"]


def test_lookup_drug():
    out = s.lookup_drug(din="2242705")  # leading zero dropped on purpose
    p = out["products"][0]
    assert p["brand_name"] == "AMOXIL" and p["atc"] == "J01CA04" and p["form"] == "CAPSULE"


def test_find_alternatives():
    out = s.find_alternatives("02242705")
    alts = {a["din"]: a for a in out["alternatives"]}
    # combo product (amox-clav) excluded; 250 mg kept but marked not same strength
    assert set(alts) == {"02000002", "02000004"}
    assert alts["02000002"]["same_strength"] is True
    assert alts["02000004"]["same_strength"] is False
    assert alts["02000002"]["shortage_status"] == "no current report"
    # same-strength, not-short option is listed first
    assert out["alternatives"][0]["din"] == "02000002"


def test_missing_credentials():
    s._hpsc = s.HPSCClient("", "", "https://api.test/v1", transport=httpx.MockTransport(hpsc_handler))
    s._hpsc.email = s._hpsc.password = ""
    assert "HPSC_EMAIL" in s.search_shortages(query="x")["error"]


def test_tools_registered():
    import asyncio
    names = {t.name for t in asyncio.run(s.mcp.list_tools())}
    assert names == {"search_shortages", "get_report", "check_watchlist", "list_tier3",
                     "recent_updates", "lookup_drug", "find_alternatives", "data_quality_scan", "build_dashboard"}


def test_default_base_url_is_new_site():
    assert s.DEFAULT_HPSC_BASE == "https://healthproductshortages.ca/api/v1"


def test_list_tier3_pages_by_id_ascending():
    seen = []
    many = [{"id": 1000 + i, "din": f"{i:08d}", "status": "active_confirmed", "type": {"label": "shortage"},
             "en_drug_brand_name": f"DRUG{i}", "tier_3": i == 205} for i in range(250)]

    def handler(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"user": {}}, headers={"auth-token": "TOKEN"})
        q = request.url.params
        seen.append(dict(q))
        rows = many if q.get("filter_status") == "active_confirmed" else []
        off, lim = int(q["offset"]), int(q["limit"])
        return httpx.Response(200, json={"data": rows[off:off + lim], "total_pages": 3})

    s._hpsc = s.HPSCClient("me@x.com", "good", "https://api.test/v1", transport=httpx.MockTransport(handler))
    out = s.list_tier3()
    assert [r["report_id"] for r in out["tier_3_reports"]] == [1205]
    assert "warning" not in out
    active = [q for q in seen if q.get("filter_status") == "active_confirmed"]
    assert [q["offset"] for q in active] == ["0", "100", "200"]
    assert all(q["orderby"] == "id" and q["order"] == "asc" and q["limit"] == "100" for q in active)


def test_list_tier3_warns_when_capped():
    many = [{"id": i, "status": "active_confirmed", "type": {"label": "shortage"}, "tier_3": False}
            for i in range(300)]

    def handler(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"user": {}}, headers={"auth-token": "TOKEN"})
        q = request.url.params
        rows = many if q.get("filter_status") == "active_confirmed" else []
        off = int(q["offset"])
        return httpx.Response(200, json={"data": rows[off:off + 100]})

    s._hpsc = s.HPSCClient("me@x.com", "good", "https://api.test/v1", transport=httpx.MockTransport(handler))
    assert "warning" in s.list_tier3(max_pages=2)


def test_summarize_real_shape():
    raw = {"id": 273126, "type": {"id": 1, "label": "shortage"}, "status": "resolved", "tier_3": True,
           "en_drug_brand_name": "IFEX", "en_drug_common_name": "Ifosfamide", "en_ingredients": "IFOSFAMIDE",
           "drug_route": "INTRAVENOUS\r\nINTRAVENOUS", "shortage_reason": {"en_reason": "Shortage of an active ingredient."},
           "en_comments": "On 75% allocation.", "late_submission": False, "unknown_estimated_end_date": False,
           "marker_a1": False}
    s_ = s.summarize(raw)
    assert s_["tier_3"] is True and s_["route"] == "INTRAVENOUS" and s_["ingredients"] == "IFOSFAMIDE"
    assert s_["reason"] == "Shortage of an active ingredient." and s_["comments"] == "On 75% allocation."
    assert s.summarize({"id": 1, "tier_3": False, "tier_note": "x"})["tier_3"] is False


import datetime as _dt


def _rep(id_, din, status, end, upd, comments=None, company="CO A", late=False, unknown=False):
    return {"report_id": id_, "din": din, "status": status, "estimated_end": end, "updated": upd,
            "comments": comments, "company": company, "late_submission": late, "end_date_unknown": unknown,
            "tier_3": False, "brand_name": "X"}


TODAY = _dt.date(2026, 11, 12)


def test_quality_labels():
    q = lambda r: s.quality_flags(r, TODAY)
    assert q(_rep(1, "1", "active_confirmed", "2026-12-01", "2026-11-05"))["confidence"] == "Recently updated"
    assert q(_rep(1, "1", "active_confirmed", "2026-12-01", "2026-09-01"))["confidence"] == "Current per report"
    over = q(_rep(1, "1", "active_confirmed", "2026-10-01", "2026-09-01"))
    assert over["confidence"] == "Overdue for update" and over["end_date_passed_days"] == 42
    assert q(_rep(1, "1", "active_confirmed", "2022-12-01", "2022-12-10"))["confidence"] == "Probably outdated"
    assert q(_rep(1, "1", "active_confirmed", None, "2026-11-01", unknown=True))["confidence"] == "No end date given"
    assert q(_rep(1, "1", "resolved", "2020-01-01", "2020-01-01"))["confidence"] == "Closed report"
    assert q(_rep(1, "1", "active_confirmed", "2026-12-01", "2026-11-05", comments="100 BTL available"))["partial_supply"] == "100 BTL available"
    assert q(_rep(1, "1", "active_confirmed", "2026-12-01", "2026-11-05", comments="30 BLS available"))["partial_supply"]
    assert q(_rep(1, "1", "active_confirmed", "2026-12-01", "2026-11-05", comments="No stock"))["partial_supply"] is None


def test_quality_stats():
    reps = [
        _rep(1, "A", "active_confirmed", "2026-11-13", "2026-11-05", "100 BTL available", "APOTEX"),
        _rep(2, "A", "active_confirmed", "2026-11-13", "2026-07-30", company="APOTEX"),
        _rep(3, "A", "active_confirmed", "2026-11-13", "2026-06-18", company="APOTEX"),
        _rep(4, "B", "active_confirmed", "2022-12-31", "2022-12-31", company="SANIS", late=True),
        _rep(5, "C", "anticipated_shortage", None, "2026-11-01", company="JAMP", unknown=True),
    ]
    st = s.quality_stats(reps, TODAY)
    assert st["open_reports"] == 5
    assert st["duplicates"]["dins_with_multiple_open_reports"] == 1 and st["duplicates"]["extra_reports"] == 2
    assert st["past_estimated_end_date"]["count"] == 1
    assert st["past_estimated_end_date"]["how_late"]["over 1 year"] == 1
    assert st["not_updated_in"]["over_365_days"]["count"] == 1
    assert st["partial_supply_mentioned"]["count"] == 1 and st["late_submissions"]["count"] == 1
    assert st["no_end_date_given"]["count"] == 1
    assert st["companies_most_past_due"][0]["company"] == "SANIS"
    assert st["most_overdue_examples"][0]["report_id"] == 4


def test_search_results_carry_quality():
    out = s.search_shortages(query="amoxil")
    r = [x for x in out["reports"] if x["report_id"] == 101][0]
    assert "confidence" in r["quality"] and r["quality"]["open_reports_for_same_din"] == 1


def test_scan_tool_registered():
    import asyncio
    assert "data_quality_scan" in {t.name for t in asyncio.run(s.mcp.list_tools())}
