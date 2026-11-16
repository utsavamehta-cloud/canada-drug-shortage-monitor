"""
Read Health Canada's Drug Product Database (DPD) bulk extract of marketed products.

The dashboard uses it to answer one question for every product that is short:
which other marketed products have the same ingredient(s), strength, form and
route, and how many of those are also reported short?

Source (no account needed), listed on Health Canada's "What is the DPD Data
Extract?" page:
    https://www.canada.ca/content/dam/hc-sc/documents/services/drug-product-database/allfiles.zip

Field positions follow Health Canada's "Read Me File - DPD Data Extract".
Files are comma separated, values in double quotes, UTF-8.
"""

from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Iterator

import httpx

DEFAULT_EXTRACT_URL = (
    "https://www.canada.ca/content/dam/hc-sc/documents/services/drug-product-database/allfiles.zip"
)

# 0-based column positions, from the Read Me file.
DRUG_CODE = 0
DRUG_CLASS, DRUG_DIN, DRUG_BRAND = 2, 3, 4
ING_NAME, ING_STRENGTH, ING_UNIT, ING_DOSE_VALUE, ING_DOSE_UNIT = 2, 4, 5, 7, 9
FORM_NAME = 2
ROUTE_NAME = 2
STATUS_CURRENT, STATUS_NAME = 1, 2
COMP_NAME = 3


class DPDExtractError(RuntimeError):
    pass


def fetch(url: str | None = None, timeout: float = 180.0) -> bytes:
    """Download the marketed-products zip."""
    url = url or os.environ.get("DPD_EXTRACT_URL") or DEFAULT_EXTRACT_URL
    r = httpx.get(url, timeout=timeout, follow_redirects=True,
                  headers={"User-Agent": "canada-drug-shortage-monitor (open-source dashboard build)"})
    if r.status_code != 200:
        raise DPDExtractError(f"DPD extract download failed ({r.status_code}).")
    return r.content


def _rows(zf: zipfile.ZipFile, name: str) -> Iterator[list[str]]:
    """Rows of one file in the zip, matched by base name, whatever the folder or letter case."""
    member = next((n for n in zf.namelist() if Path(n).name.lower() == name), None)
    if member is None:
        return
    with zf.open(member) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace", newline="")
        for row in csv.reader(text):
            # Skip blank lines and a header row, if one is ever added.
            if not row or not row[0].strip().isdigit():
                continue
            yield row


def _cell(row: list[str], i: int) -> str:
    return row[i].strip() if len(row) > i else ""


def _num(v: str) -> str:
    """'500.0' and '500' are the same strength."""
    v = v.strip()
    if re.fullmatch(r"\d+\.\d+", v):
        v = v.rstrip("0").rstrip(".")
    return v


def base_ingredient(name: str) -> str:
    """'AMOXICILLIN (AMOXICILLIN TRIHYDRATE)' -> 'AMOXICILLIN'.

    The DPD writes the salt in brackets after the base the strength refers to.
    Grouping on the base keeps makers who use different salts together.
    """
    return " ".join(re.sub(r"\([^)]*\)", " ", name or "").upper().split())


def din8(v: str) -> str:
    digits = "".join(ch for ch in str(v or "") if ch.isdigit())
    return digits.zfill(8) if digits else ""


def _strength(row: list[str]) -> str:
    s = f"{_num(_cell(row, ING_STRENGTH))} {_cell(row, ING_UNIT).upper()}".strip()
    dose_unit = _cell(row, ING_DOSE_UNIT).upper()
    if dose_unit:
        dose_value = _num(_cell(row, ING_DOSE_VALUE))
        s += " / " + (f"{dose_value} {dose_unit}" if dose_value and dose_value != "1" else dose_unit)
    return s


def load(source: bytes | str | os.PathLike) -> list[dict]:
    """Marketed human products, one dict per DIN.

    Each has: din, brand, company, ingredients [(name, strength)], forms, routes,
    key (the same-product grouping key) and label (plain text for display).
    """
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise DPDExtractError("DPD extract is not a zip file.") from e

    drugs: dict[str, dict] = {}
    for row in _rows(zf, "drug.txt"):
        din = din8(_cell(row, DRUG_DIN))
        if din:
            drugs[row[DRUG_CODE].strip()] = {"din": din, "brand": _cell(row, DRUG_BRAND),
                                             "class": _cell(row, DRUG_CLASS).upper()}
    if not drugs:
        raise DPDExtractError("No products found in drug.txt of the DPD extract.")
    if any(d["class"] == "HUMAN" for d in drugs.values()):
        drugs = {c: d for c, d in drugs.items() if d["class"] == "HUMAN"}

    ings: dict[str, set] = defaultdict(set)
    for row in _rows(zf, "ingred.txt"):
        code = row[DRUG_CODE].strip()
        if code in drugs:
            name = base_ingredient(_cell(row, ING_NAME))
            if name:
                ings[code].add((name, _strength(row)))

    forms: dict[str, set] = defaultdict(set)
    for row in _rows(zf, "form.txt"):
        if row[DRUG_CODE].strip() in drugs and _cell(row, FORM_NAME):
            forms[row[DRUG_CODE].strip()].add(_cell(row, FORM_NAME).upper())

    routes: dict[str, set] = defaultdict(set)
    for row in _rows(zf, "route.txt"):
        if row[DRUG_CODE].strip() in drugs and _cell(row, ROUTE_NAME):
            routes[row[DRUG_CODE].strip()].add(_cell(row, ROUTE_NAME).upper())

    company: dict[str, str] = {}
    for row in _rows(zf, "comp.txt"):
        code = row[DRUG_CODE].strip()
        if code in drugs and code not in company:
            company[code] = _cell(row, COMP_NAME)

    # Current status. If the file has no usable status rows, trust that the
    # extract is the marketed one and keep everything.
    current: dict[str, str] = {}
    for row in _rows(zf, "status.txt"):
        code = row[DRUG_CODE].strip()
        if code in drugs and _cell(row, STATUS_CURRENT).upper() == "Y":
            current[code] = _cell(row, STATUS_NAME).upper()
    use_status = any("MARKETED" in s for s in current.values())

    products = []
    for code, d in drugs.items():
        if not ings.get(code):
            continue
        if use_status and "MARKETED" not in current.get(code, ""):
            continue
        ing = tuple(sorted(ings[code]))
        frm = tuple(sorted(forms.get(code, ())))
        rte = tuple(sorted(routes.get(code, ())))
        products.append({
            "din": d["din"], "brand": d["brand"], "company": company.get(code, ""),
            "ingredients": list(ing), "forms": list(frm), "routes": list(rte),
            "key": "|".join([";".join(f"{n}={s}" for n, s in ing), ",".join(frm), ",".join(rte)]),
            "label": " · ".join(p for p in [
                " + ".join(f"{n.title()} {s.lower()}".strip() for n, s in ing),
                ", ".join(f.capitalize() for f in frm),
                ", ".join(r.capitalize() for r in rte)] if p),
        })
    return products
