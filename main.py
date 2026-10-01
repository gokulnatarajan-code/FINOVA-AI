"""FINOVA-AI agent endpoint: loan eligibility, bank comparison, SIP / lump sum / SWP guidance.
All rates and returns are ILLUSTRATIVE assumptions, not live bank data or guaranteed returns."""
from typing import Optional
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="FINOVA-AI Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DISCLAIMER = ("Indicative guidance only, not professional financial advice. Rates and returns are "
              "illustrative assumptions; final loan decisions rest with the bank.")

# Illustrative rates (% p.a.). Verify on each bank's website before relying on them.
BANKS = [
    {"bank": "SBI", "rate": 8.7},
    {"bank": "Indian Bank", "rate": 8.5},
    {"bank": "Bank of Baroda", "rate": 8.6},
]
RETURN_SCENARIOS = {"conservative": 8.0, "moderate": 10.0, "aggressive": 12.0}


class Inputs(BaseModel):
    goal: str = "both"                      # loan | invest | both
    age: int = 30
    monthly_income: float = 50000
    employment_type: str = "salaried"       # salaried | self_employed
    existing_emi: float = 0
    loan_amount: float = 0
    tenure_years: int = 10
    monthly_sip: float = 0
    lump_sum: float = 0
    investment_horizon_years: int = 10
    swp_monthly_withdrawal: float = 0


def emi(p, annual_rate, years):
    r, n = annual_rate / 1200, years * 12
    return p / n if r == 0 else p * r * (1 + r) ** n / ((1 + r) ** n - 1)


def max_principal(monthly_emi, annual_rate, years):
    r, n = annual_rate / 1200, years * 12
    return monthly_emi * n if r == 0 else monthly_emi * ((1 + r) ** n - 1) / (r * (1 + r) ** n)


def loan_module(x: Inputs, trace):
    foir = 0.50 if x.employment_type == "salaried" else 0.45
    tenure = max(0, min(x.tenure_years, 70 - x.age))
    available_emi = max(0.0, x.monthly_income * foir - x.existing_emi)
    trace.append(f"Applied {int(foir*100)}% income-to-EMI limit for {x.employment_type}: "
                 f"available EMI Rs {available_emi:,.0f}/month")
    trace.append(f"Tenure capped at {tenure} years (loan must end by age 70)")
    reasons, offers = [], []
    if x.age < 21:
        reasons.append("Minimum age is 21")
    if tenure < 1:
        reasons.append("Not enough tenure left before age 70")
    if available_emi <= 0:
        reasons.append("Existing EMIs already use the permitted share of income")
    for b in BANKS:
        cap = max_principal(available_emi, b["rate"], tenure) if tenure else 0
        offer = {"bank": b["bank"], "illustrative_rate_pct": b["rate"],
                 "max_eligible_amount": round(cap)}
        if x.loan_amount > 0 and tenure:
            offer["emi_for_requested_amount"] = round(emi(x.loan_amount, b["rate"], tenure))
            offer["requested_amount_ok"] = x.loan_amount <= cap
        offers.append(offer)
    best = min(offers, key=lambda o: o["illustrative_rate_pct"])
    best_cap = max(o["max_eligible_amount"] for o in offers)
    eligible = not reasons and (x.loan_amount <= best_cap if x.loan_amount > 0 else best_cap > 0)
    if x.loan_amount > best_cap and not reasons:
        reasons.append(f"Requested amount exceeds estimated limit of Rs {best_cap:,.0f}; "
                       "try a longer tenure, lower amount or clear existing EMIs")
    trace.append(f"Compared {len(offers)} banks; lowest illustrative rate: {best['bank']}")
    return {"eligible": eligible, "reasons_if_not_eligible": reasons,
            "estimated_max_loan": best_cap, "bank_comparison": offers,
            "recommended_bank": best["bank"] if eligible else None}


def sip_fv(monthly, annual_pct, years):
    i, n = (1 + annual_pct / 100) ** (1 / 12) - 1, years * 12
    return monthly * (((1 + i) ** n - 1) / i) * (1 + i) if i else monthly * n


def invest_module(x: Inputs, trace):
    y = x.investment_horizon_years
    out = {}
    for name, rate in RETURN_SCENARIOS.items():
        s = {"assumed_return_pct": rate}
        if x.monthly_sip > 0:
            invested = x.monthly_sip * 12 * y
            fv = sip_fv(x.monthly_sip, rate, y)
            s["sip"] = {"invested": round(invested), "future_value": round(fv),
                        "gain": round(fv - invested)}
        if x.lump_sum > 0:
            fv = x.lump_sum * (1 + rate / 100) ** y
            s["lump_sum"] = {"invested": round(x.lump_sum), "future_value": round(fv),
                             "gain": round(fv - x.lump_sum)}
            i, n = (1 + rate / 100) ** (1 / 12) - 1, y * 12
            sustainable = x.lump_sum * i / (1 - (1 + i) ** -n) if i else x.lump_sum / n
            swp = {"sustainable_monthly_withdrawal": round(sustainable)}
            if x.swp_monthly_withdrawal > 0:
                bal, months = x.lump_sum, 0
                while bal > 0 and months < 600:
                    bal = bal * (1 + i) - x.swp_monthly_withdrawal
                    months += 1
                swp["requested_withdrawal"] = round(x.swp_monthly_withdrawal)
                swp["corpus_lasts_years"] = round(months / 12, 1) if bal <= 0 else "50+"
            s["swp"] = swp
        out[name] = s
    trace.append(f"Projected SIP / lump sum / SWP over {y} years under 3 return scenarios")
    return out


@app.get("/")
def health():
    return {"status": "FINOVA-AI agent running", "usage": "POST /run with JSON inputs"}


@app.post("/run")
def run(x: Inputs):
    trace = [f"Received request, goal = {x.goal}"]
    result = {}
    if x.goal in ("loan", "both") and (x.loan_amount > 0 or x.goal == "loan"):
        result["loan_module"] = loan_module(x, trace)
    if x.goal in ("invest", "both") and (x.monthly_sip > 0 or x.lump_sum > 0):
        result["investment_module"] = invest_module(x, trace)
    if not result:
        trace.append("No loan amount or investment amount supplied")
        result["message"] = "Provide loan_amount and/or monthly_sip / lump_sum."
    summary = []
    lm = result.get("loan_module")
    if lm:
        summary.append(f"Loan: {'eligible' if lm['eligible'] else 'not eligible'}"
                       f" (estimated limit Rs {lm['estimated_max_loan']:,.0f})"
                       + (f", best option {lm['recommended_bank']}" if lm['recommended_bank'] else ""))
    if "investment_module" in result:
        summary.append("Investment projections generated for 3 return scenarios")
    return {"agent_trace": trace, "recommendation_summary": summary,
            **result, "disclaimer": DISCLAIMER}
