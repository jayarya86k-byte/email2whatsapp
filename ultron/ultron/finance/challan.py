"""Tax challan generation (major/minor heads, bank list, print layout).

Produces the challan data set plus a printable HTML/PDF that matches the
challan counter layout, so it can be taken to any authorised bank branch.
Portal submission stays a separate, explicit, human-confirmed step.

Head codes change over time -- verify against the CBDT/e-filing portal
before paying. The table below is the commonly used mapping and is
deliberately data, not code, so it can be corrected in one place.
"""

from __future__ import annotations

import datetime as _dt
import html
from typing import Any, Dict, List, Optional

CHALLAN_TYPES: Dict[str, Dict[str, Any]] = {
    "income_tax": {
        "label": "Income Tax / Advance Income Tax",
        "major_head": "2001",
        "minor_head": "001",
        "sub_head": "03",
        "sub_major": "000",
        "challan_code": "2811",
        "section": "195 / 234B / 234C",
    },
    "self_assessment": {
        "label": "Income Tax (Self Assessment u/s 140(1))",
        "major_head": "2001", "minor_head": "001", "sub_head": "03", "sub_major": "000",
        "challan_code": "2811", "section": "u/s 140(1)",
    },
    "tds": {
        "label": "TDS / TCS (Income Tax Act, 1961)",
        "major_head": "2002", "minor_head": "001", "sub_head": "00", "sub_major": "000",
        "challan_code": "2811", "section": "u/s 192-196",
    },
    "cess": {
        "label": "Health & Education Cess",
        "major_head": "2005", "minor_head": "001", "sub_head": "00", "sub_major": "000",
        "challan_code": "2811", "section": "Finance Act",
    },
    "gst": {
        "label": "GST (CGST / SGST / IGST / Cess)",
        "major_head": "2002", "minor_head": "007", "sub_head": "00", "sub_major": "000",
        "challan_code": "997", "section": "CGST Act 2017",
    },
    "tds_gst": {
        "label": "TDS under CGST Act",
        "major_head": "2002", "minor_head": "007", "sub_head": "01", "sub_major": "000",
        "challan_code": "997", "section": "u/s 51 CGST Act",
    },
    "refund": {
        "label": "Refund (interest/penalty handled by the portal)",
        "major_head": "2001", "minor_head": "001", "sub_head": "04", "sub_major": "000",
        "challan_code": "2811", "section": "u/s 237",
    },
    "sec234f": {
        "label": "Interest u/s 234F (tax collected at source by e-commerce)",
        "major_head": "2001", "minor_head": "001", "sub_head": "03", "sub_major": "000",
        "challan_code": "2811", "section": "u/s 234F",
    },
}

BANKS = [
    "State Bank of India", "HDFC Bank", "ICICI Bank", "Axis Bank",
    "Punjab National Bank", "Bank of Baroda", "Kotak Mahindra Bank",
    "Canara Bank", "Union Bank of India", "Central Bank of India",
    "Bank of India", "IDFC First Bank",
]

AMOUNT_IN_WORDS_LIMIT = 19


def amount_in_words(amount: float) -> str:
    """Indian numbering: crore / lakh / thousand."""
    ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
            "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
            "Seventeen", "Eighteen", "Nineteen"]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy",
            "Eighty", "Ninety"]

    def two_digit(number: int) -> str:
        if number < 20:
            return ones[number]
        return f"{tens[number // 10]}" + (f" {ones[number % 10]}" if number % 10 else "")

    def three_digit(number: int) -> str:
        hundreds, rest = divmod(number, 100)
        parts = []
        if hundreds:
            parts.append(f"{ones[hundreds]} Hundred")
        if rest:
            parts.append(two_digit(rest))
        return " ".join(parts)

    rupees = int(amount)
    paise = int(round((amount - rupees) * 100))
    if rupees == 0 and paise == 0:
        return "Zero Rupees Only"
    groups = []
    crore, rest = divmod(rupees, 10_000_000)
    lakh, rest = divmod(rest, 100_000)
    thousand, rest = divmod(rest, 1_000)
    if crore:
        groups.append(f"{three_digit(crore)} Crore")
    if lakh:
        groups.append(f"{two_digit(lakh)} Lakh")
    if thousand:
        groups.append(f"{two_digit(thousand)} Thousand")
    if rest:
        groups.append(three_digit(rest))
    text = " ".join(groups) + " Rupees"
    if paise:
        text += f" and {two_digit(paise)} Paise"
    return text + " Only"


def build(
    challan_type: str,
    amount: float,
    *,
    pan: str = "",
    name: str = "",
    assessment_year: str = "",
    financial_year: str = "",
    tax_period: str = "",
    bank: str = "State Bank of India",
    branch: str = "",
    address: str = "",
    pincode: str = "",
    remarks: str = "",
    email: str = "",
    mobile: str = "",
    taxpayer_type: str = "Individual",
    split: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    key = str(challan_type).lower()
    if key not in CHALLAN_TYPES:
        return {"ok": False, "error": f"unknown challan type '{challan_type}'",
                "available": sorted(CHALLAN_TYPES)}
    spec = CHALLAN_TYPES[key]
    amount = round(float(amount) + 1e-9, 2)
    if amount <= 0:
        return {"ok": False, "error": "challan amount must be greater than zero"}
    today = _dt.date.today()
    payload: Dict[str, Any] = {
        "ok": True,
        "challan": {
            "challan_type": key,
            "challan_label": spec["label"],
            "amount": amount,
            "amount_in_words": amount_in_words(amount),
            "currency": "INR",
            "major_head": spec["major_head"],
            "minor_head": spec["minor_head"],
            "sub_major_head": spec["sub_major"],
            "sub_head": spec["sub_head"],
            "challan_code": spec["challan_code"],
            "legal_section": spec["section"],
            "assessment_year": assessment_year,
            "financial_year": financial_year,
            "tax_period": tax_period,
            "taxpayer": {"name": name, "pan": pan, "type": taxpayer_type,
                         "address": address, "pincode": pincode,
                         "email": email, "mobile": mobile},
            "bank": {"name": bank, "branch": branch},
            "remarks": remarks,
            "generated_on": today.isoformat(),
            "valid_note": "Payment validity follows CBDT rules; re-verify heads on the portal.",
        },
        "split": {k: round(float(v), 2) for k, v in (split or {}).items()} or None,
        "payment": {
            "mode": "Cheque / cash / NEFT-RTGS at an authorised bank branch",
            "online_alternative": "Pay via e-filing portal 'Pay Tax' or net banking to designated account",
            "steps": [
                "Print this challan or note the challan number printed by the bank.",
                "Deposit at the tax collection counter of an authorised bank.",
                "Collect the signed challan receipt ( Challan No. / CIN / BSR code ).",
                "Verify the payment reflected in Form 26AS / 26Q within 3-4 days.",
            ],
        },
        "audit_fields": ["pan", "assessment_year", "tax_period", "amount", "challan_code"],
        "warning": "Head codes and challan codes are data-driven and must be verified "
                   "against the current CBDT/GST portal before payment.",
    }
    if not pan:
        payload["warnings"] = payload.get("warnings", []) + [
            "PAN missing: banks will reject the challan without a valid PAN."]
    return payload


def challan_lines(data: Dict[str, Any], widths: int = 96) -> List[str]:
    """Monospaced print layout, also used for the PDF writer."""
    challan = data.get("challan", {})
    taxpayer = challan.get("taxpayer", {})
    bank = challan.get("bank", {})
    rule = "-" * widths
    rows = [
        challan.get("challan_label", "").upper().center(widths).rstrip(),
        rule,
        f"{'Taxpayer':<22}: {taxpayer.get('name', '')} ({taxpayer.get('type', '')})",
        f"{'PAN':<22}: {taxpayer.get('pan', '')}",
        f"{'Address':<22}: {taxpayer.get('address', '')} {taxpayer.get('pincode', '')}".rstrip(),
        f"{'Assessment Year':<22}: {challan.get('assessment_year', '')}",
        f"{'Financial Year':<22}: {challan.get('financial_year', '')}",
        f"{'Tax Period':<22}: {challan.get('tax_period', '')}",
        rule,
        f"{'Major Head':<22}: {challan.get('major_head', '')}",
        f"{'Minor Head':<22}: {challan.get('minor_head', '')}",
        f"{'Sub-Major Head':<22}: {challan.get('sub_major_head', '')}",
        f"{'Sub Head':<22}: {challan.get('sub_head', '')}",
        f"{'Challan Code':<22}: {challan.get('challan_code', '')}",
        f"{'Section':<22}: {challan.get('legal_section', '')}",
        rule,
        f"{'Amount (Rs)':<22}: {challan.get('amount', 0):,.2f}",
        f"{'In words':<22}: {challan.get('amount_in_words', '')}",
        f"{'Bank / Branch':<22}: {bank.get('name', '')} / {bank.get('branch', '')}".rstrip(),
        f"{'Generated on':<22}: {challan.get('generated_on', '')}",
    ]
    if challan.get("remarks"):
        rows += [rule, f"Remarks: {challan['remarks']}"]
    rows += [rule, "(payable at the tax collection counter of an authorised bank branch)"]
    return rows


def to_html(data: Dict[str, Any]) -> str:
    challan = data.get("challan", {})
    taxpayer = challan.get("taxpayer", {})
    bank = challan.get("bank", {})

    def row(label: str, value: Any) -> str:
        return (f"<tr><th>{html.escape(label)}</th>"
                f"<td>{html.escape(str(value))}</td></tr>")

    body = [
        "<h2>Tax Challan</h2>",
        "<p class='muted'>" + html.escape(challan.get("challan_label", "")) + "</p>",
        "<table>",
        row("Taxpayer", f"{taxpayer.get('name','')} ({taxpayer.get('type','')})"),
        row("PAN", taxpayer.get("pan", "")),
        row("Address", f"{taxpayer.get('address','')} {taxpayer.get('pincode','')}"),
        row("Assessment Year", challan.get("assessment_year", "")),
        row("Financial Year", challan.get("financial_year", "")),
        row("Tax Period", challan.get("tax_period", "")),
        row("Major Head", challan.get("major_head", "")),
        row("Minor Head", challan.get("minor_head", "")),
        row("Sub-Major Head", challan.get("sub_major_head", "")),
        row("Sub Head", challan.get("sub_head", "")),
        row("Challan Code", challan.get("challan_code", "")),
        row("Section", challan.get("legal_section", "")),
        row("Amount", f"Rs {challan.get('amount', 0):,.2f}"),
        row("In words", challan.get("amount_in_words", "")),
        row("Bank / Branch", f"{bank.get('name','')} / {bank.get('branch','')}"),
        row("Generated on", challan.get("generated_on", "")),
        "</table>",
    ]
    if challan.get("remarks"):
        body.append(f"<p><strong>Remarks:</strong> {html.escape(challan['remarks'])}</p>")
    body.append("<h3>Payment steps</h3><ol>" +
                "".join(f"<li>{html.escape(s)}</li>"
                        for s in data.get("payment", {}).get("steps", [])) + "</ol>")
    body.append(f"<p class='warn'>{html.escape(data.get('warning',''))}</p>")
    if data.get("warnings"):
        body.append("<ul class='warn'>" +
                    "".join(f"<li>{html.escape(w)}</li>" for w in data["warnings"]) + "</ul>")
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Tax Challan</title>"
        "<style>body{font-family:system-ui;max-width:760px;margin:32px auto;padding:0 20px}"
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #d4d4d8;padding:6px 10px;"
        "text-align:left;width:38%}th{background:#f4f4f5}.warn{color:#b45309;font-size:13px}"
        ".muted{color:#57534e}@media print{body{margin:0}}</style></head><body>"
        + "".join(body) + "</body></html>"
    )
