"""Evidence gatherer: builds the reproducible evidence set behind a finding.

An evidence set must satisfy three properties before a finding is allowed to
become a filing (internal policy INT-FRAUD-01, clauses FILE-2.1.2 / SAR-3.2.2):

1. **Addressable** - every item has a stable identifier, so a filing can be
   regenerated exactly from the record set five years later.
2. **Hashed** - content hashes prove the evidence has not been edited since
   the finding was raised.
3. **Non-trivial** - fewer than three items, or low source quality, downgrades
   the case to human review instead of silently producing a clean report.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from backend.config import load_config

EVIDENCE_SOURCES = {
    "TRANSACTION": "transactions",
    "ACCOUNT": "accounts",
    "CUSTOMER_PROFILE": "customers",
    "CREDIT": "credit_portfolio",
    "POLICY_CLAUSE": "document_chunks",
    "ALERT": "alert_log",
}


def content_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class EvidenceGathererAgent:
    def __init__(self, warehouse, retrieval=None, config: Optional[Dict[str, Any]] = None):
        self.wh = warehouse
        self.retrieval = retrieval
        self.cfg = config or load_config()

    # -- collection ---------------------------------------------------------
    def transactions(self, txn_ids: Iterable[str], limit: int = 100) -> List[Dict[str, Any]]:
        ids = [t for t in dict.fromkeys(txn_ids) if t][:limit]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return self.wh.query(f"""
            SELECT transaction_id, account_id, customer_id, transaction_ts, business_date,
                   amount, currency, txn_type, channel, is_cash, merchant_name,
                   merchant_category, merchant_country, device_id, ip_country, status
            FROM transactions
            WHERE transaction_id IN ({placeholders})
            ORDER BY transaction_ts
        """, ids)

    def customer_profile(self, customer_id: str) -> Dict[str, Any]:
        profile = self.wh.one(
            "SELECT customer_id, name, segment, jurisdiction, is_pep, kyc_status,"
            " kyc_last_review, onboarded_at, ubo_count, risk_rating, expected_monthly_volume,"
            " domestic_currency, data_classification FROM customers WHERE customer_id = ?",
            (customer_id,)) or {}
        accounts = self.wh.query(
            "SELECT account_id, account_number_masked, account_type, currency, opened_at,"
            " status, opening_balance FROM accounts WHERE customer_id = ?", (customer_id,))
        credit = self.wh.one(
            "SELECT exposure_at_default, probability_of_default, loss_given_default,"
            " ecl_12m, stage, limit_utilisation FROM credit_portfolio WHERE customer_id = ?",
            (customer_id,)) or {}
        existing_alerts = self.wh.query(
            "SELECT alert_id, rule_code, severity, raised_at, status FROM alert_log"
            " WHERE customer_id = ? ORDER BY raised_at DESC", (customer_id,))
        profile["accounts"] = accounts
        profile["credit"] = credit
        profile["existing_alerts"] = existing_alerts
        return profile

    def clauses(self, clause_refs: Iterable[str]) -> List[Dict[str, Any]]:
        refs = [r for r in dict.fromkeys(clause_refs) if r]
        if not refs:
            return []
        out = []
        for ref in refs:
            if self.retrieval is not None:
                clause = self.retrieval.get_clause(ref)
            else:
                clause = self.wh.one(
                    "SELECT doc_id, framework, title, jurisdiction, section, clause_ref,"
                    " title_text, body, effective_date FROM document_chunks WHERE clause_ref = ?",
                    (ref,))
            if clause:
                out.append({
                    "clause_ref": clause["clause_ref"],
                    "doc_id": clause["doc_id"],
                    "framework": clause["framework"],
                    "title": clause.get("title_text", ""),
                    "text": clause["body"],
                    "citation": clause.get("citation")
                              or f"{clause['doc_id']} s.{clause['section']} {clause['clause_ref']}",
                    "effective_date": clause.get("effective_date", ""),
                })
        return out

    # -- assembly -----------------------------------------------------------
    def build(self, customer_id: str, finding: Dict[str, Any],
              clause_refs: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        txn_ids = list(finding.get("evidence_txn_ids") or [])
        transactions = self.transactions(txn_ids)
        profile = self.customer_profile(customer_id)
        refs = list(clause_refs or [])
        if not refs:
            refs = _clause_refs_from_signals(finding)
        clause_evidence = self.clauses(refs)

        items: List[Dict[str, Any]] = []
        for t in transactions:
            items.append({
                "evidence_id": f"EV-TXN-{t['transaction_id']}",
                "source": "TRANSACTION",
                "source_ref": t["transaction_id"],
                "summary": (f"{t['txn_type']} of {t['currency']} {t['amount']:,.2f} via "
                            f"{t['channel']} in {t['merchant_country']} on "
                            f"{t['business_date']} (device {t['device_id']}, IP "
                            f"{t['ip_country']})"),
                "content_hash": content_hash(t),
                "payload": t,
            })
        for c in clause_evidence:
            items.append({
                "evidence_id": f"EV-CLAUSE-{c['clause_ref']}",
                "source": "POLICY_CLAUSE",
                "source_ref": c["clause_ref"],
                "summary": f"{c['citation']}: {c['title']}",
                "content_hash": content_hash(c),
                "payload": c,
            })
        items.append({
            "evidence_id": f"EV-PROFILE-{customer_id}",
            "source": "CUSTOMER_PROFILE",
            "source_ref": customer_id,
            "summary": (f"{profile.get('name')} ({profile.get('segment')}, "
                        f"{profile.get('jurisdiction')}), KYC {profile.get('kyc_status')}, "
                        f"{len(profile.get('accounts', []))} account(s)"),
            "content_hash": content_hash(profile),
            "payload": profile,
        })
        for alert in profile.get("existing_alerts", []):
            items.append({
                "evidence_id": f"EV-ALERT-{alert['alert_id']}",
                "source": "ALERT",
                "source_ref": alert["alert_id"],
                "summary": (f"Existing operational alert {alert['rule_code']} "
                            f"({alert['severity']}) raised {alert['raised_at']}, "
                            f"status {alert['status']}"),
                "content_hash": content_hash(alert),
                "payload": alert,
            })

        quality, quality_reasons = self.assess_quality(transactions, clause_evidence, finding)

        by_source: Dict[str, int] = {}
        for it in items:
            by_source[it["source"]] = by_source.get(it["source"], 0) + 1

        return {
            "customer_id": customer_id,
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "as_of_date": self.wh.as_of(),
            "data_fingerprint": self.wh.source_fingerprint(),
            "item_count": len(items),
            "items_by_source": by_source,
            "items": items,
            "transaction_count": len(transactions),
            "clause_count": len(clause_evidence),
            "evidence_quality": quality,
            "quality_reasons": quality_reasons,
            "manifest_hash": content_hash([i["content_hash"] for i in items]),
            "replay_instructions": (
                "Re-run `python -m scripts.run_pipeline` against data fingerprint "
                f"{self.wh.source_fingerprint()} and re-hash each evidence_id to "
                "reproduce this evidence set byte for byte."),
        }

    def assess_quality(self, transactions: List[Dict[str, Any]],
                       clauses: List[Dict[str, Any]],
                       finding: Dict[str, Any]) -> tuple:
        reasons = []
        n_txn, n_clause = len(transactions), len(clauses)
        if n_txn == 0:
            reasons.append("no supporting transaction rows")
        elif n_txn < 3:
            reasons.append(f"only {n_txn} supporting transaction row(s)")
        if n_clause == 0:
            reasons.append("no governing clause attached")
        reasons.append(f"{n_txn} transaction row(s), {n_clause} clause(s)")

        score = 0.0
        score += min(0.5, n_txn / 10.0)
        score += 0.3 if n_clause else 0.0
        score += 0.2 * float(finding.get("confidence") or 0.0)
        if score >= 0.7 and n_txn >= 3 and n_clause:
            return "high", reasons
        if score >= 0.4 and (n_txn or n_clause):
            return "medium", reasons
        return "low", reasons

    def write_evidence_pack(self, pack: Dict[str, Any], run_id: str) -> str:
        from backend.config import ROOT
        out_dir = os.path.join(ROOT, self.cfg["paths"]["evidence_dir"], run_id)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{pack['customer_id']}_evidence.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(pack, fh, indent=2, default=str)
        return path



def _clause_refs_from_signals(finding: Dict[str, Any]) -> List[str]:
    refs = []
    for sig in (finding.get("signals") or {}).values():
        for reason in sig.get("reasons", []):
            for token in str(reason).split():
                if "-" in token and any(ch.isdigit() for ch in token) and token[0].isalpha():
                    refs.append(token.strip("().,"))
    return list(dict.fromkeys(refs))
