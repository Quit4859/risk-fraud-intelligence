"""Intent executors: one governed, named plan per intent.

Each executor is a pure function of (warehouse, retrieval, intent). It returns
the SQL it ran, the rows it got, structured blocks for rendering, and any clause
citations. Nothing here writes a filing or escalates anything - those are
separate, explicitly invoked actions so a read-only question can never cause a
side effect.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from backend.agents.intent_router import ExecutionResult, Intent
from backend.config import load_config


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _money(v) -> str:
    return f"USD {_num(v):,.2f}"


class IntentExecutor:
    def __init__(self, warehouse, retrieval=None, agents: Optional[Dict[str, Any]] = None,
                 config: Optional[Dict[str, Any]] = None):
        self.wh = warehouse
        self.retrieval = retrieval
        self.agents = agents or {}
        self.cfg = config or load_config()
        self.detector = self.agents.get("fraud_detector")
        self.policy = self.agents.get("policy_matcher")
        # Set by RiskCopilot so a lazy population scan is attributed to the
        # same run as the question that triggered it.
        self.intent_run_id: Optional[str] = None
        self.population_limit: int = 200
        self._registry: Dict[str, Callable[[Intent], ExecutionResult]] = {
            "portfolio_summary": self.portfolio_summary,
            "top_risks": self.top_risks,
            "customer_risk_profile": self.customer_risk_profile,
            "explain_signal": self.explain_signal,
            "rule_explanation": self.rule_explanation,
            "policy_question": self.policy_question,
            "filing_obligation": self.filing_obligation,
            "draft_filing": self.draft_filing,
            "structuring_exposure": self.structuring_exposure,
            "geographic_concentration": self.geographic_concentration,
            "account_takeover": self.account_takeover,
            "mule_network": self.mule_network,
            "trade_exposure": self.trade_exposure,
            "velocity_check": self.velocity_check,
            "credit_watchlist": self.credit_watchlist,
            "liquidity_position": self.liquidity_position,
            "large_exposures": self.large_exposures,
            "transaction_lookup": self.transaction_lookup,
        }

    def execute(self, intent: Intent) -> ExecutionResult:
        fn = self._registry.get(intent.name, self.policy_question)
        try:
            return fn(intent)
        except Exception as exc:
            return ExecutionResult(intent=intent, error=str(exc), limitations=[str(exc)])

    # -- helpers -----------------------------------------------------------
    def _clause_citations(self, refs: List[str]) -> List[Dict[str, Any]]:
        if self.retrieval is None:
            return []
        out = []
        for ref in refs:
            c = self.retrieval.get_clause(ref)
            if c:
                out.append({"clause_ref": c["clause_ref"], "citation": c["citation"],
                            "doc_id": c["doc_id"], "framework": c["framework"],
                            "title": c["title_text"], "text": c["body"]})
        return out

    def _search_clauses(self, query: str, frameworks=None) -> List[Dict[str, Any]]:
        if self.retrieval is None:
            return []
        return self.retrieval.search(query, frameworks=frameworks)

    def _citations_from_hits(self, hits) -> List[Dict[str, Any]]:
        return [{"clause_ref": h["clause_ref"], "citation": h["citation"],
                 "doc_id": h["doc_id"], "framework": h["framework"],
                 "title": h["title_text"], "text": h["body"],
                 "score": h.get("score")} for h in hits]

    def _customer(self, intent: Intent) -> Optional[Dict[str, Any]]:
        cid = intent.slots.get("customer_id")
        if not cid:
            return None
        return self.wh.one("SELECT * FROM sem_customer_profile WHERE customer_id = ?", (cid,))

    def _window(self, intent: Intent, default_days: int) -> str:
        from datetime import datetime, timedelta
        days = int(intent.slots.get("lookback_days") or default_days)
        as_of = datetime.fromisoformat(self.wh.as_of())
        return (as_of - timedelta(days=days)).date().isoformat()

    # -- portfolio ---------------------------------------------------------
    def _ensure_populated(self) -> bool:
        """Populate the findings table on demand if it is empty.

        Three intents (``top_risks``, ``portfolio_summary``, ``explain_signal``)
        read ``FROM findings``. Before a scan has run that table is empty, so
        they returned no rows, which tripped guardrail A3
        (``answer_has_no_supporting_rows``) and forced an abstention. Abstaining
        was correct - asserting a portfolio position from no findings is not -
        but shipping an app whose headline questions abstain on a cold start is
        a defect. So the executor scans lazily rather than abstaining.

        Guarded by a lock and a no-op when findings already exist, so it costs
        nothing on a warm instance. Returns True if a scan was run.
        """
        copilot = self.agents.get("copilot")
        if copilot is not None:
            # Single lock shared with the boot thread, so the two paths cannot
            # race on the same connection.
            return copilot.ensure_populated(limit=self.population_limit)
        from backend.orchestration.copilot import RiskCopilot
        copilot = RiskCopilot(warehouse=self.wh, config=self.cfg,
                              run_id=self.intent_run_id or "RUN-LAZY")
        return copilot.ensure_populated(limit=self.population_limit)

    def portfolio_summary(self, intent: Intent) -> ExecutionResult:
        self._ensure_populated()
        sql1 = "SELECT risk_level, COUNT(*) n FROM findings GROUP BY risk_level"
        rows = self.wh.query(sql1)
        res = ExecutionResult(intent=intent, sql=[sql1], rows=rows)
        findings = self.agents.get("findings_cache") or []
        dist = {"high": 0, "medium": 0, "low": 0}
        for f in findings:
            dist[f.get("risk_level", "low")] = dist.get(f.get("risk_level", "low"), 0) + 1
        if not findings:
            dist = {r["risk_level"]: r["n"] for r in rows}
        res.metrics = {
            "customers": self.wh.table_count("customers"),
            "accounts": self.wh.table_count("accounts"),
            "transactions": self.wh.table_count("transactions"),
            "as_of_date": self.wh.as_of(),
            "risk_distribution": dist,
        }
        res.blocks = [
            {"type": "kv", "title": "Portfolio position", "items": [
                {"label": "Customers under monitoring", "value": f"{res.metrics['customers']:,}"},
                {"label": "Accounts", "value": f"{res.metrics['accounts']:,}"},
                {"label": "Transactions assessed", "value": f"{res.metrics['transactions']:,}"},
                {"label": "Data as-of", "value": res.metrics["as_of_date"]},
                {"label": "High risk", "value": f"{dist.get('high', 0):,}"},
                {"label": "Medium risk", "value": f"{dist.get('medium', 0):,}"},
                {"label": "Low risk", "value": f"{dist.get('low', 0):,}"},
            ]},
        ]
        return res

    def top_risks(self, intent: Intent) -> ExecutionResult:
        self._ensure_populated()
        limit = int(intent.slots.get("limit") or 10)
        sql = """SELECT customer_id, composite_risk_score, risk_level, confidence,
                        guardrail_status, summary
                 FROM findings
                 WHERE risk_level IN ('high','medium')
                 ORDER BY composite_risk_score DESC LIMIT ?"""
        rows = self.wh.query(sql, (limit,))
        res = ExecutionResult(intent=intent, sql=[sql], rows=rows, row_count=len(rows))
        res.metrics = {"returned": len(rows),
                       "high": sum(1 for r in rows if r["risk_level"] == "high"),
                       "medium": sum(1 for r in rows if r["risk_level"] == "medium")}
        res.blocks = [
            {"type": "table", "title": f"Top {len(rows)} customers by composite risk score",
             "columns": [("customer_id", "Customer"), ("composite_risk_score", "Score"),
                         ("risk_level", "Level"), ("confidence", "Conf.")],
             "rows": rows},
        ]
        if rows:
            clause = self._clause_citations(["ESC-1.1.1", "ESC-1.1.2"])
            res.citations = clause
            res.is_regulatory_claim = True
        return res

    def customer_risk_profile(self, intent: Intent) -> ExecutionResult:
        cid = intent.slots.get("customer_id")
        if not cid:
            return ExecutionResult(
                intent=intent,
                limitations=["No customer id in the question. Ask about a specific "
                             "customer (e.g. CUST-000123) or ask for the top risks."],
                error="missing_slot:customer_id")
        profile = self._customer(intent)
        if not profile:
            return ExecutionResult(intent=intent,
                                   limitations=[f"Customer {cid} is not in the dataset."],
                                   error="customer_not_found")
        sql = "SELECT * FROM sem_customer_profile WHERE customer_id = ?"
        res = ExecutionResult(intent=intent, sql=[sql], rows=[profile], row_count=1)
        finding = None
        if self.detector is not None:
            finding = self.detector.detect_customer(cid)
        stored = self.wh.one("SELECT * FROM findings WHERE customer_id = ?", (cid,))
        res.metrics = {
            "customer_id": cid,
            "name": profile.get("name"),
            "segment": profile.get("segment"),
            "jurisdiction": profile.get("jurisdiction"),
            "kyc_status": profile.get("kyc_status"),
            "is_pep": bool(profile.get("is_pep")),
            "txn_count_90d": profile.get("txn_count_90d"),
            "gross_amount_90d": profile.get("gross_amount_90d"),
            "cash_amount_90d": profile.get("cash_amount_90d"),
            "max_single_txn_90d": profile.get("max_single_txn_90d"),
            "probability_of_default": profile.get("probability_of_default"),
            "ecl_12m": profile.get("ecl_12m"),
            "source_risk_rating": profile.get("source_risk_rating"),
        }
        if finding:
            res.metrics.update({
                "composite_score": finding["composite_score"],
                "risk_level": finding["risk_level"],
                "confidence": finding["confidence"],
                "fired_rules": finding["fired_rules"],
                "primary_reason": finding["primary_reason"],
                "evidence_count": finding["evidence_count"],
            })
        elif stored:
            res.metrics.update({
                "composite_score": stored["composite_risk_score"],
                "risk_level": stored["risk_level"],
                "confidence": stored["confidence"],
                "summary": stored["summary"],
            })
        res.blocks = [
            {"type": "kv", "title": f"Customer {cid}", "items": [
                {"label": "Name", "value": profile.get("name")},
                {"label": "Segment", "value": profile.get("segment")},
                {"label": "Jurisdiction", "value": f"{profile.get('jurisdiction')} "
                                                   f"({profile.get('jurisdiction_name')})"},
                {"label": "KYC status", "value": profile.get("kyc_status")},
                {"label": "Politically exposed", "value": "Yes" if profile.get("is_pep") else "No"},
                {"label": "Existing risk rating", "value": profile.get("source_risk_rating") or "Unrated"},
            ]},
            {"type": "kv", "title": "Activity (last 90 days)", "items": [
                {"label": "Transactions", "value": f"{_num(profile.get('txn_count_90d')):,.0f}"},
                {"label": "Gross value", "value": _money(profile.get("gross_amount_90d"))},
                {"label": "Cash value", "value": _money(profile.get("cash_amount_90d"))},
                {"label": "Largest single transaction",
                 "value": _money(profile.get("max_single_txn_90d"))},
            ]},
            {"type": "kv", "title": "Copilot assessment", "items": [
                {"label": "Composite risk score",
                 "value": f"{_num(res.metrics.get('composite_score')):.3f}"},
                {"label": "Risk level", "value": str(res.metrics.get("risk_level", "not yet assessed"))},
                {"label": "Model confidence",
                 "value": f"{_num(res.metrics.get('confidence')):.2f}"},
                {"label": "Rules fired",
                 "value": ", ".join(res.metrics.get("fired_rules") or []) or "none"},
                {"label": "Evidence items", "value": str(res.metrics.get("evidence_count", 0))},
            ]},
        ]
        if res.metrics.get("primary_reason"):
            res.blocks.append({"type": "text", "title": "Primary driver",
                               "text": res.metrics["primary_reason"]})
        if res.metrics.get("fired_rules"):
            res.citations = self._citations_from_hits(
                self.retrieval.clauses_for_rules(res.metrics["fired_rules"])) \
                if self.retrieval else []
            res.is_regulatory_claim = True
        return res

    def explain_signal(self, intent: Intent) -> ExecutionResult:
        cid = intent.slots.get("customer_id")
        if not cid:
            return ExecutionResult(intent=intent,
                                   limitations=["Name the customer, e.g. "
                                                "'why was CUST-000123 flagged?'"],
                                   error="missing_slot:customer_id")
        sql = "SELECT * FROM findings WHERE customer_id = ?"
        row = self.wh.one(sql, (cid,))
        if not row and self.detector is not None:
            finding = self.detector.detect_customer(cid)
        else:
            finding = {
                "customer_id": cid, "composite_score": row["composite_risk_score"],
                "risk_level": row["risk_level"], "confidence": row["confidence"],
                "fired_rules": json.loads(row["signals_json"] or "{}") and
                              [k for k, v in json.loads(row["signals_json"]).items()
                               if v.get("fired")],
                "signals": json.loads(row["signals_json"] or "{}"),
                "evidence_txn_ids": (json.loads(row["evidence_json"] or "{}")
                                     .get("transaction_ids", [])),
                "evidence_count": 0,
            }
        res = ExecutionResult(intent=intent, sql=[sql] if row else [],
                              rows=[row] if row else [], row_count=1 if row else 0)
        res.metrics = {"customer_id": cid, "risk_level": finding.get("risk_level"),
                       "composite_score": finding.get("composite_score"),
                       "confidence": finding.get("confidence"),
                       "fired_rules": finding.get("fired_rules") or []}
        rules = [r for r in (finding.get("fired_rules") or [])]
        for code, sig in (finding.get("signals") or {}).items():
            if rules and code not in rules:
                continue
            res.blocks.append({
                "type": "bullets", "title": f"{code} (score {sig.get('score')})",
                "items": list(sig.get("reasons", [])) or ["rule did not fire"],
            })
        if not res.blocks:
            res.limitations.append("No stored finding for this customer; "
                                   "run the detection job first.")
        if finding.get("fired_rules") and self.retrieval:
            res.citations = self._citations_from_hits(
                self.retrieval.clauses_for_rules(finding["fired_rules"]))
            res.is_regulatory_claim = True
        return res

    def rule_explanation(self, intent: Intent) -> ExecutionResult:
        code = intent.slots.get("rule_code")
        if not code:
            return ExecutionResult(
                intent=intent,
                limitations=["Name the rule, e.g. 'explain the structuring rule'."],
                error="missing_slot:rule_code")
        sql = "SELECT binding_rule_code, obligation, policy_id, framework, name," \
              " key_thresholds, source_doc_id, description FROM policies" \
              " WHERE binding_rule_code = ?"
        rows = self.wh.query(sql, (code,))
        res = ExecutionResult(intent=intent, sql=[sql], rows=rows, row_count=len(rows))
        if not rows:
            res.limitations.append(f"No policy is bound to rule {code}.")
        for r in rows:
            res.blocks.append({"type": "kv", "title": f"{r['policy_id']} - {r['name']}", "items": [
                {"label": "Framework", "value": r["framework"]},
                {"label": "Obligation", "value": r["obligation"]},
                {"label": "Thresholds", "value": json.dumps(json.loads(r["key_thresholds"] or "{}"))},
                {"label": "Description", "value": r["description"]},
            ]})
        hits = self.retrieval.clauses_for_rules([code]) if self.retrieval else []
        res.citations = self._citations_from_hits(hits)
        res.is_regulatory_claim = True
        res.metrics = {"rule_code": code, "policies_bound": len(rows)}
        return res

    def policy_question(self, intent: Intent) -> ExecutionResult:
        question = intent.slots.get("_question", "")
        frameworks = [intent.slots["framework"]] if intent.slots.get("framework") else None
        hits = self._search_clauses(question or "regulatory obligation threshold", frameworks)
        res = ExecutionResult(intent=intent, rows=hits, row_count=len(hits),
                              citations=self._citations_from_hits(hits),
                              is_regulatory_claim=True)
        res.metrics = {"retrieved_clauses": len(hits),
                       "top_score": hits[0]["score"] if hits else 0.0}
        for h in hits[:5]:
            res.blocks.append({"type": "clause", "title": h["citation"],
                               "text": f"{h['title_text']} - {h['body']}"})
        if not hits:
            res.limitations.append("No clause in the policy corpus matched the question.")
        return res

    def filing_obligation(self, intent: Intent) -> ExecutionResult:
        cid = intent.slots.get("customer_id")
        if not cid:
            return ExecutionResult(
                intent=intent,
                limitations=["Name the customer, e.g. 'do I need to file a SAR for "
                             "CUST-000123?'"],
                error="missing_slot:customer_id")
        if self.policy is None:
            return ExecutionResult(intent=intent, error="policy matcher unavailable")
        finding = (self.detector.detect_customer(cid) if self.detector else None)
        rules = finding["fired_rules"] if finding else []
        match = self.policy.match(cid, rules)
        metrics = {
            "composite_risk_score": finding["composite_score"] if finding else None,
            "cash_aggregate_72h": _cash_aggregate(self.wh, cid),
        }
        breached = self.policy.resolve_obligations(match, metrics)
        res = ExecutionResult(intent=intent, rows=match["matched_policies"],
                              row_count=len(match["matched_policies"]),
                              citations=match["governing_clauses"],
                              is_regulatory_claim=True)
        res.metrics = {
            "customer_id": cid,
            "obligations_total": match["obligations_total"],
            "obligations_triggered": len(breached),
            "filings_indicated": sorted({b["filing_type"] for b in breached}),
        }
        if breached:
            res.blocks.append({"type": "table", "title": "Obligations triggered",
                               "columns": [("filing_type", "Filing"), ("policy_id", "Policy"),
                                           ("metric", "Metric"), ("actual", "Actual"),
                                           ("threshold", "Threshold")],
                               "rows": breached})
        else:
            res.blocks.append({"type": "text", "title": "Obligations triggered",
                               "text": "No filing obligation is currently triggered on the "
                                       "evidence available."})
        return res

    def draft_filing(self, intent: Intent) -> ExecutionResult:
        cid = intent.slots.get("customer_id")
        ftype = intent.slots.get("filing_type")
        if not cid:
            return ExecutionResult(intent=intent,
                                   limitations=["Name the customer to draft a filing for."],
                                   error="missing_slot:customer_id")
        if not ftype:
            return ExecutionResult(
                intent=intent,
                limitations=["Name the filing type: SAR, CTR or STR."],
                error="missing_slot:filing_type")
        return ExecutionResult(
            intent=intent,
            limitations=["Filing generation is an action, not an answer. Use the "
                         "`file` command or the Generate Filing action in the app, which "
                         "runs the governed pipeline and records the four-eyes control."],
            error="action_requires_explicit_invocation")

    # -- domain questions ---------------------------------------------------
    def structuring_exposure(self, intent: Intent) -> ExecutionResult:
        start = self._window(intent, 7)
        sql = """SELECT customer_id, deposit_count, aggregate_amount, avg_deposit,
                        window_start, window_end
                 FROM sem_cash_72h_aggregation
                 WHERE rn = 1 AND DATE(window_start) >= ?
                 ORDER BY aggregate_amount DESC LIMIT 50"""
        rows = self.wh.query(sql, (start,))
        res = ExecutionResult(intent=intent, sql=[sql], rows=rows, row_count=len(rows))
        res.metrics = {
            "window_start": start,
            "customers_with_aggregation": len(rows),
            "total_aggregate": round(sum(_num(r["aggregate_amount"]) for r in rows), 2),
            "largest_aggregate": _num(rows[0]["aggregate_amount"]) if rows else 0.0,
            "reporting_threshold": self.cfg["detection"]["structuring"]["ctr_threshold"],
        }
        res.blocks = [{
            "type": "table", "title": f"Sub-threshold cash aggregating above the filing "
                                       f"line since {start}",
            "columns": [("customer_id", "Customer"), ("deposit_count", "Deposits"),
                        ("aggregate_amount", "Aggregate"), ("avg_deposit", "Avg/deposit")],
            "rows": rows,
        }]
        res.citations = self._clause_citations(["STR-2.1.2", "CTR-1.2.2", "STR-2.1.1"])
        res.is_regulatory_claim = True
        return res

    def geographic_concentration(self, intent: Intent) -> ExecutionResult:
        start = self._window(intent, 90)
        sql = """SELECT customer_id, txn_count, risk_txns, risk_countries, risk_share,
                        geography_status
                 FROM sem_elevated_risk_exposure
                 WHERE risk_share > 0.05 AND customer_id IN
                       (SELECT customer_id FROM transactions
                        WHERE business_date >= ? GROUP BY customer_id)
                 ORDER BY risk_share DESC LIMIT 50"""
        rows = self.wh.query(sql, (start,))
        res = ExecutionResult(intent=intent, sql=[sql], rows=rows, row_count=len(rows))
        res.metrics = {
            "window_days": intent.slots.get("lookback_days") or 90,
            "customers_above_10pct": sum(1 for r in rows if _num(r["risk_share"]) > 0.10),
            "customers_concentrated": sum(1 for r in rows
                                          if r["geography_status"] == "CONCENTRATED"),
        }
        res.blocks = [{"type": "table", "title": "Elevated-risk jurisdiction exposure",
                       "columns": [("customer_id", "Customer"), ("txn_count", "Txns"),
                                   ("risk_txns", "Risk txns"), ("risk_countries", "Countries"),
                                   ("risk_share", "Share"), ("geography_status", "Status")],
                       "rows": rows}]
        res.citations = self._clause_citations(["GEO-2.1.1", "GEO-2.2.1"])
        res.is_regulatory_claim = True
        return res

    def account_takeover(self, intent: Intent) -> ExecutionResult:
        start = self._window(intent, 14)
        sql = """SELECT customer_id, COUNT(*) AS txns, COUNT(DISTINCT device_id) AS devices,
                        SUM(amount) AS amount, MIN(business_date) AS first_seen
                 FROM transactions
                 WHERE status = 'posted' AND business_date >= ?
                   AND ip_country IN ('NG','RU','IR','KP','SY','PA','KY','VG')
                   AND customer_id IN
                       (SELECT customer_id FROM sem_elevated_risk_exposure)
                 GROUP BY customer_id
                 ORDER BY amount DESC LIMIT 50"""
        rows = self.wh.query(sql, (start,))
        res = ExecutionResult(intent=intent, sql=[sql], rows=rows, row_count=len(rows))
        res.metrics = {"window_start": start, "customers_matched": len(rows),
                       "total_amount": round(sum(_num(r["amount"]) for r in rows), 2)}
        res.blocks = [{"type": "table", "title": "Suspected account-takeover activity",
                       "columns": [("customer_id", "Customer"), ("txns", "Txns"),
                                   ("devices", "Devices"), ("amount", "Value"),
                                   ("first_seen", "First seen")],
                       "rows": rows}]
        res.citations = self._clause_citations(["GEO-2.2.2", "GEO-2.2.1"])
        res.is_regulatory_claim = True
        return res

    def mule_network(self, intent: Intent) -> ExecutionResult:
        sql = "SELECT * FROM sem_mule_network_clusters ORDER BY customers_on_device DESC LIMIT 25"
        rows = self.wh.query(sql)
        members = []
        for r in rows[:5]:
            m = self.wh.query(
                "SELECT DISTINCT customer_id FROM transactions WHERE device_id = ?"
                " ORDER BY customer_id LIMIT 60", (r["device_id"],))
            members.append({"device_id": r["device_id"],
                            "customers": [x["customer_id"] for x in m]})
        res = ExecutionResult(intent=intent, sql=[sql], rows=rows, row_count=len(rows))
        res.metrics = {
            "clusters": len(rows),
            "largest_cluster": rows[0]["customers_on_device"] if rows else 0,
            "largest_cluster_value": _num(rows[0]["total_amount"]) if rows else 0.0,
        }
        res.blocks = [
            {"type": "table", "title": "Shared device clusters (mule network candidates)",
             "columns": [("device_id", "Device"), ("customers_on_device", "Customers"),
                         ("txn_count", "Txns"), ("total_amount", "Value"),
                         ("first_seen", "First seen"), ("last_seen", "Last seen")],
             "rows": rows},
            {"type": "bullets", "title": "Largest cluster membership",
             "items": [f"{m['device_id']}: {len(m['customers'])} customers "
                       f"({', '.join(m['customers'][:8])}{'...' if len(m['customers']) > 8 else ''})"
                       for m in members] or ["No shared-device cluster found."]},
        ]
        res.citations = self._clause_citations(["STR-2.1.3", "STR-2.2.1"])
        res.is_regulatory_claim = True
        return res

    def trade_exposure(self, intent: Intent) -> ExecutionResult:
        start = self._window(intent, 120)
        sql = """SELECT customer_id, COUNT(*) AS wires, SUM(amount) AS outbound,
                        COUNT(DISTINCT merchant_country) AS countries
                 FROM transactions
                 WHERE status = 'posted' AND channel = 'WIRE' AND txn_type = 'wire_out'
                   AND business_date >= ? AND amount % 5000 = 0
                 GROUP BY customer_id
                 HAVING COUNT(*) >= 3
                 ORDER BY outbound DESC LIMIT 50"""
        rows = self.wh.query(sql, (start,))
        res = ExecutionResult(intent=intent, sql=[sql], rows=rows, row_count=len(rows))
        res.metrics = {"window_start": start, "customers_matched": len(rows),
                       "total_outbound": round(sum(_num(r["outbound"]) for r in rows), 2)}
        res.blocks = [{"type": "table", "title": "Round-value cross-border wire activity",
                       "columns": [("customer_id", "Customer"), ("wires", "Wires"),
                                   ("outbound", "Outbound"), ("countries", "Countries")],
                       "rows": rows}]
        res.citations = self._clause_citations(["CDD-1.2.1", "STR-2.1.1"])
        res.is_regulatory_claim = True
        return res

    def velocity_check(self, intent: Intent) -> ExecutionResult:
        start = self._window(intent, 30)
        sql = """SELECT customer_id, SUM(gross_amount) AS amount, COUNT(*) AS days,
                        MAX(gross_amount) AS peak_day_amount
                 FROM sem_daily_activity
                 WHERE business_date >= ?
                 GROUP BY customer_id
                 ORDER BY amount DESC LIMIT 50"""
        rows = self.wh.query(sql, (start,))
        res = ExecutionResult(intent=intent, sql=[sql], rows=rows, row_count=len(rows))
        res.metrics = {"window_start": start, "customers": len(rows)}
        res.blocks = [{"type": "table", "title": f"Highest aggregate activity since {start}",
                       "columns": [("customer_id", "Customer"), ("amount", "Total"),
                                   ("days", "Active days"), ("peak_day_amount", "Peak day")],
                       "rows": rows}]
        res.citations = self._clause_citations(["MON-3.1.1", "MON-3.1.2"])
        res.is_regulatory_claim = True
        return res

    def credit_watchlist(self, intent: Intent) -> ExecutionResult:
        status = intent.slots.get("credit_status")
        sql = "SELECT * FROM sem_credit_watchlist WHERE probability_of_default > 0.05" \
              " ORDER BY probability_of_default DESC LIMIT 50"
        rows = self.wh.query(sql)
        if intent.slots.get("jurisdiction"):
            rows = [r for r in rows if r["jurisdiction"] == intent.slots["jurisdiction"]]
        res = ExecutionResult(intent=intent, sql=[sql], rows=rows, row_count=len(rows))
        res.metrics = {
            "vulnerable_obligors": sum(1 for r in rows if r["credit_status"] == "VULNERABLE_OBLIGOR"),
            "watch": sum(1 for r in rows if r["credit_status"] == "WATCH"),
            "total_exposure": round(sum(_num(r["exposure_at_default"]) for r in rows), 2),
            "total_ecl_12m": round(sum(_num(r["ecl_12m"]) for r in rows), 2),
        }
        res.blocks = [{"type": "table", "title": "Credit watchlist (PD above 5%)",
                       "columns": [("customer_id", "Customer"), ("segment", "Segment"),
                                   ("probability_of_default", "PD"),
                                   ("exposure_at_default", "EAD"),
                                   ("ecl_12m", "ECL 12m"), ("credit_status", "Status")],
                       "rows": rows}]
        res.citations = self._clause_citations(["ECL-4.1.2", "ECL-4.1.1"])
        res.is_regulatory_claim = True
        return res

    def liquidity_position(self, intent: Intent) -> ExecutionResult:
        sql = "SELECT * FROM sem_liquidity_position ORDER BY entity_id"
        rows = self.wh.query(sql)
        breaches = self.wh.query(
            "SELECT * FROM sem_large_exposures WHERE limit_status != 'WITHIN_LIMIT'")
        res = ExecutionResult(intent=intent, sql=[sql], rows=rows, row_count=len(rows))
        res.metrics = {
            "entities": len(rows),
            "lcr_breaches": sum(1 for r in rows if r["lcr_status"] == "LCR_BREACH"),
            "nsfr_breaches": sum(1 for r in rows if r["nsfr_status"] == "NSFR_BREACH"),
            "large_exposure_exceptions": len(breaches),
        }
        res.blocks = [
            {"type": "table", "title": "Liquidity position",
             "columns": [("entity_id", "Entity"), ("lcr", "LCR"), ("nsfr", "NSFR"),
                         ("hqla_total", "HQLA"), ("net_cash_outflow_30d", "30d outflow"),
                         ("lcr_status", "LCR status"), ("nsfr_status", "NSFR status")],
             "rows": rows},
            {"type": "table", "title": "Large exposure exceptions",
             "columns": [("entity_id", "Entity"), ("counterparty_name", "Counterparty"),
                         ("large_exposure_ratio", "Ratio"),
                         ("limit_status", "Status")],
             "rows": breaches[:20]},
        ]
        res.citations = self._clause_citations(["LCR-3.1.1", "NSFR-3.2.1", "LE-2.1.1"])
        res.is_regulatory_claim = True
        return res

    def large_exposures(self, intent: Intent) -> ExecutionResult:
        sql = ("SELECT * FROM sem_large_exposures WHERE limit_status != 'WITHIN_LIMIT'"
               " ORDER BY large_exposure_ratio DESC LIMIT 50")
        rows = self.wh.query(sql)
        res = ExecutionResult(intent=intent, sql=[sql], rows=rows, row_count=len(rows))
        res.metrics = {
            "exceptions": len(rows),
            "breaches": sum(1 for r in rows if r["limit_status"] == "BREACH"),
            "board_reports": sum(1 for r in rows if r["limit_status"] == "BOARD_REPORT"),
            "largest_ratio": _num(rows[0]["large_exposure_ratio"]) if rows else 0.0,
        }
        res.blocks = [{"type": "table", "title": "Large exposure exceptions (>80% of the 25% limit)",
                       "columns": [("entity_id", "Entity"), ("counterparty_name", "Counterparty"),
                                   ("exposure_at_default", "Exposure"),
                                   ("large_exposure_ratio", "Ratio"),
                                   ("limit_status", "Status")],
                       "rows": rows}]
        res.citations = self._clause_citations(["LE-2.1.1", "LE-2.1.2"])
        res.is_regulatory_claim = True
        return res

    def transaction_lookup(self, intent: Intent) -> ExecutionResult:
        tid = intent.slots.get("transaction_id")
        cid = intent.slots.get("customer_id")
        if tid:
            sql = "SELECT * FROM transactions WHERE transaction_id = ?"
            rows = self.wh.query(sql, (tid,))
        elif cid:
            sql = ("SELECT transaction_id, business_date, amount, txn_type, channel,"
                   " merchant_country, is_cash, status FROM transactions"
                   " WHERE customer_id = ? AND status = 'posted'"
                   " ORDER BY transaction_ts DESC LIMIT 50")
            rows = self.wh.query(sql, (cid,))
        else:
            return ExecutionResult(
                intent=intent,
                limitations=["Provide a transaction id (TXN-00000123) or a customer id."],
                error="missing_slot:transaction_id|customer_id")
        res = ExecutionResult(intent=intent, sql=[sql], rows=rows, row_count=len(rows))
        res.metrics = {"returned": len(rows)}
        cols = [(k, k.replace("_", " ")) for k in (rows[0].keys() if rows else
                 ["transaction_id", "business_date", "amount", "txn_type"])]
        res.blocks = [{"type": "table", "title": "Transactions", "columns": cols, "rows": rows}]
        if not rows:
            res.limitations.append("No matching transaction was found.")
        return res


def _cash_aggregate(wh, customer_id: str) -> float:
    row = wh.one("SELECT aggregate_amount FROM sem_cash_72h_aggregation"
                 " WHERE customer_id = ? AND rn = 1", (customer_id,))
    return _num(row["aggregate_amount"]) if row else 0.0
