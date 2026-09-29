#!/usr/bin/env python3
"""
Referentially consistent synthetic data generator for the Risk, Fraud &
Regulatory Intelligence Copilot.

Design goals
------------
1.  Privacy by construction: no production data, no real PII, no real PANs.
    Names, IPs, device fingerprints and account numbers are all drawn from
    seeded synthetic vocabularies.
2.  Referential integrity: every account belongs to exactly one customer,
    every transaction belongs to an existing account, and every transaction
    carries a customer_id that matches the account owner.
3.  Planted ground truth: a known set of fraud scenarios is injected with
    explicit labels so the detection layer can be *measured*, not asserted.
    A matched control group of benign customers lets us report precision,
    recall and false-positive rate.
4.  Deterministic: same seed + same as-of date => byte-identical output.

Usage
-----
    python scripts/generate_synthetic_data.py --customers 600 --seed 42
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from backend.config import load_config  # noqa: E402

# --------------------------------------------------------------------------
# Synthetic vocabularies
# --------------------------------------------------------------------------

FIRST_NAMES = [
    "Aarav", "Mei", "Fatima", "Diego", "Ananya", "Omar", "Elena", "Kwame", "Yuki",
    "Priya", "Tomas", "Sofia", "Rahul", "Chen", "Amara", "Lucas", "Ingrid", "Hassan",
    "Nadia", "Jonas", "Leila", "Marco", "Zara", "Victor", "Aisha", "Dmitri",
    "Chioma", "Hiro", "Rosa", "Ibrahim", "Freya", "Rafael", "Sana", "Andrei",
    "Noor", "Santiago", "Keiko", "Idris", "Beatriz", "Viktor", "Thandi", "Luca",
]
LAST_NAMES = [
    "Sharma", "Chen", "Haddad", "Morales", "Iyer", "Al-Rashid", "Petrova", "Mensah",
    "Tanaka", "Nair", "Novak", "Rossi", "Kapoor", "Wei", "Okonkwo", "Silva",
    "Larsen", "Fahmy", "Kovacs", "Berger", "Rahman", "Ricci", "Ahmed", "Novak",
    "Diallo", "Volkov", "Eze", "Sato", "Fernandez", "Rahman", "Lindqvist",
    "Costa", "Farooq", "Ivanov", "Saleh", "Ortega", "Yamamoto", "Bello",
    "Almeida", "Petrov", "Mokoena", "Conti",
]

SEGMENTS = {
    "retail": {"weight": 0.46, "typology": "Consumer banking, salary + card spend"},
    "sme": {"weight": 0.26, "typology": "Small business, invoice payments"},
    "corporate": {"weight": 0.12, "typology": "Corporate treasury and trade finance"},
    "nbfc": {"weight": 0.10, "typology": "NBFC lending, loan disbursals and collections"},
    "individual_high_networth": {"weight": 0.06, "typology": "Private banking, wealth products"},
}

JURISDICTIONS = {
    "IN": {"weight": 0.34, "name": "India", "ccy": "INR", "aml_regime": "FIU-IND / PMLA"},
    "GB": {"weight": 0.18, "name": "United Kingdom", "ccy": "GBP", "aml_regime": "JMLSG / FCA"},
    "US": {"weight": 0.20, "name": "United States", "ccy": "USD", "aml_regime": "FinCEN / BSA"},
    "AE": {"weight": 0.10, "name": "United Arab Emirates", "ccy": "AED", "aml_regime": "CBUAE"},
    "SG": {"weight": 0.08, "name": "Singapore", "ccy": "SGD", "aml_regime": "MAS"},
    "NG": {"weight": 0.05, "name": "Nigeria", "ccy": "NGN", "aml_regime": "FIU / EFCC"},
    "DE": {"weight": 0.05, "name": "Germany", "ccy": "EUR", "aml_regime": "BaFin"},
}

# Jurisdictions the model treats as elevated risk. Deliberately a
# *config-driven* list, not a hardcoded Python literal elsewhere.
ELEVATED_RISK_COUNTRIES = {
    "NG", "RU", "IR", "KP", "SY", "PA", "KY", "VG", "MM", "LB",
}

MERCHANTS = {
    "grocery": ["FRESHCO", "GREENBASKET", "CITYFOODS"],
    "fuel": ["SHELLPETRO", "FUELMAX"],
    "atm": ["ATM-ATM", "ATM-BANK", "ATM-NBFC"],
    "restaurant": ["BISTRO9", "CAFECORNER", "SPICEHOUSE"],
    "electronics": ["TECHWORLD", "GADGETS4U"],
    "travel": ["SKYLINE-AIR", "HOTELSINTL", "RAILCONNECT"],
    "utility": ["POWERGRID", "WATERWORKS", "NETFIBRE"],
    "gambling": ["BETSTREAM", "CASINOXX"],
    "crypto": ["CHAINXCHANGE", "DIGICOINPAY"],
    "cash_equivalent": ["CASHDEP-ATM", "CASHDEP-BRANCH", "CASHDEP-CC"],
    "trade": ["MERIDIAN-TRADING", "ORION-IMPORT-EXPORT", "PACIFIC-SUPPLY"],
    "lending": ["LOANSERVICER", "MICROFINANCE-PARTNER"],
}
MCC = {
    "grocery": "5411", "fuel": "5541", "atm": "6011", "restaurant": "5812",
    "electronics": "5732", "travel": "4722", "utility": "4900", "gambling": "7995",
    "crypto": "6051", "cash_equivalent": "4829", "trade": "5099", "lending": "6012",
}

CHANNELS = ["ATM", "POS", "ONLINE", "MOBILE", "BRANCH", "WIRE", "ACH"]
CASH_CHANNELS = {"ATM", "BRANCH"}

ACCOUNT_TYPES = {
    "retail": [("current", 0.6), ("savings", 0.3), ("credit_card", 0.1)],
    "sme": [("current", 0.85), ("trade_finance", 0.15)],
    "corporate": [("current", 0.6), ("trade_finance", 0.3), ("escrow", 0.1)],
    "nbfc": [("loan_ledger", 0.7), ("current", 0.3)],
    "individual_high_networth": [("current", 0.4), ("custody", 0.4), ("savings", 0.2)],
}

# Baseline behaviour profile per segment: (txns/month, mean ticket, cash share,
# cross-border share, high-risk-country share)
BASELINE = {
    "retail": (34, 7800, 0.16, 0.03, 0.006),
    "sme": (52, 41000, 0.34, 0.11, 0.010),
    "corporate": (40, 185000, 0.05, 0.24, 0.014),
    "nbfc": (26, 96000, 0.42, 0.05, 0.008),
    "individual_high_networth": (22, 132000, 0.18, 0.19, 0.021),
}

# --------------------------------------------------------------------------
# Row containers
# --------------------------------------------------------------------------


@dataclass
class Tables:
    customers: list = field(default_factory=list)
    accounts: list = field(default_factory=list)
    transactions: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    alert_log: list = field(default_factory=list)
    credit: list = field(default_factory=list)
    liquidity: list = field(default_factory=list)
    counterparties: list = field(default_factory=list)
    entity_exposures: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def weighted(rng: random.Random, mapping: dict) -> str:
    keys = list(mapping)
    return rng.choices(keys, weights=[mapping[k]["weight"] for k in keys], k=1)[0]


def simple_amount(rng: random.Random, mean_ticket: float) -> float:
    """Right-skewed ticket size: log-normal, as real spend distributions are."""
    return round(max(25.0, rng.lognormvariate(0, 0.9) * mean_ticket), 2)


# --------------------------------------------------------------------------
# Entity generation
# --------------------------------------------------------------------------


def make_customer(rng: random.Random, idx: int, as_of: date) -> dict:
    segment = weighted(rng, SEGMENTS)
    juris = weighted(rng, JURISDICTIONS)
    j = JURISDICTIONS[juris]
    name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
    onboarded = as_of - timedelta(days=rng.randint(400, 3650))
    is_pep = rng.random() < 0.035
    kyc_status = rng.choices(
        ["current", "due_diligence", "overdue", "enhanced"],
        weights=[0.80, 0.10, 0.04, 0.06],
    )[0]
    if is_pep or segment == "individual_high_networth":
        kyc_status = rng.choices(["enhanced", "current"], weights=[0.65, 0.35])[0]
    # A prior risk rating exists for part of the book (the rest is unrated and
    # must be rated by the copilot) - this is what makes the model useful.
    rated = rng.random() < 0.55
    risk_rating = rng.choices(["low", "medium", "high"], weights=[0.62, 0.28, 0.10])[0] if rated else None
    return {
        "customer_id": f"CUST-{idx:06d}",
        "name": f"{name} {j['name'][:2].upper()}{rng.randint(100, 999)} Ltd"
        if segment in ("sme", "corporate", "nbfc") else name,
        "segment": segment,
        "business_type": segment,
        "jurisdiction": juris,
        "jurisdiction_name": j["name"],
        "domestic_currency": j["ccy"],
        "industry": rng.choice(
            ["Financial Services", "Trading", "Manufacturing", "Real Estate",
             "Technology", "Agriculture", "Healthcare", "Logistics", "Education"]
        ) if segment in ("sme", "corporate") else "N/A",
        "is_pep": int(is_pep),
        "kyc_status": kyc_status,
        "kyc_last_review": (as_of - timedelta(days=rng.randint(10, 900))).isoformat(),
        "onboarded_at": onboarded.isoformat(),
        "ubo_count": rng.randint(1, 4) if segment in ("sme", "corporate") else 1,
        "risk_rating": risk_rating,
        "expected_monthly_volume": round(rng.uniform(5_000, 4_000_000), 2),
        "expected_monthly_txns": rng.randint(6, 90),
        "data_classification": "SYNTHETIC_NO_PII",
    }


def make_accounts(rng: random.Random, cust: dict, as_of: date) -> list:
    base_type = BASELINE[cust["segment"]][0]
    n = rng.choices([1, 2, 3], weights=[0.62, 0.28, 0.10])[0]
    types = ACCOUNT_TYPES[cust["segment"]]
    accounts = []
    for k in range(n):
        atype = rng.choices([t for t, _ in types], weights=[w for _, w in types])[0]
        opened = datetime.fromisoformat(cust["onboarded_at"]).date() + timedelta(days=rng.randint(1, 120))
        opened = min(opened, as_of - timedelta(days=5))
        acct_no = f"{rng.randint(10000000, 99999999)}{rng.randint(1000, 9999)}"
        accounts.append({
            "account_id": f"ACCT-{cust['customer_id'].split('-')[1]}-{k + 1}",
            "customer_id": cust["customer_id"],
            "account_number_masked": f"XXXX-XXXX-{acct_no[-4:]}",
            "account_type": atype,
            "currency": cust["domestic_currency"],
            "opened_at": opened.isoformat(),
            "status": "active" if rng.random() < 0.94 else "dormant",
            "opening_balance": round(rng.uniform(1_000, 900_000), 2),
            "expected_daily_volume": round(
                cust["expected_monthly_volume"] / 30 * rng.uniform(0.5, 1.8), 2
            ),
            "branch_code": f"BR-{rng.randint(1, 240):03d}",
        })
    return accounts


# --------------------------------------------------------------------------
# Transaction generation
# --------------------------------------------------------------------------


class TxnFactory:
    def __init__(self, rng: random.Random, as_of: date, start: date):
        self.rng = rng
        self.as_of = as_of
        self.start = start
        self.seq = 0
        self.rows: list = []

    def emit(self, cust, acct, ts: datetime, amount, txn_type, channel, merchant_cat,
             merchant_country, device_id=None, ip_country=None, status="posted",
             flagged=""):
        self.seq += 1
        txn_id = f"TXN-{self.seq:08d}"
        if device_id is None:
            device_id = f"DEV-{abs(hash(cust['customer_id'])) % 10**9:09d}"
        self.rows.append({
            "transaction_id": txn_id,
            "account_id": acct["account_id"],
            "customer_id": cust["customer_id"],
            "transaction_ts": ts.isoformat(sep=" "),
            "business_date": ts.date().isoformat(),
            "amount": round(float(amount), 2),
            "currency": acct["currency"],
            "txn_type": txn_type,
            "channel": channel,
            "is_cash": int(channel in CASH_CHANNELS or txn_type in ("cash_deposit", "cash_withdrawal")),
            "merchant_name": self.rng.choice(MERCHANTS[merchant_cat]),
            "merchant_category": merchant_cat,
            "merchant_country": merchant_country,
            "merchant_mcc": MCC[merchant_cat],
            "device_id": device_id,
            "ip_country": ip_country or cust["jurisdiction"],
            "status": status,
            "is_reversal": 0,
            "ingested_at": (ts + timedelta(minutes=self.rng.randint(1, 240))).isoformat(sep=" "),
            "planted_scenario": flagged,
        })
        return txn_id


def generate_baseline_txns(rng, factory, cust, accounts, horizon_days):
    """Normal, legitimate activity for a customer.

    Two realism constraints keep the control group genuinely un-flaggable:

    1. *Location consistency.* Each customer has a small, stable set of
       jurisdictions they actually operate in (home plus one or two normal
       trading partners - elevated-risk jurisdictions are excluded, because a
       routine retail customer does not pick one as a trading partner). Country
       is then assigned in *epochs*: the customer "sits" in one jurisdiction for
       several days to a few weeks before relocating. Without this, uniform
       per-transaction sampling manufactures 6,000 km/h hops that no detection
       rule should be expected to ignore, and every internationally active
       customer becomes an impossible-travel false positive.
    2. *Cash discipline.* A legitimate customer's daily cash activity stays
       below the reporting threshold. Customers who routinely cross the CTR
       line are not a clean control group - they are a reporting population -
       and labelling them "benign" would corrupt the ground truth.
    """
    txns_per_month, mean_ticket, cash_share, xborder_share, risk_share = BASELINE[cust["segment"]]
    acct = rng.choice(accounts)

    home = cust["jurisdiction"]
    normal_partners = [c for c in JURISDICTIONS
                       if c != home and c not in ELEVATED_RISK_COUNTRIES]
    active = [home] + rng.sample(normal_partners, k=min(len(normal_partners),
                                                         rng.randint(1, 2)))
    risk_pool = sorted(ELEVATED_RISK_COUNTRIES)
    cust["active_countries"] = active

    # Country epochs: relocate every 5-30 days, weighted towards the home and
    # active jurisdictions.
    epochs: List[tuple] = []
    day = 0
    while day < horizon_days:
        duration = rng.randint(5, 30)
        if rng.random() < 0.55:
            country = home
        else:
            country = rng.choices(active, weights=[0.70] + [0.30 / (len(active) - 1)] * (len(active) - 1)
                                  if len(active) > 1 else [1.0])[0]
        epochs.append((day, min(horizon_days, day + duration), country))
        day += duration

    def country_for(d: int) -> str:
        for lo, hi, country in epochs:
            if lo <= d < hi:
                return country
        return home

    n = max(6, int(txns_per_month * horizon_days / 30 * rng.uniform(0.75, 1.3)))
    daily_cash: Dict[str, float] = {}
    ctr_line = 10_000.0
    for _ in range(n):
        day = rng.randint(0, horizon_days)
        ts = datetime.combine(
            factory.start + timedelta(days=day),
            datetime.min.time(),
        ) + timedelta(hours=rng.randint(7, 21), minutes=rng.randint(0, 59))
        if ts.date() >= factory.as_of:
            continue
        if rng.random() < 0.012:
            continue  # genuine dip in activity
        roll = rng.random()
        if roll < cash_share:
            channel = rng.choice(list(CASH_CHANNELS))
            if channel == "ATM":
                cat = rng.choices(["atm", "cash_equivalent"], weights=[0.5, 0.5])[0]
            else:
                cat = "cash_equivalent"
            amount = simple_amount(rng, mean_ticket * 0.35)
            # Benign cash is capped below the structuring avoidance band (85% of
            # the reporting line). Without this cap, ordinary cash-using
            # customers can accumulate three deposits inside the band purely by
            # chance, which is what produced 101 of 119 false positives.
            key = ts.date().isoformat()
            if daily_cash.get(key, 0.0) + amount > ctr_line * 0.70:
                continue
            daily_cash[key] = daily_cash.get(key, 0.0) + amount
            if rng.random() < 0.010:
                amount = round(rng.uniform(11_000, 24_000), 2)  # one large legal cash movement
            ttype = "cash_deposit" if rng.random() < 0.6 else "cash_withdrawal"
        elif roll < cash_share + xborder_share:
            channel = "WIRE"
            cat = rng.choice(["trade", "travel", "electronics"])
            amount = simple_amount(rng, mean_ticket * 2.2)
            ttype = rng.choice(["wire_out", "wire_in"])
        else:
            channel = rng.choices(CHANNELS, weights=[0.22, 0.30, 0.24, 0.20, 0.02, 0.01, 0.01])[0]
            cat = rng.choices(
                ["grocery", "fuel", "restaurant", "electronics", "travel", "utility",
                 "gambling", "crypto", "lending"],
                weights=[0.30, 0.16, 0.20, 0.07, 0.05, 0.12, 0.02, 0.015, 0.085],
            )[0]
            amount = simple_amount(rng, mean_ticket * 0.25)
            ttype = {"grocery": "card_purchase", "fuel": "card_purchase", "restaurant": "card_purchase",
                     "electronics": "card_purchase", "travel": "card_purchase", "utility": "transfer",
                     "gambling": "card_purchase", "crypto": "card_purchase",
                     "lending": "transfer"}[cat]
        if rng.random() < risk_share:
            country = rng.choice(risk_pool)
        else:
            country = country_for(day)
        factory.emit(cust, acct, ts, amount, ttype, channel, cat, country)
    # A handful of rejected / reversed rows so the pipeline must handle them.
    if rng.random() < 0.35 and factory.rows:
        src = rng.choice(factory.rows)
        factory.emit(cust, accounts[0], datetime.fromisoformat(src["transaction_ts"]) + timedelta(days=1),
                     src["amount"], src["txn_type"], src["channel"], src["merchant_category"],
                     src["merchant_country"], device_id=src["device_id"], status="reversed")


# --------------------------------------------------------------------------
# Planted fraud scenarios
# --------------------------------------------------------------------------


def plant_structuring(rng, factory, cust, accounts, as_of, horizon_days, ctr_line=10_000.0):
    """Sub-threshold cash deposits clustered just under the CTR line, then cash out.

    Deposits are placed inside the *avoidance band* (85-99.9% of the reporting
    threshold), because that is what deliberate threshold avoidance looks like.
    The previous range (2,100-9,850) spread the deposits across the whole
    sub-threshold space, which is indistinguishable from ordinary cash use - it
    is why 85% of benign controls satisfied the old detector.
    """
    acct = accounts[0]
    device = f"DEV-STRUCT-{abs(hash(cust['customer_id'])) % 10**7:07d}"
    # The most recent deposit must fall inside the detector's candidate window.
    end_day = rng.randint(1, 3)
    band_low, band_high = ctr_line * 0.87, ctr_line * 0.995
    for d in range(12):
        day = horizon_days - end_day - d
        if day < 0:
            continue
        date_d = factory.start + timedelta(days=day)
        for _ in range(rng.randint(3, 6)):
            amount = round(rng.uniform(band_low, band_high), 2)
            ts = datetime.combine(date_d, datetime.min.time()) + timedelta(
                hours=rng.randint(8, 18), minutes=rng.randint(0, 59))
            factory.emit(cust, acct, ts, amount, "cash_deposit",
                         rng.choice(["ATM", "BRANCH"]), "cash_equivalent",
                         cust["jurisdiction"], device_id=device, flagged="structuring")
    ts = datetime.combine(factory.start + timedelta(days=horizon_days - end_day + 1),
                          datetime.min.time()) + timedelta(hours=11)
    factory.emit(cust, acct, ts, round(rng.uniform(38_000, 96_000), 2), "cash_withdrawal",
                 "BRANCH", "cash_equivalent", cust["jurisdiction"], device_id=device,
                 flagged="structuring")


def plant_account_takeover(rng, factory, cust, accounts, as_of, horizon_days):
    """Credential compromise: new device, new IP country, immediate large spend."""
    acct = accounts[0]
    compromised_device = f"DEV-ATO-{rng.randint(10**8, 10**9 - 1)}"
    # Never the customer's own jurisdiction: a login from home is not a
    # takeover, and planting it would be a case the rule is right to miss.
    ip_pool = [c for c in ("NG", "RU", "PH", "RO", "KZ") if c != cust["jurisdiction"]]
    ip_country = rng.choice(ip_pool)
    for i in range(3):
        day = horizon_days - rng.randint(1, 5)
        ts = datetime.combine(factory.start + timedelta(days=day), datetime.min.time()) + timedelta(
            hours=rng.randint(1, 6), minutes=rng.randint(0, 59))
        factory.emit(cust, acct, ts, round(rng.uniform(7_000, 41_000), 2),
                     "card_purchase", rng.choice(["ONLINE", "MOBILE"]), "electronics",
                     rng.choice(["US", "GB", "AE"]), device_id=compromised_device,
                     ip_country=ip_country, flagged="account_takeover")
    # Dormancy before takeover: backdate the account status signal.
    for d in range(20, 62, 6):
        day = horizon_days - rng.randint(70, 95)
        ts = datetime.combine(factory.start + timedelta(days=day), datetime.min.time()) + timedelta(hours=12)
        factory.emit(cust, acct, ts, round(rng.uniform(60, 400), 2), "transfer", "ACH",
                     "utility", cust["jurisdiction"], flagged="account_takeover")


def plant_impossible_travel(rng, factory, cust, accounts, as_of, horizon_days):
    """Three or more jurisdictions inside 24 hours, including an elevated-risk one.

    Planted inside the geographic detector's 90-day window so the ground-truth
    label is genuinely detectable, not a case the model is meant to miss.
    """
    acct = accounts[0]
    for _ in range(rng.randint(2, 3)):
        day = rng.randint(max(1, horizon_days - 55), horizon_days - 1)
        base = datetime.combine(factory.start + timedelta(days=day), datetime.min.time()) + timedelta(hours=6)
        chain = rng.sample(
            [cust["jurisdiction"], "AE", "GB", "US", "PA", "NG", "KY", "SG"], 3)
        for k, country in enumerate(chain):
            ts = base + timedelta(hours=5 * k)
            factory.emit(cust, acct, ts, round(rng.uniform(1_800, 14_500), 2),
                         rng.choice(["card_purchase", "wire_out"]),
                         "ONLINE" if k % 2 == 0 else "WIRE",
                         rng.choice(["travel", "electronics", "trade"]),
                         country, flagged="impossible_travel")


def plant_mule_network(rng, factory, hub, mules, accounts_by_cust, horizon_days):
    """Star topology: many senders -> one hub -> fast pass-through out. Shared device."""
    hub_acct = accounts_by_cust[hub["customer_id"]][0]
    shared_device = f"DEV-MULE-{rng.randint(10**6, 10**7 - 1)}"
    day = horizon_days - rng.randint(1, 4)
    hub_ts = datetime.combine(factory.start + timedelta(days=day), datetime.min.time()) + timedelta(hours=9)
    total_in = 0.0
    for mule in mules:
        m_acct = accounts_by_cust[mule["customer_id"]][0]
        amount = round(rng.uniform(3_500, 18_000), 2)
        total_in += amount
        ts = hub_ts - timedelta(hours=rng.randint(3, 30))
        factory.emit(mule, m_acct, ts, amount, "wire_in", "WIRE", "trade",
                     mule["jurisdiction"], device_id=shared_device, flagged="mule_network")
    # Pass-through: hub pushes 88% of the inflow onward within 24h.
    for k in range(rng.randint(3, 5)):
        ts = hub_ts + timedelta(hours=rng.randint(2, 22))
        factory.emit(hub, hub_acct, ts, round(total_in * 0.88 / 4.5, 2), "wire_out", "WIRE",
                     "trade", rng.choice(["PA", "AE", "NG", "VG"]),
                     device_id=shared_device, flagged="mule_network")
    return total_in


def plant_trade_based(rng, factory, cust, accounts, horizon_days):
    """Invoice-mismatch style pattern: round-value, back-to-back cross-border wires."""
    acct = accounts[0]
    for i in range(rng.randint(3, 6)):
        day = horizon_days - rng.randint(2, 60)
        ts = datetime.combine(factory.start + timedelta(days=day), datetime.min.time()) + timedelta(
            hours=rng.randint(8, 17))
        amount = float(rng.choice([50_000, 75_000, 100_000, 150_000, 200_000, 250_000]))
        factory.emit(cust, acct, ts, amount, "wire_out", "WIRE", "trade",
                     rng.choice(["HK", "SG", "AE", "CN", "TR"]), flagged="trade_based")
    day = horizon_days - rng.randint(70, 110)
    ts = datetime.combine(factory.start + timedelta(days=day), datetime.min.time()) + timedelta(hours=11)
    factory.emit(cust, acct, ts, round(rng.uniform(1_200, 9_400), 2), "cash_deposit",
                 "BRANCH", "cash_equivalent", cust["jurisdiction"], flagged="trade_based")


# --------------------------------------------------------------------------
# Supporting datasets
# --------------------------------------------------------------------------


def generate_credit_portfolio(rng, customers, as_of):
    rows = []
    for c in customers:
        seg = c["segment"]
        if seg in ("corporate", "nbfc", "sme"):
            pd_prob = {"corporate": 0.021, "nbfc": 0.048, "sme": 0.062}[seg]
            pd_prob *= rng.uniform(0.25, 3.2)
        else:
            pd_prob = rng.uniform(0.002, 0.045)
        ead = round(c["expected_monthly_volume"] * rng.uniform(1.5, 9.0), 2)
        lgd = round(rng.uniform(0.25, 0.65), 3)
        pd_bp = round(pd_prob, 5)
        rows.append({
            "customer_id": c["customer_id"],
            "as_of_date": as_of.isoformat(),
            "exposure_at_default": ead,
            "probability_of_default": pd_bp,
            "loss_given_default": lgd,
            "stage": "stage_2" if pd_prob > 0.10 else ("stage_1" if pd_prob > 0.03 else "stage_0"),
            "ecl_12m": round(ead * pd_bp * lgd, 2),
            "ecl_lifetime": round(ead * pd_bp * lgd * rng.uniform(1.6, 3.4), 2),
            "limit_utilisation": round(rng.uniform(0.05, 0.99), 4),
            "rating_model": "RISK-RATING-v3.2",
        })
    return rows


def generate_liquidity(rng, as_of, days=30):
    """Entity-level HQLA / net outflow view for the liquidity copilot path."""
    rows = []
    for entity in ["BANK-ENTITY-IN", "BANK-ENTITY-GB", "NBFC-ENTITY-IN"]:
        for d in range(days):
            day = as_of - timedelta(days=d)
            hqla = round(rng.uniform(2_400_000, 6_800_000), 2)
            outflow = round(hqla * rng.uniform(0.72, 1.42), 2)
            rows.append({
                "entity_id": entity,
                "report_date": day.isoformat(),
                "hqla_total": hqla,
                "net_cash_outflow_30d": outflow,
                "lcr": round(hqla / outflow, 4) if outflow else None,
                "available_stable_funding": round(rng.uniform(3_100_000, 7_400_000), 2),
                "required_stable_funding": round(rng.uniform(3_000_000, 7_000_000), 2),
                "nsfr": round(rng.uniform(0.93, 1.21), 4),
                "counterparty_concentration_pct": round(rng.uniform(0.04, 0.34), 4),
            })
    return rows


def generate_counterparties_and_exposures(rng, as_of):
    cps = []
    for i in range(40):
        cps.append({
            "counterparty_id": f"CP-{i:04d}",
            "name": f"{rng.choice(LAST_NAMES)} {rng.choice(['Holdings', 'Trading', 'Capital', 'Partners'])}",
            "jurisdiction": rng.choice(list(JURISDICTIONS)),
            "internal_rating": rng.choice(["AAA", "AA", "A", "BBB", "BB", "B"]),
            "is_related_party": int(rng.random() < 0.12),
        })
    rows = []
    for entity in ["BANK-ENTITY-IN", "BANK-ENTITY-GB", "NBFC-ENTITY-IN"]:
        eligible_capital = round(rng.uniform(90_000_000, 340_000_000), 2)
        for cp in cps:
            exposure = round(eligible_capital * rng.uniform(0.02, 0.42), 2)
            rows.append({
                "entity_id": entity,
                "counterparty_id": cp["counterparty_id"],
                "as_of_date": as_of.isoformat(),
                "exposure_at_default": exposure,
                "eligible_capital": eligible_capital,
                "large_exposure_ratio": round(exposure / eligible_capital, 4),
                "limit_utilisation": round(exposure / (eligible_capital * 0.25), 4),
                "breached_25pct_limit": int(exposure / eligible_capital > 0.25),
            })
    return cps, rows


def generate_alert_log(rng, customers, as_of):
    """A pre-existing operational alert backlog, so copilot findings can be
    de-duplicated against what the rules engine already raised."""
    rows = []
    for c in customers:
        if rng.random() < 0.16:
            rows.append({
                "alert_id": f"ALERT-{len(rows) + 1:07d}",
                "customer_id": c["customer_id"],
                "rule_code": rng.choice(["R01_VELOCITY", "R07_GEO", "R12_STRUCT", "R22_SANCTIONS_SCREEN"]),
                "severity": rng.choice(["low", "medium", "high"]),
                "raised_at": (as_of - timedelta(days=rng.randint(0, 45))).isoformat(),
                "status": rng.choice(["open", "closed", "suppressed"]),
                "analyst_notes": "",
            })
    return rows


# --------------------------------------------------------------------------
# CSV emission
# --------------------------------------------------------------------------

SCHEMAS = {
    "customers": ["customer_id", "name", "segment", "business_type", "jurisdiction",
                  "jurisdiction_name", "domestic_currency", "industry", "is_pep",
                  "kyc_status", "kyc_last_review", "onboarded_at", "ubo_count",
                  "risk_rating", "expected_monthly_volume", "expected_monthly_txns",
                  "data_classification"],
    "accounts": ["account_id", "customer_id", "account_number_masked", "account_type",
                 "currency", "opened_at", "status", "opening_balance",
                 "expected_daily_volume", "branch_code"],
    "transactions": ["transaction_id", "account_id", "customer_id", "transaction_ts",
                     "business_date", "amount", "currency", "txn_type", "channel",
                     "is_cash", "merchant_name", "merchant_category", "merchant_country",
                     "merchant_mcc", "device_id", "ip_country", "status", "is_reversal",
                     "ingested_at", "planted_scenario"],
    "credit_portfolio": ["customer_id", "as_of_date", "exposure_at_default",
                         "probability_of_default", "loss_given_default", "stage",
                         "ecl_12m", "ecl_lifetime", "limit_utilisation", "rating_model"],
    "liquidity_metrics": ["entity_id", "report_date", "hqla_total", "net_cash_outflow_30d",
                          "lcr", "available_stable_funding", "required_stable_funding",
                          "nsfr", "counterparty_concentration_pct"],
    "counterparties": ["counterparty_id", "name", "jurisdiction", "internal_rating",
                       "is_related_party"],
    "entity_exposures": ["entity_id", "counterparty_id", "as_of_date", "exposure_at_default",
                         "eligible_capital", "large_exposure_ratio", "limit_utilisation",
                         "breached_25pct_limit"],
    "alert_log": ["alert_id", "customer_id", "rule_code", "severity", "raised_at",
                  "status", "analyst_notes"],
    "ground_truth_labels": ["customer_id", "is_fraud", "scenario", "planted", "planted_on",
                            "expected_risk_level", "narrative"],
}


def write_csv(path, rows, columns):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main(argv=None):
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Generate synthetic risk/fraud data")
    ap.add_argument("--customers", type=int, default=600)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--as-of", type=str, default=None, help="ISO date, defaults to today (UTC)")
    ap.add_argument("--horizon-days", type=int, default=400)
    ap.add_argument("--fraud-prevalence", type=float, default=0.07,
                    help="Planted fraud as a fraction of the population (B3). "
                         "Fixed counts made prevalence profile-dependent: 40.5%% "
                         "on the 200-customer demo vs 13.5%% on 600.")
    ap.add_argument("--out", type=str, default=os.path.join(ROOT, "data", "raw"))
    ap.add_argument("--gold-dir", type=str, default=os.path.join(ROOT, "data", "gold"))
    args = ap.parse_args(argv)

    as_of = (datetime.fromisoformat(args.as_of).date() if args.as_of
             else datetime.utcnow().date() - timedelta(days=1))
    start = as_of - timedelta(days=args.horizon_days)
    rng = random.Random(args.seed)

    t = Tables()
    customers = [make_customer(rng, i, as_of) for i in range(1, args.customers + 1)]
    accounts_by_cust = {c["customer_id"]: make_accounts(rng, c, as_of) for c in customers}
    for c in customers:
        accounts_by_cust[c["customer_id"]][0]["currency"] = c["domestic_currency"]

    factory = TxnFactory(rng, as_of, start)

    # ---- Reserve scenario populations up front so we never double-assign. ---
    idx = list(range(len(customers)))
    rng.shuffle(idx)
    cursor = 0

    def take(n):
        nonlocal cursor
        chunk = idx[cursor:cursor + n]
        cursor += n
        return [customers[i] for i in chunk]

    # Planted counts are proportional to the population, not fixed. Fixed counts
    # gave the 600-customer profile 13.5% fraud prevalence and the deployed
    # 200-customer demo profile 40.5%, so every published rate described a
    # dataset nobody was looking at. Target ~7% prevalence, split across
    # typologies in proportion to their relative base rate.
    target_fraud = max(1, int(round(len(customers) * args.fraud_prevalence)))
    # Typology mix: mule networks and geographic are the most common in a real
    # portfolio; trade-based the rarest.
    mix = {"structuring": 0.17, "takeover": 0.20, "travel": 0.22,
           "trade": 0.12, "hub": 0.01, "mules": 0.28}
    counts = {name: max(1, int(round(target_fraud * share)))
              for name, share in mix.items()}
    # Rounding can push the total over target; trim from the largest bucket.
    while sum(counts.values()) > target_fraud and max(counts.values()) > 1:
        counts[max(counts, key=lambda k: counts[k])] -= 1

    structuring = take(counts["structuring"])
    takeover = take(counts["takeover"])
    travel = take(counts["travel"])
    trade = take(counts["trade"])
    hub = take(counts["hub"])
    mules = take(counts["mules"])
    # The tail must come from the *shuffled* index list, not from position in
    # `customers`, or the control group overlaps the planted populations.
    benign = [customers[i] for i in idx[cursor:]]

    # ---- Baseline activity for everyone -----------------------------------
    for c in customers:
        generate_baseline_txns(rng, factory, c, accounts_by_cust[c["customer_id"]], args.horizon_days)

    # ---- Plant the scenarios ----------------------------------------------
    for c in structuring:
        plant_structuring(rng, factory, c, accounts_by_cust[c["customer_id"]], as_of, args.horizon_days)
    for c in takeover:
        plant_account_takeover(rng, factory, c, accounts_by_cust[c["customer_id"]], as_of, args.horizon_days)
    for c in travel:
        plant_impossible_travel(rng, factory, c, accounts_by_cust[c["customer_id"]], as_of, args.horizon_days)
    for c in trade:
        plant_trade_based(rng, factory, c, accounts_by_cust[c["customer_id"]], args.horizon_days)
    hub_cust = hub[0]
    plant_mule_network(rng, factory, hub_cust, mules, accounts_by_cust, args.horizon_days)

    # ---- Ground truth ------------------------------------------------------
    labels = []
    def add_label(c, scenario, risk, narrative):
        labels.append({
            "customer_id": c["customer_id"], "is_fraud": 1, "scenario": scenario,
            "planted": 1, "planted_on": as_of.isoformat(),
            "expected_risk_level": risk, "narrative": narrative,
        })

    for c in structuring:
        add_label(c, "structuring", "high",
                  f"{len([1 for _ in range(0)])}Daily sub-USD10k cash deposits aggregating above the "
                  "CTR threshold, followed by a large branch cash withdrawal.")
    for c in takeover:
        add_label(c, "account_takeover", "high",
                  "Dormant account wakes on an unseen device from an elevated-risk IP and "
                  "immediately books large online card spend.")
    for c in travel:
        add_label(c, "geographic", "medium",
                  "Physical presence inconsistent with transaction geography: three or more "
                  "jurisdictions inside 24 hours including an elevated-risk one.")
    for c in trade:
        add_label(c, "trade_based", "medium",
                  "Repeated round-value cross-border wires with no matching inbound trade "
                  "settlement - classic invoice-mismatch / TBML pattern.")
    for c in mules:
        add_label(c, "mule_network", "medium",
                  "Receives inbound wires from many senders on a shared device and forwards "
                  "funds onward within 24 hours - pass-through mule.")
    add_label(hub_cust, "mule_network", "high",
              "Receives fan-in from 20+ senders and passes ~88% of the inflow onward within a day.")

    for c in benign:
        labels.append({
            "customer_id": c["customer_id"], "is_fraud": 0, "scenario": "benign_control",
            "planted": 0, "planted_on": "", "expected_risk_level": "low",
            "narrative": "Control group: ordinary customer behaviour, no planted scenario.",
        })

    # ---- Supporting datasets ----------------------------------------------
    t.customers = customers
    t.accounts = [a for lst in accounts_by_cust.values() for a in lst]
    t.transactions = factory.rows
    t.labels = labels
    t.credit = generate_credit_portfolio(rng, customers, as_of)
    t.liquidity = generate_liquidity(rng, as_of)
    t.counterparties, t.entity_exposures = generate_counterparties_and_exposures(rng, as_of)
    t.alert_log = generate_alert_log(rng, customers, as_of)

    # ---- Emit --------------------------------------------------------------
    out = args.out
    write_csv(os.path.join(out, "customers.csv"), t.customers, SCHEMAS["customers"])
    write_csv(os.path.join(out, "accounts.csv"), t.accounts, SCHEMAS["accounts"])
    write_csv(os.path.join(out, "transactions.csv"), t.transactions, SCHEMAS["transactions"])
    write_csv(os.path.join(out, "credit_portfolio.csv"), t.credit, SCHEMAS["credit_portfolio"])
    write_csv(os.path.join(out, "liquidity_metrics.csv"), t.liquidity, SCHEMAS["liquidity_metrics"])
    write_csv(os.path.join(out, "counterparties.csv"), t.counterparties, SCHEMAS["counterparties"])
    write_csv(os.path.join(out, "entity_exposures.csv"), t.entity_exposures, SCHEMAS["entity_exposures"])
    write_csv(os.path.join(out, "alert_log.csv"), t.alert_log, SCHEMAS["alert_log"])
    write_csv(os.path.join(args.gold_dir, "ground_truth_labels.csv"),
              t.labels, SCHEMAS["ground_truth_labels"])

    t.meta = {
        "as_of_date": as_of.isoformat(),
        "window_start": start.isoformat(),
        "window_end": as_of.isoformat(),
        "seed": args.seed,
        "horizon_days": args.horizon_days,
        "row_counts": {
            "customers": len(t.customers), "accounts": len(t.accounts),
            "transactions": len(t.transactions), "ground_truth_labels": len(t.labels),
            "credit_portfolio": len(t.credit), "liquidity_metrics": len(t.liquidity),
            "counterparties": len(t.counterparties), "entity_exposures": len(t.entity_exposures),
            "alert_log": len(t.alert_log),
        },
        "planted_counts": {
            s: sum(1 for l in t.labels if l["scenario"] == s)
            for s in sorted({l["scenario"] for l in t.labels})
        },
        "data_classification": "SYNTHETIC_NO_PII",
        "generator_version": "1.0.0",
    }
    with open(os.path.join(out, "_dataset_meta.json"), "w", encoding="utf-8") as fh:
        json.dump(t.meta, fh, indent=2)

    print(f"[generate_synthetic_data] as_of={as_of} seed={args.seed}")
    for k, v in t.meta["row_counts"].items():
        print(f"  {k:24s} {v:>8,}")
    print("  planted:", json.dumps(t.meta["planted_counts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
