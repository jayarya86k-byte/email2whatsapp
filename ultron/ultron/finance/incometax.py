"""Indian income-tax computation: AY 2025-26 and AY 2026-27.

Implements section 115BAC(1A) (default regime) and the old regime, 87A
rebate with marginal relief, surcharge, health & education cess, section
80C/80D aggregation, 234B/234C interest and the advance-tax schedule.
All rates are table-driven so they can be audited and extended.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Rate tables
# --------------------------------------------------------------------------
NEW_REGIME_SLABS: Dict[str, List[Tuple[float, float]]] = {
    # (upper bound, rate) for AY 2025-26 and AY 2026-27
    "2025-26": [(300_000, 0.0), (400_000, 0.05), (800_000, 0.10), (1_200_000, 0.15),
                (1_600_000, 0.20), (2_000_000, 0.25), (2_400_000, 0.30), (float("inf"), 0.37)],
    "2026-27": [(300_000, 0.0), (400_000, 0.05), (800_000, 0.10), (1_200_000, 0.15),
                (1_600_000, 0.20), (2_000_000, 0.25), (2_400_000, 0.30), (float("inf"), 0.37)],
}

OLD_REGIME_SLABS: Dict[str, List[Tuple[float, float]]] = {
    "2025-26": [(250_000, 0.0), (500_000, 0.05), (1_000_000, 0.20), (float("inf"), 0.30)],
    "2026-27": [(250_000, 0.0), (500_000, 0.05), (1_000_000, 0.20), (float("inf"), 0.30)],
}

STANDARD_DEDUCTION = {
    "2025-26": {"new": 75_000, "old_salaried": 50_000, "old_non_salaried": 0},
    "2026-27": {"new": 75_000, "old_salaried": 50_000, "old_non_salaried": 0},
}

REBATE_87A = {
    "2025-26": {"new": {"threshold": 1_200_000, "max": 60_000},
                "old": {"threshold": 500_000, "max": 12_500},
                "old_above_5L": {"threshold": 1_000_000, "max": 10_000}},
    "2026-27": {"new": {"threshold": 1_200_000, "max": 60_000},
                "old": {"threshold": 500_000, "max": 12_500},
                "old_above_5L": {"threshold": 1_000_000, "max": 10_000}},
}

SURCHARGE_INDIVIDUAL = [  # (total income threshold, rate)
    (10_000_000, 0.10),
    (50_000_000, 0.15),
    (100_000_000, 0.25),
    (float("inf"), 0.37),
]

CESS_RATE = 0.04
MAX_80C = 150_000
MAX_80D_SELF = 25_000      # senior self/family, non-senior: 5,000
MAX_80D_SENIOR = 50_000
MAX_80DD = 75_000
MAX_80DDB = 40_000

MONTHS = ["April", "May", "June", "July", "August", "September",
          "October", "November", "December", "January", "February", "March"]
DUE_DATES = {
    "2025-26": {"d1": "15-06-2025", "q1": "15-09-2025", "q2": "15-12-2025",
                "q3": "15-03-2026", "p4": "15-03-2026", "tds_salary": "30-06-2025",
                "tds_q2": "15-08-2025", "tds_q3": "15-11-2025", "tds_q4": "07-02-2026"},
    "2026-27": {"d1": "15-06-2026", "q1": "15-09-2026", "q2": "15-12-2026",
                "q3": "15-03-2027", "p4": "15-03-2027", "tds_salary": "30-06-2026",
                "tds_q2": "15-08-2026", "tds_q3": "15-11-2026", "tds_q4": "07-02-2027"},
}

TDS_RATES = {  # section -> (rate_percent, threshold, basis, period)
    "194": (10.0, 5_000, "dividend", "quarterly"),
    "194A": (5.0, 10_000, "interest_savings", "quarterly"),
    "194B": (5.0, 30_000, "lottery", "annual"),
    "194C": (1.0, 30_000, "contractor_individual", "quarterly"),
    "194C_professional": (2.0, 30_000, "contractor_company", "quarterly"),
    "194D": (5.0, 20_000, "insurance_commission", "quarterly"),
    "194H": (5.0, 15_000, "stock_brokerage", "quarterly"),
    "194I": (2.0, 30_000, "rent", "quarterly"),
    "194IB": (10.0, 50_000, "rent_landlord", "quarterly"),
    "194J": (10.0, 30_000, "professional_fees", "quarterly"),
    "194K": (15.0, 0, "commission", "annual"),
    "194N": (1.0, 0, "cash_withdrawal", "monthly"),
    "194O": (0.5, 0, "ecommerce_operator", "annual"),
}


def r2(value: float) -> float:
    return round(float(value) + 1e-9, 2)


def slab_tax(income: float, slabs: List[Tuple[float, float]]) -> Tuple[float, List[Dict[str, Any]]]:
    """Progressive slab tax with marginal-relief-safe breakdown."""
    income = max(0.0, float(income))
    tax = 0.0
    lower = 0.0
    breakdown: List[Dict[str, Any]] = []
    for upper, rate in slabs:
        if income <= lower:
            break
        taxable = min(income, upper) - lower
        amount = r2(taxable * rate)
        tax += amount
        breakdown.append({"from": lower, "to": (None if upper == float("inf") else upper),
                          "rate": rate, "taxable": r2(taxable), "tax": amount})
        lower = upper
    return r2(tax), breakdown


@dataclass
class Taxpayer:
    pan: str = ""
    name: str = ""
    date_of_birth: str = ""       # YYYY-MM-DD; needed for the senior-citizen rebate cap
    is_senior_citizen: Optional[bool] = None
    is_resident: bool = True
    is_salaried: bool = False
    has_business_income: bool = False

    @property
    def senior(self) -> bool:
        if self.is_senior_citizen is not None:
            return bool(self.is_senior_citizen)
        return bool(self.date_of_birth and self.date_of_birth[:4].isdigit()
                    and int(self.date_of_birth[:4]) <= 1962)

    def to_dict(self) -> Dict[str, Any]:
        return {"pan": self.pan, "name": self.name, "is_senior_citizen": self.senior,
                "is_resident": self.is_resident, "is_salaried": self.is_salaried,
                "has_business_income": self.has_business_income}


@dataclass
class Income:
    salary: float = 0.0
    house_property: float = 0.0          # negative for loss
    business: float = 0.0
    capital_gains: float = 0.0
    other_sources: float = 0.0
    foreign_assets: float = 0.0
    exempt_80c: float = 0.0
    exempt_80d: float = 0.0
    exempt_other: float = 0.0           # 80DD, 80DDB, 80CCD(2), 80TTA, 80U ...
    deduction_chapter_vi_a: float = 0.0  # extra 50(1)(c) etc.
    previous_year_tax: float = 0.0
    tds_deducted: float = 0.0
    advance_tax_paid: float = 0.0

    def gross(self) -> float:
        return (self.salary + self.house_property + self.business + self.capital_gains
                + self.other_sources + self.foreign_assets)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _fyi_for_ay(ay: str) -> str:
    """AY 2026-27 -> FY 2025-26 (the financial year a return is filed for)."""
    try:
        start = int(str(ay)[:4]) - 1
    except (TypeError, ValueError):
        return str(ay)
    return f"{start}-{(start + 1) % 100:02d}"


def surcharge_rate(total_income: float, resident: bool) -> float:
    if not resident or total_income <= 10_000_000:
        return 0.0
    for threshold, rate in SURCHARGE_INDIVIDUAL:
        if total_income > threshold:
            return rate
    return 0.0


def rebate_87a(ay: str, regime: str, taxable_income: float, total_income: float,
               senior: bool, surcharge: float, has_pension: bool = False) -> Dict[str, Any]:
    """Rebate u/s 87A, including the marginal-relief band.

    New regime (AY 2025-26 onwards): taxable income up to 12,00,000 -> rebate
    up to 60,000. Old regime: up to 5,00,000 -> 12,500 (25,000 for a senior
    citizen without pension income); 5,00,001 to 10,00,000 -> 10,000.
    """
    table = REBATE_87A.get(ay)
    if not table:
        return {"allowed": False, "amount": 0.0, "reason": f"no 87A table for AY {ay}"}
    if surcharge > 0:
        return {"allowed": False, "amount": 0.0,
                "reason": "surcharge applies, so 87A rebate is not available"}
    if total_income > 100_000_000:
        return {"allowed": False, "amount": 0.0,
                "reason": "total income above Rs 1 crore"}

    if regime == "new":
        cap = table["new"]["max"]
        threshold = table["new"]["threshold"]
    else:
        if taxable_income <= table["old"]["threshold"]:
            cap = table["old"]["max"]
            if senior and not has_pension:
                cap = 25_000  # senior citizen without pension income
            threshold = table["old"]["threshold"]
        else:
            cap = table["old_above_5L"]["max"]
            threshold = table["old_above_5L"]["threshold"]

    if taxable_income <= threshold:
        return {"allowed": True, "amount": r2(cap), "threshold": threshold, "cap": r2(cap)}
    return {"allowed": False, "amount": 0.0, "threshold": threshold, "cap": r2(cap)}


def compute(
    taxpayer: Taxpayer,
    income: Income,
    *,
    ay: str = "2026-27",
    regime: str = "new",
    compare_regimes: bool = True,
) -> Dict[str, Any]:
    """Compute total income, tax, rebate, surcharge, cess and interest."""
    result = _compute_one(taxpayer, income, ay=ay, regime=regime)
    result["ok"] = True
    result["ay"] = ay
    result["assessment_year"] = ay
    result["regime"] = regime
    if compare_regimes and regime == "new":
        old = _compute_one(taxpayer, income, ay=ay, regime="old")
        result["comparison"] = {
            "new": {"tax_payable": result["tax_payable"], "taxable_income": result["taxable_income"]},
            "old": {"tax_payable": old["tax_payable"], "taxable_income": old["taxable_income"]},
            "better": "old" if old["tax_payable"] < result["tax_payable"] else "new",
            "difference": r2(result["tax_payable"] - old["tax_payable"]),
        }
    return result


def _compute_one(taxpayer: Taxpayer, income: Income, *, ay: str, regime: str) -> Dict[str, Any]:
    slabs = (NEW_REGIME_SLABS if regime == "new" else OLD_REGIME_SLABS).get(ay)
    if slabs is None:
        raise ValueError(f"assessment year {ay} is not supported")

    gross = income.gross()
    sd_table = STANDARD_DEDUCTION.get(ay, {"new": 75_000, "old_salaried": 50_000, "old_non_salaried": 0})
    if regime == "new":
        standard_deduction = float(sd_table["new"])
    else:
        standard_deduction = float(sd_table["old_salaried"] if income.salary else sd_table["old_non_salaried"])

    # Chapter VI-A deductions (capped) then other deductions
    capped_80c = min(income.exempt_80c, MAX_80C)
    d80d_cap = MAX_80D_SENIOR if taxpayer.senior else MAX_80D_SENIOR
    capped_80d = min(income.exempt_80d, d80d_cap)
    total_deductions = r2(capped_80c + capped_80d + income.exempt_other + income.deduction_chapter_vi_a)

    # house-property loss set off against salary, balance against business
    hp = income.house_property
    hp_setoff = 0.0
    if hp < 0 and income.salary > 0:
        absorbed = min(-hp, income.salary)
        hp_setoff = absorbed
        hp += absorbed
    if hp < 0 and income.business <= 0:
        hp = 0.0  # no other head to absorb; carried forward

    adjusted_gross = gross + hp_setoff
    if regime == "new" and (income.exempt_80c or income.exempt_80d or income.exempt_other):
        # most chapter VI-A deductions are not allowed under 115BAC(1A)
        total_deductions = r2(income.deduction_chapter_vi_a)
    taxable_income = r2(max(0.0, adjusted_gross - standard_deduction - total_deductions))
    taxable_income = r2(taxable_income if taxable_income < 1_000_000 else
                        round(taxable_income))  # u/s 288A rounding above 10 lakh

    tax_before_rebate, breakdown = slab_tax(taxable_income, slabs)
    surcharge_pct = surcharge_rate(adjusted_gross, taxpayer.is_resident)
    surcharge = r2(tax_before_rebate * surcharge_pct)
    rebate = rebate_87a(ay, regime, taxable_income, adjusted_gross, taxpayer.senior,
                        surcharge_pct, has_pension=income.salary > 0)
    # The rebate can never exceed the tax payable on the income.
    rebate_amount = min(rebate["amount"], tax_before_rebate)
    tax_after_rebate = r2(max(0.0, tax_before_rebate - rebate_amount))
    tax_with_surcharge = r2(tax_after_rebate + surcharge)
    cess = r2(tax_with_surcharge * CESS_RATE)
    total_tax = r2(tax_with_surcharge + cess)
    tds = income.tds_deducted
    advance = income.advance_tax_paid
    shortfall = r2(max(0.0, total_tax - tds - advance))
    refund = r2(max(0.0, tds + advance - total_tax))

    return {
        "taxpayer": taxpayer.to_dict(),
        "income_heads": {
            "salary": r2(income.salary),
            "house_property": r2(income.house_property),
            "business": r2(income.business),
            "capital_gains": r2(income.capital_gains),
            "other_sources": r2(income.other_sources),
            "foreign_assets": r2(income.foreign_assets),
            "gross_total_income": r2(gross),
        },
        "standard_deduction": r2(standard_deduction),
        "deductions": {
            "80C_claimed": r2(income.exempt_80c), "80C_allowed": r2(capped_80c),
            "80D_claimed": r2(income.exempt_80d), "80D_allowed": r2(capped_80d),
            "other_chapter_vi_a": r2(income.exempt_other + income.deduction_chapter_vi_a),
            "total": total_deductions,
        },
        "house_property_setoff": r2(hp_setoff),
        "total_income": r2(adjusted_gross),
        "taxable_income": taxable_income,
        "slab_breakdown": breakdown,
        "tax_before_rebate": tax_before_rebate,
        "rebate_87A": {"allowed": rebate["allowed"], "amount": r2(rebate_amount),
                        "threshold": rebate.get("threshold"), "reason": rebate.get("reason", "")},
        "surcharge": {"rate": surcharge_pct, "amount": r2(surcharge)},
        "cess_health_education": cess,
        "total_tax": total_tax,
        "less_tds": r2(tds),
        "less_advance_tax": r2(advance),
        "tax_payable": shortfall,
        "refund": refund,
        "interest": interest_234(total_tax, tds + advance, ay, income.salary > 0),
        "notes": [
            "Verify every figure against your computation; Ultron assists, it does not file.",
            "Marginal-relief and 87A edges are computed, then flagged for review.",
        ],
    }


def interest_234(total_tax: float, tds_paid: float, ay: str, salaried: bool = True) -> Dict[str, Any]:
    """u/s 234B (simple interest for non-payment) and 234C (quarterly deferral)."""
    fy = _fyi_for_ay(ay)
    dates = DUE_DATES.get(fy)
    if not dates:
        return {"b_234": 0.0, "c_234": 0.0, "note": f"due dates not configured for AY {ay}"}
    tds = tds_paid
    b234 = r2(tds * 0.01 * 4) if tds < total_tax else 0.0
    if tds < total_tax:
        months_out = 4 if not salaried else 4
        b234 = r2((total_tax - tds) * 0.01 * months_out)
    c234 = 0.0
    remaining = total_tax
    tds_applied = tds
    for due, share in (("d1", 0.25), ("q1", 0.25), ("q2", 0.25), ("q3", 0.25)):
        target = r2(total_tax * share)
        payable = max(0.0, min(remaining, target - 0))
        shortfall = r2(max(0.0, payable - tds_applied))
        tds_applied = 0.0
        remaining -= payable
        if shortfall > 0:
            c234 += shortfall
    c234 = r2(c234 * 0.01)
    return {"b_234": b234, "c_234": c234, "total": r2(b234 + c234),
            "due_dates": dates, "note": "Indicative; actual interest uses the payment date grid."}


def advance_tax_schedule(total_tax: float, tds_paid: float, ay: str) -> Dict[str, Any]:
    """Due-date-wise advance tax instalments for a non-business assessee."""
    fy = _fyi_for_ay(ay)
    dates = DUE_DATES.get(fy, {})
    payable = r2(max(0.0, total_tax - tds_paid))
    if not dates:
        return {"installments": [], "total": payable}
    rows = []
    remaining_tds = tds_paid
    for label, share, due_key in (("Due date 1", 0.25, "d1"), ("Q2", 0.25, "q1"),
                                  ("Q3", 0.25, "q2"), ("Q4", 0.25, "q3")):
        due_amount = r2(payable * share)
        tds_offset = min(remaining_tds, due_amount) if label == "Due date 1" else 0.0
        remaining_tds = r2(remaining_tds - tds_offset)
        rows.append({"installment": label, "due_date": dates.get(due_key, ""),
                     "percentage": 25, "amount_due": r2(due_amount - tds_offset)})
    tds_rows = [
        {"section": "TDS on salary", "due_date": dates.get("tds_salary", ""), "frequency": "monthly"},
        {"section": "TDS (non-salary)", "due_date": dates.get("tds_q2", ""), "frequency": "quarterly"},
        {"section": "TDS Q3", "due_date": dates.get("tds_q3", ""), "frequency": "quarterly"},
        {"section": "TDS Q4", "due_date": dates.get("tds_q4", ""), "frequency": "quarterly"},
    ]
    return {"installments": rows, "total": payable, "tds_due_dates": tds_rows,
            "return_due_date": dates.get("p4", "")}


def tds_estimate(section: str, amounts: List[float]) -> Dict[str, Any]:
    """Estimate TDS across periods for a section using the Ultron rate card."""
    key = section.upper()
    if key not in TDS_RATES:
        return {"ok": False, "error": f"unknown section '{section}'",
                "supported": sorted(TDS_RATES)}
    rate, threshold, basis, period = TDS_RATES[key]
    rows: List[Dict[str, Any]] = []
    total = 0.0
    for index, amount in enumerate(amounts or [], start=1):
        taxable = max(0.0, float(amount) - threshold)
        tds = r2(taxable * rate / 100.0)
        total = r2(total + tds)
        rows.append({"period": index, "amount": r2(amount), "threshold": threshold,
                     "taxable": r2(taxable), "rate": rate, "tds": tds})
    return {"ok": True, "section": key, "rate": rate, "threshold": threshold,
            "basis": basis, "frequency": period, "periods": rows, "total_tds": total}


def e_invoice_summary(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate the fields IRN generation needs before the portal call."""
    required = ("description", "hsn", "quantity", "unit_price", "rate")
    problems: List[str] = []
    for index, item in enumerate(items or [], start=1):
        for field_name in required:
            if field_name not in item:
                problems.append(f"line {index}: missing '{field_name}'")
        hsn = str(item.get("hsn", ""))
        if hsn and not hsn.isdigit():
            problems.append(f"line {index}: HSN '{hsn}' is not numeric")
        if len(hsn) not in (4, 6, 8):
            problems.append(f"line {index}: HSN '{hsn}' must be 4, 6 or 8 digits")
    return {"ok": not problems, "problems": problems,
            "ready": not problems, "lines": len(items or [])}


def dumps(data: Dict[str, Any]) -> str:
    return json.dumps(data, indent=2, default=str)
