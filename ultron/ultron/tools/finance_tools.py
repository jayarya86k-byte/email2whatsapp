"""Finance tools exposed to the agent: GST, income tax, challans, audit packs.

Every tool is read/compute oriented. Anything that would touch a government
portal is a separate, explicit tool with risk >= 0.9 and is never reachable
without a human confirmation.
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..finance import challan as challan_mod
from ..finance import gst as gst_mod
from ..finance import incometax as it_mod
from ..tools.doc_tools import render_html, write_pdf
from .registry import ToolRegistry

INCOME_FIELDS = {
    "salary": "salary", "house_property": "house_property", "business": "business",
    "capital_gains": "capital_gains", "other_sources": "other_sources",
    "foreign_assets": "foreign_assets", "exempt_80c": "exempt_80c",
    "exempt_80d": "exempt_80d", "exempt_other": "exempt_other",
    "deduction_chapter_vi_a": "deduction_chapter_vi_a", "tds_deducted": "tds_deducted",
    "advance_tax_paid": "advance_tax_paid",
}


def _income(data: Dict[str, Any]) -> it_mod.Income:
    kwargs = {field_name: float(data.get(key, 0) or 0)
              for key, field_name in INCOME_FIELDS.items()}
    return it_mod.Income(**kwargs)


def _taxpayer(data: Dict[str, Any], config: Any) -> it_mod.Taxpayer:
    return it_mod.Taxpayer(
        pan=str(data.get("pan") or config.get("finance.payer_pan", "") or ""),
        name=str(data.get("name", "")),
        date_of_birth=str(data.get("date_of_birth", "")),
        is_senior_citizen=data.get("is_senior_citizen"),
        is_resident=bool(data.get("is_resident", True)),
        is_salaried=bool(data.get("is_salaried", bool(data.get("salary", 0)))),
        has_business_income=bool(data.get("has_business_income", False)),
    )


def register(registry: ToolRegistry, policy: Any, audit: Any, config: Any) -> None:
    default_ay = str(config.get("finance.default_fy", "2025-26"))

    # ---------------- GST ----------------
    def gst_invoice(
        items: List[Dict[str, Any]],
        seller_state: str,
        place_of_supply: str = "",
        invoice_number: str = "",
        invoice_date: str = "",
        reverse_charge: bool = False,
    ) -> Dict[str, Any]:
        """Compute a GST invoice: line taxes, HSN summary, e-invoice payload."""
        prepared = []
        for item in items or []:
            entry = dict(item)
            entry.setdefault("invoice_number", invoice_number)
            entry.setdefault("invoice_date", invoice_date)
            prepared.append(entry)
        data = gst_mod.compute_invoice(
            prepared, seller_state,
            place_of_supply=place_of_supply or None,
            reverse_charge_override=True if reverse_charge else None,
        )
        audit.record("finance.gst_invoice", risk=0.1, capability="fs.read",
                     detail={"seller_state": seller_state, "lines": len(prepared)})
        return data

    def gst_returns(
        outward: List[Dict[str, Any]],
        inward: Dict[str, Any],
        period: str,
        reverse_charge: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """Draft GSTR-1 (from invoices) and GSTR-3B (from outward + inward figures)."""
        gstr1 = gst_mod.gstr1_draft(outward, period=period,
                                    taxpayer=config.get("finance.payer_pan", ""))
        gstr3b = gst_mod.gstr3b_draft(period, config.get("finance.payer_pan", ""),
                                      _totals(outward), inward or {}, reverse_charge or {})
        audit.record("finance.gst_returns", risk=0.2, capability="fs.read",
                     detail={"period": period, "invoices": len(outward or [])})
        return {"ok": True, "gstr1": gstr1, "gstr3b": gstr3b}

    def gst_invoice_document(
        path: str, items: List[Dict[str, Any]], seller_state: str,
        seller_name: str = "", seller_gstin: str = "", buyer_name: str = "",
        buyer_gstin: str = "", place_of_supply: str = "", invoice_number: str = "1",
        invoice_date: str = "", reverse_charge: bool = False,
    ) -> Dict[str, Any]:
        """Render a GST invoice as a print-ready HTML file."""
        computed = gst_invoice(items, seller_state, place_of_supply, invoice_number,
                               invoice_date, reverse_charge)
        if not computed.get("ok"):
            return computed
        body = [f"<h1>Tax Invoice</h1><p><strong>{seller_name}</strong> ({seller_gstin or 'unregistered'})</p>",
                f"<p>Invoice {invoice_number} &middot; {invoice_date} &middot; Place of supply: "
                f"{computed['supplier_state_name']}</p>",
                f"<p>Bill to: <strong>{buyer_name}</strong> ({buyer_gstin or 'B2C'})</p>",
                "<table><tr><th>#</th><th>Description</th><th>HSN</th><th>Qty</th>"
                "<th>Taxable</th><th>CGST</th><th>SGST</th><th>IGST</th><th>Amount</th></tr>"]
        for line in computed["lines"]:
            body.append(
                f"<tr><td>{line['einvoice']['sl_no']}</td><td>{line['description']}</td>"
                f"<td>{line['hsn']}</td><td>{line['quantity']}</td>"
                f"<td>{line['taxable_value']:,.2f}</td><td>{line['cgst']:,.2f}</td>"
                f"<td>{line['sgst']:,.2f}</td><td>{line['igst']:,.2f}</td>"
                f"<td>{line['amount']:,.2f}</td></tr>")
        totals = computed["totals"]
        body.append(
            f"<tr class='totals'><td colspan='4'>Total</td>"
            f"<td>{totals['taxable']:,.2f}</td><td>{totals['cgst']:,.2f}</td>"
            f"<td>{totals['sgst']:,.2f}</td><td>{totals['igst']:,.2f}</td>"
            f"<td>{totals['invoice']:,.2f}</td></tr></table>")
        target = Path(os.path.expanduser(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_html(f"Invoice {invoice_number}", "".join(body)), "utf-8")
        audit.record("finance.gst_invoice_doc", risk=0.3, capability="fs.write",
                     detail={"path": str(target)})
        return {"ok": True, "path": str(target), "totals": totals,
                "supply_type": computed["supply_type"]}

    # ---------------- Income tax ----------------
    def income_tax_compute(
        income: Dict[str, Any],
        taxpayer_data: Dict[str, Any] = None,
        assessment_year: str = "",
        regime: str = "new",
    ) -> Dict[str, Any]:
        """Compute income tax for an assessment year under both regimes."""
        ay = assessment_year or _ay_from_fy(default_ay)
        person = _taxpayer(taxpayer_data or {}, config)
        result = it_mod.compute(person, _income(income), ay=ay, regime=regime)
        result["advance_tax_schedule"] = it_mod.advance_tax_schedule(
            result["total_tax"],
            float((income or {}).get("tds_deducted", 0) or 0) +
            float((income or {}).get("advance_tax_paid", 0) or 0),
            ay,
        )
        audit.record("finance.income_tax", risk=0.15, capability="fs.read",
                     detail={"ay": ay, "regime": regime})
        return result

    def income_tax_document(
        path: str,
        income: Dict[str, Any],
        taxpayer_data: Dict[str, Any] = None,
        assessment_year: str = "",
        regime: str = "new",
    ) -> Dict[str, Any]:
        """Write the full computation as a readable HTML working paper."""
        computed = income_tax_compute(income, taxpayer_data, assessment_year, regime)
        if not computed.get("ok"):
            return computed
        rows = "".join(
            f"<tr><th>{label}</th><td>{_fmt(value)}</td></tr>"
            for label, value in [
                ("Gross total income", computed["income_heads"]["gross_total_income"]),
                ("Standard deduction", computed["standard_deduction"]),
                ("Chapter VI-A deductions", computed["deductions"]["total"]),
                ("Total income", computed["total_income"]),
                ("Taxable income", computed["taxable_income"]),
                ("Tax before rebate", computed["tax_before_rebate"]),
                ("Rebate u/s 87A", computed["rebate_87A"]["amount"]),
                ("Surcharge", computed["surcharge"]["amount"]),
                ("Health & education cess", computed["cess_health_education"]),
                ("Total tax", computed["total_tax"]),
                ("Less TDS", computed["less_tds"]),
                ("Less advance tax", computed["less_advance_tax"]),
                ("Tax payable", computed["tax_payable"]),
                ("Refund", computed["refund"]),
            ])
        slabs = "".join(
            f"<tr><td>{_fmt(b['from'])} - {b['to'] if b['to'] else 'above'}</td>"
            f"<td>{b['rate'] * 100:g}%</td><td>{_fmt(b['taxable'])}</td><td>{_fmt(b['tax'])}</td></tr>"
            for b in computed["slab_breakdown"])
        instal = "".join(
            f"<tr><td>{i['installment']}</td><td>{i['due_date']}</td>"
            f"<td>{i['percentage']}%</td><td>{_fmt(i['amount_due'])}</td></tr>"
            for i in computed["advance_tax_schedule"]["installments"])
        comparison = ""
        if computed.get("comparison"):
            c = computed["comparison"]
            comparison = (f"<h2>Regime comparison</h2><p>New: {_fmt(c['new']['tax_payable'])} | "
                          f"Old: {_fmt(c['old']['tax_payable'])} | Better: <strong>{c['better']}</strong> "
                          f"(difference {_fmt(c['difference'])})</p>")
        body = (f"<h1>Income Tax Computation</h1><p>{html.escape(str(computed['assessment_year']))} | "
                f"regime: {computed['regime']}</p><table>{rows}</table>{comparison}"
                f"<h2>Slab working</h2><table><tr><th>Band</th><th>Rate</th><th>Taxable</th>"
                f"<th>Tax</th></tr>{slabs}</table>"
                f"<h2>Advance tax</h2><table><tr><th>Instalment</th><th>Due</th><th>%</th>"
                f"<th>Amount</th></tr>{instal}</table>"
                f"<p class='warn'>{html.escape('Working paper only. Verify before filing.')}</p>")
        target = Path(os.path.expanduser(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_html(f"Income Tax {computed['assessment_year']}", body), "utf-8")
        audit.record("finance.income_tax_doc", risk=0.3, capability="fs.write",
                     detail={"path": str(target)})
        return {"ok": True, "path": str(target), "tax_payable": computed["tax_payable"],
                "taxable_income": computed["taxable_income"]}

    def tds_estimate(section: str, amounts: List[float]) -> Dict[str, Any]:
        """Estimate TDS for a section (194/194A/194C/194I/194J ...)."""
        return it_mod.tds_estimate(section, amounts)

    # ---------------- Challans ----------------
    def generate_challan(
        challan_type: str,
        amount: float,
        pan: str = "",
        name: str = "",
        assessment_year: str = "",
        financial_year: str = "",
        tax_period: str = "",
        bank: str = "",
        branch: str = "",
        address: str = "",
        remarks: str = "",
        output_path: str = "",
    ) -> Dict[str, Any]:
        """Build a tax challan and optionally write it as HTML/PDF."""
        data = challan_mod.build(
            challan_type, amount,
            pan=pan or str(config.get("finance.payer_pan", "")),
            name=name, assessment_year=assessment_year, financial_year=financial_year,
            tax_period=tax_period,
            bank=bank or str(config.get("finance.challan_bank", "State Bank of India")),
            branch=branch, address=address, remarks=remarks,
        )
        if not data.get("ok"):
            return data
        if output_path:
            target = Path(os.path.expanduser(output_path))
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.suffix.lower() == ".pdf":
                lines = challan_mod.challan_lines(data)
                write_pdf(str(target), lines, title=data["challan"]["challan_label"])
            else:
                if target.suffix.lower() != ".html":
                    target = target.with_suffix(".html")
                target.write_text(challan_mod.to_html(data), "utf-8")
            data["output_path"] = str(target)
        audit.record("finance.challan", risk=0.45, capability="fs.write",
                     detail={"type": challan_type, "amount": amount,
                             "output": data.get("output_path")})
        return data

    def filing_checklist(fy: str = "") -> Dict[str, Any]:
        """Return the compliance calendar and the data Ultron needs from you."""
        fy = fy or default_ay
        ay = _ay_from_fy(fy)
        dates = it_mod.DUE_DATES.get(fy, {})
        return {
            "ok": True,
            "financial_year": fy,
            "assessment_year": ay,
            "deadlines": {
                "GST GSTR-1 monthly": "11th of the following month",
                "GST GSTR-1/3B quarterly (QRMP)": "25th after each quarter end",
                "GST GSTR-9 annual": "31 December following the FY",
                "TDS/TCS deposit": "7th of the following month (April: 30th)",
                "TDS return 26Q": "31 July / 31 October / 31 January / 30 April",
                "Advance tax": dates,
                "Income tax return (non-audit)": dates.get("p4", ""),
                "Tax audit report 3CA/3CB": dates.get("p4", ""),
                "Form 16 / 16A": "on or before 15 June following the FY",
            },
            "ultron_needs": [
                "PAN and date of birth of every assessee",
                "Salary or commission statements (Form 16 from prior year)",
                "Bank statements (savings, fixed deposit, PPF) for interest income",
                "Rent receipts and landlord PAN for HRA / 24(b) and 194IB",
                "80C proofs (EPF, ELPP, life insurance, principal on housing loan)",
                "80D proof, 80CCD(1B), 80C(1B), NPS contribution",
                "Capital gains statements (equity MTM and STT-exempt if eligible)",
                "Business books, bank statements and depreciation schedule",
                "GST returns filed and GSTR-2B for ITC reconciliation",
                "TDS certificates (26AS) for every deductor",
            ],
            "how_ultron_works": [
                "Ultron computes and cross-checks; it never submits to a portal silently.",
                "Portal submission is a separate, confirmed action requiring your credentials from the vault.",
                "Every computation is written to the tamper-evident audit log.",
            ],
        }

    registry.function_tool(gst_invoice, risk=0.1, capability="fs.read", category="finance")
    registry.function_tool(gst_returns, risk=0.2, capability="fs.read", category="finance")
    registry.function_tool(gst_invoice_document, risk=0.3, capability="fs.write",
                           category="finance", confirm_args=["path"])
    registry.function_tool(income_tax_compute, risk=0.15, capability="fs.read", category="finance")
    registry.function_tool(income_tax_document, risk=0.3, capability="fs.write",
                           category="finance", confirm_args=["path"])
    registry.function_tool(tds_estimate, risk=0.05, capability="fs.read", category="finance")
    registry.function_tool(generate_challan, risk=0.45, capability="fs.write",
                           category="finance", confirm_args=["challan_type", "amount", "output_path"])
    registry.function_tool(filing_checklist, risk=0.0, capability="fs.read", category="finance")


def _totals(invoices: List[Dict[str, Any]]) -> Dict[str, Any]:
    totals = {"taxable_value": 0.0, "cgst": 0.0, "sgst": 0.0, "igst": 0.0, "cess": 0.0}
    for invoice in invoices or []:
        for key in totals:
            totals[key] = round(totals[key] + float(invoice.get(key, 0) or 0), 2)
    return totals


def _ay_from_fy(fy: str) -> str:
    try:
        start = int(str(fy)[:4])
    except (TypeError, ValueError):
        return "2026-27"
    return f"{start + 1}-{(start + 1) % 100:02d}"


def _fmt(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)

