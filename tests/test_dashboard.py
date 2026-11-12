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
    res = dashboard.build(tmp_path / "site", hist, today=dt.date(2026, 11, 12), author="Test Author", repo_url="https://github.com/x/y")
    html = (tmp_path / "site" / "index.html").read_text()
    data = json.loads((tmp_path / "site" / "data.json").read_text())
    assert "/*__DATA__*/" not in html and "Test Author" in html
    assert data["kpi"]["open_reports"] == 400
    assert data["kpi"]["products"] == len({r["din"] for r in reps})  # merged by DIN
    assert sum(l["count"] for l in data["quality"]["labels"]) == 400
    assert len(json.loads(hist.read_text())) == 2
    assert any(r["nrep"] > 1 for r in [dict(r, nrep=len(r["reports"])) for r in data["rows"]])
    assert data["rows"][0]["tier3"] is True  # Tier 3 sorted first
    assert all(c["open"] >= 10 for c in data["companies"])
    s._hpsc = None
