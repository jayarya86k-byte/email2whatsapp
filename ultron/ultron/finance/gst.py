"""GST engine (India).

Covers invoice-level tax computation (CGST/SGST/IGST, RCM, discounts,
e-invoicing fields), HSN summary, and GSTR-1 / GSTR-3B draft generation.
No network calls: the portal filing step is a separate, explicit action.
"""

from __future__ import annotations

import json
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram",
    "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "25": "Daman and Diu", "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep",
    "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman and Nicobar Islands",
    "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh", "97": "Other Territory",
}

# Rate -> (CGST, SGST/UTGST, IGST) slab, kept explicit for auditability.
SLABS = {
    0.0: (0.0, 0.0, 0.0),
    0.25: (0.0, 0.0, 0.25),
    0.5: (0.0, 0.0, 0.5),
    1.0: (0.5, 0.5, 1.0),
    1.5: (0.75, 0.75, 1.5),
    5.0: (2.5, 2.5, 5.0),
    6.0: (3.0, 3.0, 6.0),
    7.5: (3.75, 3.75, 7.5),
    12.0: (6.0, 6.0, 12.0),
    18.0: (9.0, 9.0, 18.0),
    28.0: (14.0, 14.0, 28.0),
}

RCM_RATES = {5.0: 1.0, 12.0: 2.5, 18.0: 5.0, 28.0: 18.0}  # (IGST on taxable value)


def state_code(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.isdigit():
        return text.zfill(2)
    for code, name in STATE_CODES.items():
        if name.lower() == text.lower():
            return code
    raise ValueError(f"unknown place of supply: {value!r}")


def state_name(code: str) -> str:
    return STATE_CODES.get(str(code).zfill(2), "Unknown")


@dataclass
class LineItem:
    description: str
    hsn: str
    quantity: float = 1.0
    unit_price: float = 0.0      # per unit, exclusive of tax
    rate: float = 0.0            # GST rate in percent (0, 0.25, 3, 5, 12, 18, 28 ...)
    discount_percent: float = 0.0
    taxable_value: Optional[float] = None   # overrides quantity x unit_price - discount
    reverse_charge: bool = False
    cess_percent: float = 0.0
    # e-invoicing fields
    hsn_digits: int = 6
    unit: str = "NOS"
    gstin: str = ""
    invoice_number: str = ""
    invoice_date: str = ""
    place_of_supply: str = ""

    def base_value(self) -> float:
        if self.taxable_value is not None:
            return round(float(self.taxable_value), 2)
        gross = float(self.quantity) * float(self.unit_price)
        return round(gross - gross * float(self.discount_percent) / 100.0, 2)


def round2(value: float) -> float:
    return round(float(value) + 1e-9, 2)


def compute_invoice(
    items: List[Dict[str, Any]],
    seller_state: str,
    *,
    place_of_supply: Optional[str] = None,
    reverse_charge_override: Optional[bool] = None,
    inter_state_supplies: Optional[bool] = None,
) -> Dict[str, Any]:
    """Compute a GST invoice.

    `items` are dicts matching LineItem. The place of supply defaults to the
    first item's place_of_supply, else the seller state. Set
    `inter_state_supplies` to force IGST when the JSON style/relation differs.
    """
    seller = state_code(seller_state)
    lines: List[Dict[str, Any]] = []
    hsn_summary: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    totals = {"taxable": 0.0, "cgst": 0.0, "sgst": 0.0, "igst": 0.0,
              "cess": 0.0, "round_off": 0.0, "invoice": 0.0}
    rcm = {"taxable": 0.0, "igst": 0.0, "cgst": 0.0, "sgst": 0.0}

    for raw in items or []:
        item = LineItem(**{k: v for k, v in raw.items()
                           if k in LineItem.__dataclass_fields__})
        pos = state_code(place_of_supply or item.place_of_supply or seller)
        base = item.base_value()
        slab_key = min(SLABS, key=lambda s: abs(s - round(float(item.rate), 2)))
        slab_rate = slab_key
        cgst_pct, sgst_pct, igst_pct = SLABS[slab_rate]

        if inter_state_supplies is None:
            inter = pos != seller
        else:
            inter = bool(inter_state_supplies)
        reverse = item.reverse_charge if reverse_charge_override is None else bool(reverse_charge_override)

        if reverse:
            taxable = base
            rcm_rate = RCM_RATES.get(slab_rate, slab_rate / 2 if slab_rate >= 1.5 else 0.0)
            rcm_igst = round2(taxable * rcm_rate / 100.0)
            cgst = sgst = igst = 0.0
            cess = round2(taxable * item.cess_percent / 100.0)
            rcm["taxable"] = round2(rcm["taxable"] + taxable)
            rcm["igst"] = round2(rcm["igst"] + rcm_igst)
        elif inter:
            igst = round2(base * igst_pct / 100.0)
            cgst = sgst = 0.0
            cess = round2((igst) * item.cess_percent / 100.0)
        else:
            cgst = round2(base * cgst_pct / 100.0)
            sgst = round2(base * sgst_pct / 100.0)
            igst = 0.0
            cess = round2((cgst + sgst) * item.cess_percent / 100.0)

        line_total = round2(base + cgst + sgst + igst + cess)
        totals["taxable"] += base
        totals["cgst"] += cgst
        totals["sgst"] += sgst
        totals["igst"] += igst
        totals["cess"] += cess
        totals["invoice"] += line_total

        hsn = item.hsn or "NA"
        lines.append({
            "description": item.description,
            "hsn": hsn,
            "hsn_digits": item.hsn_digits,
            "unit": item.unit,
            "quantity": item.quantity,
            "rate": item.rate,
            "discount_percent": item.discount_percent,
            "taxable_value": base,
            "rate_display": _rate_label(slab_rate),
            "cgst": cgst, "sgst": sgst, "igst": igst, "cess": cess,
            "reverse_charge": reverse,
            "place_of_supply": pos,
            "place_of_supply_name": state_name(pos),
            "amount": line_total,
            "einvoice": {
                "sl_no": len(lines) + 1,
                "invoice_number": item.invoice_number,
                "invoice_date": item.invoice_date,
                "gstin": item.gstin,
                "unit": item.unit,
                "rate": slab_rate,
                "quantity": item.quantity,
                "total_value": line_total,
            },
        })

    # aggregate HSN totals from finished lines
    for line in lines:
        key = line["hsn"]
        bucket = hsn_summary.setdefault(
            key, {"hsn": key, "description": line["description"], "qty": 0.0,
                  "taxable": 0.0, "cgst": 0.0, "sgst": 0.0, "igst": 0.0, "cess": 0.0,
                  "rate": line["rate_display"], "rate_pct": _rate_pct(line["rate_display"]),
                  "b2b": True}
        )
        bucket["qty"] = round2(bucket["qty"] + line["quantity"])
        for name, line_key in (("taxable", "taxable_value"), ("cgst", "cgst"),
                               ("sgst", "sgst"), ("igst", "igst"), ("cess", "cess")):
            bucket[name] = round2(bucket[name] + line[line_key])
        bucket["b2b"] = bucket["b2b"] and bool(line["einvoice"]["gstin"])

    for key in totals:
        totals[key] = round2(totals[key])
    totals["round_off"] = round2(round(totals["invoice"]) - totals["invoice"])
    totals["invoice"] = round2(round(totals["invoice"]))
    for key in rcm:
        rcm[key] = round2(rcm[key])

    return {
        "ok": True,
        "supplier_state": seller,
        "supplier_state_name": state_name(seller),
        "supply_type": "inter-state (IGST)" if totals["igst"] else "intra-state (CGST+SGST)",
        "lines": lines,
        "hsn_summary": list(hsn_summary.values()),
        "totals": totals,
        "reverse_charge": rcm,
        "einvoice_ready": all(len(line["einvoice"]["gstin"]) == 15 for line in lines) and bool(lines),
    }


def _rate_pct(label: str) -> float:
    return float(str(label).rstrip("%") or 0)


def _rate_label(rate: float) -> str:
    if rate == 0:
        return "0%"
    if rate in (0.25, 1.5, 7.5):
        return f"{rate}%"
    return f"{rate:g}%"


def gstr1_draft(
    invoices: List[Dict[str, Any]],
    *,
    period: str = "",
    taxpayer: str = "",
) -> Dict[str, Any]:
    """Group invoices into GSTR-1 tables (B2B, B2C, credit/debit notes, HSN)."""
    b2b: List[Dict[str, Any]] = []
    b2c = {"9A": 0.0, "9B": 0.0}
    hsn: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for invoice in invoices or []:
        pos = state_code(invoice.get("place_of_supply", ""))
        inter = invoice.get("cgst", 0) == 0 and invoice.get("igst", 0) > 0
        if invoice.get("gstin"):
            b2b.append({
                "gstin": invoice["gstin"],
                "receiver_name": invoice.get("receiver_name", ""),
                "invoice_number": invoice.get("invoice_number", ""),
                "invoice_date": invoice.get("invoice_date", ""),
                "invoice_value": round2(invoice.get("invoice_value", 0.0)),
                "place_of_supply": f"{pos}-{state_name(pos)}",
                "rate": invoice.get("rate_display", "18%"),
                "taxable_value": round2(invoice.get("taxable_value", 0.0)),
                "cgst": round2(invoice.get("cgst", 0.0)),
                "sgst": round2(invoice.get("sgst", 0.0)),
                "igst": round2(invoice.get("igst", 0.0)),
                "cess": round2(invoice.get("cess", 0.0)),
                "invoice_type": invoice.get("invoice_type", "R"),
                "e_invoice": invoice.get("einvoice", {}).get("irn", ""),
                "e_waybill": invoice.get("ewaybill", ""),
            })
        else:
            key = "9B" if inter else "9A"
            b2c[key] = round2(b2c[key] + invoice.get("invoice_value", 0.0))
        for row in invoice.get("hsn_summary", []) or []:
            key = row.get("hsn", "NA")
            bucket = hsn.setdefault(key, {"hsn": key, "description": row.get("description", ""),
                                          "qty": 0.0, "taxable": 0.0, "cgst": 0.0,
                                          "sgst": 0.0, "igst": 0.0, "cess": 0.0,
                                          "rate": row.get("rate", "18%"), "b2b": row.get("b2b", False)})
            bucket["qty"] = round2(bucket["qty"] + row.get("qty", 0.0))
            for name in ("taxable", "cgst", "sgst", "igst", "cess"):
                bucket[name] = round2(bucket[name] + row.get(name, 0.0))
            bucket["b2b"] = bucket["b2b"] or bool(row.get("b2b", False))
    return {"ok": True, "form": "GSTR-1", "period": period, "gstin": taxpayer,
            "tables": {"b2b": b2b, "b2c_consolidated": b2c, "hsn_summary": list(hsn.values())}}


def gstr3b_draft(
    period: str,
    taxpayer: str,
    outward: Dict[str, Any],
    inward: Dict[str, Any],
    rcm: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble a GSTR-3B draft from inward/outward supply figures."""
    def pick(section: Dict[str, Any], *names: str) -> float:
        for name in names:
            if name in section:
                return round2(section[name])
        return 0.0

    outward = outward or {}
    inward = inward or {}
    rcm = rcm or {}

    o_taxable = pick(outward, "3.1(a)", "taxable_value", "3.1a")
    o_cgst = pick(outward, "3.1(b)", "cgst")
    o_sgst = pick(outward, "3.1(c)", "sgst")
    o_igst = pick(outward, "3.1(d)", "igst")
    o_cess = pick(outward, "3.1(e)", "cess")

    i_taxable = pick(inward, "4A(1)", "4A1", "taxable_value")
    i_cgst = pick(inward, "4A(2)", "4A2")
    i_sgst = pick(inward, "4A(3)", "4A3")
    i_igst = pick(inward, "4A(4)", "4A4")
    i_cess = pick(inward, "4A(5)", "4A5")

    itc = {
        "import_goods": pick(inward, "4A(6)", "4A6"),
        "import_services": pick(inward, "4A(7)", "4A7"),
        "rcm": round2(rcm.get("igst", 0.0)),
        "input_service_4B1": pick(inward, "4B1"),
        "input_service_4B2": pick(inward, "4B2"),
        "other_ITC": pick(inward, "4C", "4D", "4F", "5A", "6A", "6B", "6C", "6D", "7"),
    }
    total_itc = round2(sum(itc.values()))
    setoff = {
        "igst": round2(min(o_igst, i_igst + itc["import_goods"] + itc["import_services"] + itc["rcm"])),
        "cgst": round2(min(o_cgst, i_cgst + itc["input_service_4B1"] + itc["input_service_4B2"] + itc["rcm"] / 2)),
    }
    setoff["sgst"] = setoff["cgst"]
    payable = {
        "tax_payable_cgst": round2(max(0.0, o_cgst - setoff["cgst"])),
        "tax_payable_sgst": round2(max(0.0, o_sgst - setoff["sgst"])),
        "tax_payable_igst": round2(max(0.0, o_igst - setoff["igst"])),
    }
    return {
        "ok": True,
        "form": "GSTR-3B",
        "period": period,
        "gstin": taxpayer,
        "3.1_outward_supplies": {"taxable": o_taxable, "cgst": o_cgst, "sgst": o_sgst,
                                 "igst": o_igst, "cess": o_cess},
        "4A_inward_supplies": {"taxable": i_taxable, "cgst": i_cgst, "sgst": i_sgst,
                               "igst": i_igst, "cess": i_cess},
        "4_ITC_available": itc,
        "6.1_ITC_utilised": setoff,
        "9_payable": payable,
        "9_total_tax_payable": round2(sum(payable.values())),
        "notes": [
            "ITC is capped at the tax paid; unutilised credit carries forward.",
            "Reverse-charge ITC is available only to an eligible registered person.",
        ],
    }


def to_json(data: Dict[str, Any]) -> str:
    return json.dumps(data, indent=2, default=str)
