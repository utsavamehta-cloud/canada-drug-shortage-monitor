"""End-to-end dashboard build with simulated API data (no network)."""
import datetime as dt
import json
import random

import httpx

from hpsc_mcp import dashboard, server as s

COMPANIES = ["APOTEX INC", "JAMP PHARMA CORPORATION", "TEVA CANADA LIMITED", "SANIS HEALTH INC",
             "SANDOZ CANADA INCORPORATED", "PHARMASCIENCE INC", "MINT PHARMACEUTICALS INC", "AURO PHARMA INC"]
ATCS = ["C09AA", "N06AB", "J01CA", "A02BC", "L01AA", "R03AC", "N03AX", "C10AA", "G03AA", "M01AE"]


def make_reports(n=400, seed=1):
    rnd = random.Random(seed)
    today = dt.date(2026, 11, 12)
    out = []
    for i in range(n):
        upd = today - dt.timedelta(days=rnd.choice([1, 3, 10, 40, 200, 500, 1400]))
        end = None if rnd.random() < 0.5 else (upd + dt.timedelta(days=rnd.choice([10, 60, 120]))).isoformat() + "T00:00:00-05:00"
        din = f"02{rnd.randint(100000, 100300):06d}"
        out.append({
            "id": 200000 + i, "din": din, "company_name": rnd.choice(COMPANIES),
            "status": "active_confirmed" if rnd.random() < 0.95 else "anticipated_shortage",
            "type": {"label": "shortage"}, "tier_3": rnd.random() < 0.02,
            "en_drug_brand_name": f"DRUG-{i % 300}", "en_drug_common_name": f"INGREDIENT {i % 300}",
            "drug_strength": "10MG", "drug_dosage_form": "TABLET", "drug_route": "ORAL",
            "shortage_reason": {"en_reason": "Disruption of the manufacture of the drug."},
            "en_comments": "100 BTL available" if rnd.random() < 0.02 else None,
            "actual_start_date": (upd - dt.timedelta(days=5)).isoformat() + "T00:00:00-05:00",
            "estimated_end_date": end, "unknown_estimated_end_date": end is None,
            "updated_date": upd.isoformat() + "T12:00:00-05:00", "created_date": (upd - dt.timedelta(days=rnd.choice([0, 2, 30]))).isoformat() + "T12:00:00-05:00",
            "atc_number": rnd.choice(ATCS), "late_submission": rnd.random() < 0.12,
        })
    return out


def handler_factory(reports):
    resolved = [dict(r, id=r["id"] + 9000, status="resolved") for r in reports[:30]]

    def handler(req):
        if req.url.path.endswith("/login"):
            return httpx.Response(200, json={"user": {}}, headers={"auth-token": "T"})
        q = req.url.params
        st = q.get("filter_status")
        rows = resolved if st == "resolved" else [r for r in reports if r["status"] == st]
        off, lim = int(q.get("offset", 0)), int(q.get("limit", 20))
        return httpx.Response(200, json={"data": rows[off:off + lim]})
    return handler


def test_build_dashboard(tmp_path, monkeypatch):
    reps = make_reports()
    s._hpsc = s.HPSCClient("a@b.c", "p", "https://api.test/v1", transport=httpx.MockTransport(handler_factory(reps)))
    monkeypatch.setattr(dashboard.dt, "date", type("D", (dt.date,), {"today": staticmethod(lambda: dt.date(2026, 11, 12))}))
    hist = tmp_path / "h.json"
    hist.write_text(json.dumps([{"date": "2026-11-05", "open": 380, "outdated_pct": 49.0, "no_end_pct": 50, "dup_pct": 9, "tier3": 7}]))
    res = dashboard.build(tmp_path / "site", hist, today=dt.date(2026, 11, 12), author="Test Author", repo_url="https://github.com/x/y", use_dpd=False)
    assert res["same_ingredient_data"] is False
    html = (tmp_path / "site" / "index.html").read_text()
    data = json.loads((tmp_path / "site" / "data.json").read_text())
    assert "/*__DATA__*/" not in html and "Test Author" in html
    assert data["kpi"]["open_reports"] == 400
    assert data["kpi"]["products"] == len({r["din"] for r in reps})  # merged by DIN
    assert sum(l["count"] for l in data["quality"]["labels"]) == 400
    h = json.loads(hist.read_text())
    assert len(h) == 2 and all("no_update_12m_pct" in p and "outdated_pct" not in p for p in h)
    assert sum(b["count"] for b in data["quality"]["age_buckets"]) == 400
    assert "outdated" not in html.lower().replace("outdated_pct", "")
    assert any(r["nrep"] > 1 for r in [dict(r, nrep=len(r["reports"])) for r in data["rows"]])
    assert data["rows"][0]["tier3"] is True  # Tier 3 sorted first
    assert all(c["open"] >= 10 for c in data["companies"])
    s._hpsc = None
    assert data["dpd"] is None and data["groups"] == {}  # built without the DPD extract
    assert all(r["impact"] == "unknown" for r in data["rows"])


# ---------------------------------------------------------------------------
# Same-ingredient groups from the DPD extract
# ---------------------------------------------------------------------------

def _csv(rows):
    return "\n".join(",".join(f'"{c}"' for c in r) for r in rows) + "\n"


def make_extract():
    """A tiny DPD extract in the column layout of Health Canada's Read Me file."""
    import io
    import zipfile
    # code, din, brand, company, ingredient, strength, form, status
    prods = [
        (1, "02000001", "APO-AMOXI", "APOTEX INC", "AMOXICILLIN (AMOXICILLIN TRIHYDRATE)", "500", "CAPSULE", "MARKETED"),
        (2, "02000002", "TEVA-AMOXI", "TEVA CANADA LIMITED", "AMOXICILLIN (AMOXICILLIN TRIHYDRATE)", "500.0", "CAPSULE", "MARKETED"),
        (3, "02000003", "JAMP-AMOXI", "JAMP PHARMA CORPORATION", "AMOXICILLIN", "500", "CAPSULE", "MARKETED"),
        (4, "02000004", "APO-AMOXI 250", "APOTEX INC", "AMOXICILLIN (AMOXICILLIN TRIHYDRATE)", "250", "CAPSULE", "MARKETED"),
        (5, "02000005", "SOLO-DRUG", "SANDOZ CANADA INCORPORATED", "RAREMYCIN", "10", "TABLET", "MARKETED"),
        (6, "02000006", "OLD-AMOXI", "SANIS HEALTH INC", "AMOXICILLIN", "500", "CAPSULE", "CANCELLED POST MARKET"),
        (7, "02000007", "DOG-AMOXI", "VETCO", "AMOXICILLIN", "500", "CAPSULE", "MARKETED"),
        (8, "2000008", "PMS-AMOXI", "PHARMASCIENCE INC", "AMOXICILLIN", "500", "CAPSULE", "MARKETED"),
    ]
    files = {"drug.txt": [], "ingred.txt": [], "form.txt": [], "route.txt": [], "status.txt": [], "comp.txt": []}
    for code, din, brand, co, ing, strength, form, status in prods:
        cls = "Veterinary" if code == 7 else "Human"
        files["drug.txt"].append([code, "", cls, din, brand, "", "N", "", "1", "02-NOV-2026", "0100000001", "", brand, ""])
        files["ingred.txt"].append([code, 100, ing, "I", strength, "MG", "", "", "N", "", "", ing, "MG", "", ""])
        files["form.txt"].append([code, 10, form, form])
        files["route.txt"].append([code, 20, "ORAL", "ORALE"])
        files["status.txt"].append([code, "N", "APPROVED", "01-JAN-2010", "APPROUVE", "", ""])
        files["status.txt"].append([code, "Y", status, "01-JAN-2012", status, "", ""])
        files["comp.txt"].append([code, "ABC", 1000 + code, co, "DIN OWNER", "Y", "N", "N", "N", "", "1 Main St", "Toronto", "Ontario", "Canada", "M1M 1M1", "", "Ontario", "Canada"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, rows in files.items():
            z.writestr(name, _csv(rows))
    return buf.getvalue()


def test_dpd_extract_groups_same_ingredient_strength_form():
    from hpsc_mcp import dpd_extract
    prods = dpd_extract.load(make_extract())
    by_din = {p["din"]: p for p in prods}
    # Cancelled and veterinary products are left out; a 7-digit DIN is padded.
    assert set(by_din) == {"02000001", "02000002", "02000003", "02000004", "02000005", "02000008"}
    # Salt in brackets and "500.0" vs "500" do not split the group; a different strength does.
    k = by_din["02000001"]["key"]
    assert by_din["02000002"]["key"] == k == by_din["02000003"]["key"] == by_din["02000008"]["key"]
    assert by_din["02000004"]["key"] != k
    assert by_din["02000001"]["label"] == "Amoxicillin 500 mg · Capsule · Oral"
    assert by_din["02000001"]["company"] == "APOTEX INC"


def _open_report(i, din, status="active_confirmed"):
    return s.summarize({
        "id": 300000 + i, "din": din, "company_name": "X", "status": status, "type": {"label": "shortage"},
        "en_drug_brand_name": f"B{i}", "en_drug_common_name": "AMOXICILLIN", "drug_strength": "500MG",
        "drug_dosage_form": "CAPSULE", "drug_route": "ORAL", "updated_date": "2026-11-10T12:00:00-05:00",
        "created_date": "2026-11-01T12:00:00-05:00", "estimated_end_date": "2026-12-20T00:00:00-05:00",
    })


def test_impact_label_and_sort():
    from hpsc_mcp import dpd_extract
    prods = dpd_extract.load(make_extract())
    today = dt.date(2026, 11, 12)
    # 1 of the 4 amoxicillin 500 mg products short; the only raremycin product short;
    # the only 250 mg product short; one DIN that is not in the marketed list.
    reports = [_open_report(1, "02000001"), _open_report(2, "02000005"),
               _open_report(3, "02000004"), _open_report(4, "02999999")]
    payload, _ = dashboard.build_payload(reports, [], today, products=prods)
    rows = {r["din"]: r for r in payload["rows"]}
    assert (rows["02000001"]["impact"], rows["02000001"]["gn"], rows["02000001"]["gs"]) == ("some", 4, 1)
    assert (rows["02000005"]["impact"], rows["02000005"]["gn"]) == ("none_left", 1)
    assert rows["02000004"]["impact"] == "none_left"
    assert rows["02999999"]["impact"] == "unknown" and rows["02999999"]["g"] is None
    # Fewest options first, not-matched last.
    assert [r["impact"] for r in payload["rows"]] == ["none_left", "none_left", "some", "unknown"]
    g = payload["groups"][rows["02000001"]["g"]]
    assert g["n"] == 4 and g["s"] == 1 and g["impact"] == "some"
    assert g["m"][-1][0] == "02000001"  # products with no open report are listed first
    assert payload["kpi"]["none_left"] == 2 and payload["kpi"]["matched"] == 3
    assert payload["dpd"] == {"marketed_products": 6}
    assert g["check"] is False and rows["02000001"]["check"] is False  # oral capsule: no presentation caution

    # Three of four short -> "most"; all four -> "none_left".
    more = reports + [_open_report(5, "02000002"), _open_report(6, "02000003", "anticipated_shortage")]
    rows = {r["din"]: r for r in dashboard.build_payload(more, [], today, products=prods)[0]["rows"]}
    assert (rows["02000001"]["impact"], rows["02000001"]["gs"]) == ("most", 3)
    rows = {r["din"]: r for r in dashboard.build_payload(more + [_open_report(7, "02000008")], [], today, products=prods)[0]["rows"]}
    assert rows["02000001"]["impact"] == "none_left"


def test_build_survives_missing_dpd(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no network")
    monkeypatch.setattr(dashboard.dpd_extract, "fetch", boom)
    assert dashboard.collect_products() is None


def test_presentation_check_rule():
    from hpsc_mcp.dpd_extract import needs_presentation_check as chk
    assert chk(["SUBCUTANEOUS"], []) and chk(["INTRAVENOUS"], []) and chk(["INHALATION"], []) and chk(["TRANSDERMAL"], [])
    assert chk(["ORAL"], ["Schedule D"])           # biologic, whatever the route
    assert not chk(["ORAL"], ["Prescription"]) and not chk(["TOPICAL"], []) and not chk(["OPHTHALMIC"], [])
