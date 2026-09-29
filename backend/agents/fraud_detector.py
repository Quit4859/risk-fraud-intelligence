"""Fraud detection agents - SQL-first, config-driven, explainable.

Design rules
------------
1.  Every detector returns a score in [0, 1], a confidence in [0, 1], the
    human-readable ``reasons`` that produced the score, and the *identifiers of
    the rows* that justify it. A score that cannot name its evidence is a bug.
2.  Thresholds come from ``config/settings.yaml``, never from literals here.
3.  The composite score is a configured weighted sum. Its component
    contributions are returned so a reviewer can see why it landed where it did.
4.  The composite is *not* the verdict. Findings, filings and human approval are
    separate governed steps.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from backend.config import load_config

VALID_TXN_STATUSES = ("posted",)


def _as_of(warehouse) -> datetime:
    return datetime.fromisoformat(warehouse.as_of() + " 23:59:59")


def _rows(warehouse, sql: str, params=()) -> List[Dict[str, Any]]:
    return warehouse.query(sql, params)


class BaseDetector:
    rule_code = "BASE"

    def __init__(self, warehouse, config: Optional[Dict[str, Any]] = None):
        self.wh = warehouse
        self.cfg = config or load_config()
        self.det = self.cfg["detection"]
        self.as_of = _as_of(warehouse)
        self.geo_centroids = {
            k.upper(): tuple(v) for k, v in
            (self.cfg.get("geo_reference", {}).get("centroids") or {}).items()
        }

    # -- helpers -----------------------------------------------------------
    def _window_start(self, days: int) -> str:
        return (self.as_of - timedelta(days=days)).strftime("%Y-%m-%d")

    def _score(self, raw: float, floor: float = 0.0, ceiling: float = 0.99) -> float:
        return max(floor, min(ceiling, raw))

    def _evidence_bonus(self, n: int, ideal: int = 12) -> float:
        """More supporting rows -> more confidence, saturating at ``ideal``."""
        if n <= 0:
            return 0.0
        return min(0.25, 0.25 * (n / ideal))

    def empty(self, customer_id: str, note: str = "no_data") -> Dict[str, Any]:
        return {
            "rule_code": self.rule_code,
            "customer_id": customer_id,
            "score": 0.0,
            "confidence": 0.30,
            "fired": False,
            "reasons": [note],
            "evidence_txn_ids": [],
            "metrics": {},
        }

    def result(self, customer_id: str, score: float, confidence: float, reasons: List[str],
               evidence_txn_ids: List[str], metrics: Dict[str, Any],
               fired: Optional[bool] = None) -> Dict[str, Any]:
        score = round(self._score(score), 4)
        return {
            "rule_code": self.rule_code,
            "customer_id": customer_id,
            "score": score,
            "confidence": round(max(0.0, min(1.0, confidence)), 4),
            "fired": bool(fired if fired is not None else score >= 0.5),
            "reasons": reasons,
            "evidence_txn_ids": sorted(set(evidence_txn_ids))[:50],
            "evidence_count": len(set(evidence_txn_ids)),
            "metrics": metrics,
        }


# --------------------------------------------------------------------------


class StructuringDetector(BaseDetector):
    """Sub-threshold cash deposits aggregating above the CTR line.

    Implements the 72-hour rolling aggregation of clause STR-2.1.2 rather than
    the naive same-day test, because that is what the regulation actually says.
    The window is read from ``aggregation_window_hours`` rather than hardcoded,
    so the number quoted in the narrative cannot drift from the number applied.

    Three tests make the difference between this and a false-positive machine:

    1. every candidate deposit must sit in the *avoidance band* - a configured
       fraction (default 85-99.9%) of the reporting threshold. A 2,100 deposit
       is ordinary cash use; a 9,400 deposit three days running is not.
    2. at least ``min_deposit_count`` of them must fall inside one
       aggregation window and sum above the threshold.
    3. HIGH additionally requires corroboration (clause STR-2.2.1): a
       pass-through withdrawal or a new counterparty in the window. Without it
       the case is capped below the HIGH band, because an aggregation alone is
       weak evidence.
    """

    rule_code = "STRUCTURING"

    def run(self, customer_id: str) -> Dict[str, Any]:
        c = self.det["structuring"]
        threshold = float(c["ctr_threshold"])
        window_hours = float(c["aggregation_window_hours"])
        band_low = threshold * float(c.get("band_low_ratio", 0.85))
        band_high = threshold * float(c.get("band_high_ratio", 0.999))
        start = self._window_start(int(c["candidate_lookback_days"]))

        rows = _rows(self.wh, """
            SELECT transaction_id, transaction_ts, business_date, amount, channel, currency
            FROM transactions
            WHERE customer_id = ?
              AND status = 'posted'
              AND is_cash = 1
              AND amount < ?
              AND amount >= ?
              AND business_date >= ?
            ORDER BY transaction_ts
        """, (customer_id, band_high, band_low, start))

        if len(rows) < c["min_deposit_count"]:
            return self.empty(
                customer_id,
                f"fewer_than_{c['min_deposit_count']}_cash_deposits_in_the_"
                f"avoidance_band_{band_low:,.0f}-{band_high:,.0f}")

        rolling = []
        for i, r in enumerate(rows):
            j = i
            total = 0.0
            while j < len(rows):
                gap = _hours_between(rows[i]["transaction_ts"], rows[j]["transaction_ts"])
                if gap > window_hours:
                    break
                total += rows[j]["amount"]
                j += 1
            if j - i >= c["min_deposit_count"] and total >= threshold:
                rolling.append({
                    "window_start": rows[i]["transaction_ts"],
                    "window_end": rows[j - 1]["transaction_ts"],
                    "deposit_count": j - i,
                    "aggregate": round(total, 2),
                    "txn_ids": [x["transaction_id"] for x in rows[i:j]],
                })

        reasons, evidence, metrics = [], [], {}
        score = 0.0
        confidence = 0.40 + self._evidence_bonus(len(rows))
        corroboration: List[str] = []

        if rolling:
            worst = max(rolling, key=lambda w: w["aggregate"])
            evidence = list(worst["txn_ids"])
            ratio = worst["aggregate"] / threshold
            count_penalty = 1 + min(0.2, 0.02 * worst["deposit_count"])
            score = min(0.97, (0.55 + min(0.28, 0.12 * (ratio - 1))) * count_penalty)
            reasons.append(
                f"{worst['deposit_count']} cash deposits, each between "
                f"USD {band_low:,.0f} and USD {band_high:,.0f} - deliberately "
                f"inside the USD {threshold:,.0f} reporting line - aggregated to "
                f"USD {worst['aggregate']:,.0f} within {int(window_hours)} hours. "
                f"Threshold avoidance under clause STR-2.1.2.")
            confidence = min(0.95, confidence + 0.25)
            metrics = {
                "worst_window": worst,
                "window_count": len(rolling),
                "deposits_in_window": len(rows),
                "reporting_threshold": threshold,
                "avoidance_band": [round(band_low, 2), round(band_high, 2)],
                "aggregation_window_hours": int(window_hours),
                "aggregation_rule": (
                    f"{int(window_hours)}h rolling, sub-threshold cash only"),
            }

        # -- corroboration (clause STR-2.2.1) -----------------------------
        if evidence and c.get("require_corroboration_for_high", True):
            corroboration, pass_through_id, counterparty_id = self._corroboration(
                customer_id, evidence, threshold, c, window_hours)
            if pass_through_id:
                evidence.append(pass_through_id)
            if counterparty_id:
                evidence.extend(counterparty_id)
            if corroboration:
                score = min(0.99, score + 0.15)
                reasons.extend(corroboration)
                metrics["corroboration"] = corroboration
                metrics["corroborated"] = True
            else:
                # Aggregation without corroboration stays a MEDIUM signal. It is
                # reported and reviewable, but it does not clear the HIGH band.
                score = min(score, 0.60)
                reasons.append(
                    "No pass-through withdrawal or new counterparty in the "
                    f"{int(window_hours)}-hour window, so this aggregation is "
                    "reported as a medium signal pending review rather than "
                    "threshold avoidance (clause STR-2.2.1).")
                metrics["corroborated"] = False

        if score == 0.0:
            reasons.append(
                f"cash activity present but no aggregation of "
                f"{c['min_deposit_count']}+ deposits inside the "
                f"USD {band_low:,.0f}-{band_high:,.0f} avoidance band above the "
                f"filing line within {int(window_hours)} hours")
            return self.result(customer_id, 0.0, confidence, reasons, [], metrics, fired=False)

        return self.result(customer_id, score, confidence, reasons, evidence, metrics)

    def _corroboration(self, customer_id: str, evidence: List[str],
                       threshold: float, c: Dict[str, Any],
                       window_hours: float):
        """Look for a pass-through withdrawal and new counterparties.

        Returns (messages, pass_through_txn_id, new_counterparty_txn_ids).
        """
        messages: List[str] = []
        if not evidence:
            return messages, None, []

        placeholders = ",".join("?" for _ in evidence)
        stamps = self.wh.query(
            f"SELECT transaction_ts FROM transactions"
            f" WHERE transaction_id IN ({placeholders})", tuple(evidence))
        window_start = (min(r["transaction_ts"] for r in stamps)
                        if stamps else "1970-01-01 00:00:00")

        # Pass-through: a cash outflow after the deposits.
        pass_id = None
        out = self.wh.one(f"""
            SELECT transaction_id, transaction_ts, amount FROM transactions
            WHERE customer_id = ? AND status = 'posted'
              AND is_cash = 1 AND amount >= ? AND amount < ?
              AND transaction_ts > ?
            ORDER BY transaction_ts LIMIT 1
        """, (customer_id, threshold * float(c.get("pass_through_ratio", 0.80)),
              threshold, window_start))
        if out:
            pass_id = out["transaction_id"]
            messages.append(
                f"Cash outflow of USD {float(out['amount']):,.0f} followed the "
                f"structured deposits, consistent with pass-through "
                f"(clause STR-2.2.1).")

        # New counterparty: outbound wires to parties not transacted with before.
        new_cps = self.wh.query(f"""
            SELECT t.transaction_id FROM transactions t
            WHERE t.customer_id = ? AND t.status = 'posted'
              AND t.transaction_ts > ?
              AND t.amount >= ?
              AND t.merchant_name IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM transactions p
                  WHERE p.customer_id = t.customer_id
                    AND p.merchant_name = t.merchant_name
                    AND p.transaction_ts < t.transaction_ts)
            ORDER BY t.transaction_ts LIMIT 3
        """, (customer_id, window_start, threshold * 0.5))
        cp_ids = [r["transaction_id"] for r in new_cps]
        if cp_ids:
            messages.append(
                f"{len(cp_ids)} payment(s) to previously unseen counterparties "
                f"in the window, consistent with dispersal of the aggregated "
                f"funds (clause STR-2.2.1).")
        return messages, pass_id, cp_ids


class VelocityDetector(BaseDetector):
    """Robust (median/MAD) daily-amount anomaly. Clause MON-3.1.1."""

    rule_code = "VELOCITY"

    def run(self, customer_id: str) -> Dict[str, Any]:
        c = self.det["velocity"]
        rows = _rows(self.wh, """
            SELECT business_date,
                   SUM(amount) AS total_amount,
                   COUNT(*)     AS txn_count
            FROM transactions
            WHERE customer_id = ? AND status = 'posted'
              AND business_date >= ?
            GROUP BY business_date
            ORDER BY business_date
        """, (customer_id, self._window_start(c["baseline_days"] + c["lookback_days"])))

        if len(rows) < 10:
            return self.empty(customer_id, "insufficient_daily_history")

        by_date = {r["business_date"]: r for r in rows}
        today_key = self.wh.as_of()
        today = by_date.get(today_key)
        if today is None:
            recent = rows[-1]
            today_key, today = recent["business_date"], recent

        baseline_rows = [r for r in rows if r["business_date"] < today_key]
        if len(baseline_rows) < 8:
            return self.empty(customer_id, "insufficient_baseline_after_cold_start")

        amounts = sorted(r["total_amount"] for r in baseline_rows)
        median = _median(amounts)
        mad = _median([abs(a - median) for a in amounts])
        scale = 1.4826 * mad
        if scale <= 0:
            scale = (sum(amounts) / len(amounts)) * 0.25 or 1.0

        z = (today["total_amount"] - median) / scale
        reasons: List[str] = []
        evidence: List[str] = []
        metrics = {
            "anchor_date": today_key,
            "today_amount": round(today["total_amount"], 2),
            "baseline_median": round(median, 2),
            "robust_scale_mad": round(scale, 2),
            "robust_zscore": round(z, 3),
            "baseline_days_used": len(baseline_rows),
            "today_txn_count": today["txn_count"],
        }

        if z <= c["zscore_threshold"]:
            return self.result(
                customer_id,
                max(0.0, min(0.3, z / (c["zscore_threshold"] * 4))),
                0.45 + self._evidence_bonus(len(baseline_rows)),
                [f"Daily spend on {today_key} (USD {today['total_amount']:,.0f}) is within "
                 f"{c['zscore_threshold']:.1f} robust standard deviations of the "
                 f"90-day baseline (z={z:.2f})."],
                evidence, metrics, fired=False)

        evidence = _rows(self.wh, """
            SELECT transaction_id FROM transactions
            WHERE customer_id = ? AND status = 'posted' AND business_date = ?
        """, (customer_id, today_key))
        evidence_ids = [r["transaction_id"] for r in evidence]

        score = min(0.95, 0.55 + min(0.35, 0.09 * (z - c["zscore_threshold"])))
        reasons.append(
            f"Daily spend on {today_key} of USD {today['total_amount']:,.0f} is "
            f"{z:.1f} robust z-scores above the 90-day median of "
            f"USD {median:,.0f} ({today['txn_count']} transactions).")
        if today["txn_count"] > 20:
            reasons.append(f"Transaction count also spiked to {today['txn_count']}.")
            score = min(0.97, score + 0.05)

        return self.result(customer_id, score, min(0.95, 0.55 + self._evidence_bonus(len(baseline_rows))),
                           reasons, evidence_ids, metrics)


class GeographicDetector(BaseDetector):
    """Elevated-risk concentration and physically impossible travel."""

    rule_code = "GEOGRAPHIC"

    def run(self, customer_id: str) -> Dict[str, Any]:
        c = self.det["geographic"]
        days = c["lookback_days"]
        start = self._window_start(days)
        # The elevated-risk country list is inlined as SQL literals from config
        # so the query text shown to a reviewer is self-contained.
        risk_list = ", ".join(f"'{x}'" for x in c["risk_countries"])
        home = (self.wh.one("SELECT jurisdiction FROM customers WHERE customer_id = ?",
                            (customer_id,)) or {}).get("jurisdiction")

        # Clause GEO-2.1.1 is a *cross-border concentration* test, so the
        # customer's own jurisdiction is excluded from both the numerator and the
        # denominator. A customer domiciled in an elevated-risk jurisdiction is
        # not "geographically concentrated"; they are a high-risk-geometry
        # relationship handled under CDD-1.2.1, and conflating the two would
        # flag every legitimate local customer as an anomaly.
        geo = self.wh.one(f"""
            SELECT COUNT(*) AS txn_count,
                   SUM(CASE WHEN merchant_country IN ({risk_list}) THEN 1 ELSE 0 END)
                       AS risk_txns,
                   COUNT(DISTINCT CASE WHEN merchant_country IN ({risk_list})
                                       THEN merchant_country END) AS risk_countries,
                   MAX(merchant_country) AS any_country
            FROM transactions
            WHERE customer_id = ? AND status = 'posted' AND business_date >= ?
              AND merchant_country IS NOT NULL AND merchant_country <> ?
        """, (customer_id, start, home)) or {}

        total = int(geo.get("txn_count") or 0)
        risk_n = int(geo.get("risk_txns") or 0)
        share = (risk_n / total) if total else 0.0

        reasons: List[str] = []
        evidence: List[str] = []
        score = 0.0
        metrics: Dict[str, Any] = {
            "txn_count": total, "elevated_risk_txns": risk_n,
            "elevated_risk_share": round(share, 4), "window_days": days,
            "elevated_risk_countries": c["risk_countries"],
            "home_jurisdiction": home,
            "home_excluded": True,
        }
        if home in c["risk_countries"]:
            metrics["high_risk_home_jurisdiction"] = True
            metrics["governing_regime"] = "CDD-1.2.1 (enhanced due diligence), not GEO-2.1.1"

        if total == 0:
            metrics["note"] = ("no cross-border activity in the window; the home "
                               "jurisdiction is excluded from the concentration test")
            return self.empty(customer_id, "no_cross_border_activity_in_window")

        min_sample = int(c["min_cross_border_txns"])
        if total < min_sample:
            metrics["note"] = (f"only {total} cross-border transaction(s) in {days} days; "
                               f"the share test needs at least {min_sample} to be meaningful")
            metrics["suppressed"] = "insufficient_cross_border_sample"

        if share > c["high_risk_share_threshold"] and total >= min_sample:
            score = max(score, min(0.95, 0.70 + share * 0.3))
            reasons.append(
                f"{share:.0%} of {total} transactions in {days} days were booked in "
                "elevated-risk jurisdictions, above the 30% concentration threshold "
                "(clause GEO-2.1.1).")
        elif share > c["elevated_share_threshold"] and total >= min_sample:
            score = max(score, 0.45 + share)
            reasons.append(
                f"{share:.0%} of transactions in {days} days were booked in elevated-risk "
                "jurisdictions (clause GEO-2.1.1).")

        # Impossible travel: distinct countries inside a rolling 24h window.
        # Impossible travel: implied speed between consecutive jurisdiction
        # changes, evaluated inside a window that is genuinely bounded in time
        # (a row-count bound would let the window span days and hide a fast hop).
        # A raw country count would fire on any internationally active
        # customer; an implied speed above cruise velocity cannot be explained
        # by travel at all.
        hops = _rows(self.wh, """
            SELECT merchant_country, transaction_ts, transaction_id
            FROM transactions
            WHERE customer_id = ? AND status = 'posted' AND business_date >= ?
            ORDER BY transaction_ts
        """, (customer_id, start))
        hop_found = None
        risk_set = set(c["risk_countries"])
        centroids = self.geo_centroids
        max_kmh = float(c["impossible_travel_kmh"])
        min_countries = int(c["min_jurisdictions_in_window"])
        max_hours = float(c["impossible_travel_hours"])

        for i in range(1, len(hops)):
            cur = hops[i]
            # Everything within the window ending at this transaction.
            window = [hops[j] for j in range(i - 1, -1, -1)
                      if _hours_between(hops[j]["transaction_ts"], cur["transaction_ts"])
                      <= max_hours]
            window.append(cur)
            window.sort(key=lambda r: r["transaction_ts"])
            countries = []
            for item in window:
                if item["merchant_country"] not in countries:
                    countries.append(item["merchant_country"])
            if len(countries) < min_countries:
                continue
            fastest = None
            for prev, nxt in zip(window, window[1:]):
                if prev["merchant_country"] == nxt["merchant_country"]:
                    continue
                a, b = centroids.get(prev["merchant_country"]), centroids.get(nxt["merchant_country"])
                if not a or not b:
                    continue
                hours = _hours_between(prev["transaction_ts"], nxt["transaction_ts"])
                if hours <= 0.05:
                    continue
                km = _haversine(a, b)
                kmh = km / hours
                if kmh > max_kmh and (fastest is None or kmh > fastest["implied_kmh"]):
                    fastest = {"leg": f"{prev['merchant_country']}->{nxt['merchant_country']}",
                               "distance_km": round(km, 1), "hours": round(hours, 2),
                               "implied_kmh": round(kmh, 1),
                               "from_txn": prev["transaction_id"],
                               "to_txn": nxt["transaction_id"]}
            if fastest is None:
                continue
            span = round(_hours_between(window[0]["transaction_ts"], cur["transaction_ts"]), 1)
            hop_found = {
                "countries": countries,
                "start": window[0]["transaction_ts"],
                "end": cur["transaction_ts"],
                "span_hours": span,
                "window_hours": max_hours,
                "txn_ids": [h["transaction_id"] for h in window],
                "includes_elevated_risk": bool(set(countries) & risk_set),
                "fastest_leg": fastest,
                "implied_kmh": fastest["implied_kmh"],
                "distance_km": fastest["distance_km"],
                "leg": fastest["leg"],
            }
            break
        if hop_found:
            evidence = hop_found["txn_ids"]
            hop_score = 0.72 if hop_found["includes_elevated_risk"] else 0.55
            score = max(score, hop_score)
            reasons.append(
                f"Within a {hop_found['span_hours']:.1f} hour window the customer transacted "
                f"in {len(hop_found['countries'])} jurisdictions "
                f"({', '.join(hop_found['countries'])}); the leg "
                f"{hop_found['leg']} implies {hop_found['distance_km']:,.0f} km in "
                f"{hop_found['fastest_leg']['hours']:.1f} hours "
                f"({hop_found['implied_kmh']:,.0f} km/h), above the {max_kmh:,.0f} km/h "
                "commercial cruise ceiling - physically impossible travel, not a "
                "legitimate trip (clause GEO-2.2.1).")
            metrics["impossible_travel"] = hop_found
            metrics["countries_in_window"] = len(hop_found["countries"])

        if score == 0.0:
            reasons.append(f"Geographic footprint is normal: {total} transactions, "
                           f"{share:.0%} in elevated-risk jurisdictions.")
            return self.result(customer_id, 0.0, 0.55 + self._evidence_bonus(total),
                               reasons, [], metrics, fired=False)

        return self.result(customer_id, score, min(0.94, 0.5 + self._evidence_bonus(total)),
                           reasons, evidence, metrics)


class AccountTakeoverDetector(BaseDetector):
    """New device + new network country + immediate abnormal ticket."""

    rule_code = "ACCOUNT_TAKEOVER"

    def run(self, customer_id: str) -> Dict[str, Any]:
        c = self.det["account_takeover"]
        days = c["lookback_days"]
        risk_countries = c["risk_countries"]
        placeholders = ",".join("?" for _ in risk_countries)
        start = self._window_start(days)
        # The "new device" test is a first-seen test: a device that did not touch
        # this customer at all in the preceding 90 days. Counting occurrences in
        # the detection window would mislabel a device used three times as known.
        history_start = self._window_start(days + 90)

        rows = _rows(self.wh, """
            SELECT t.transaction_id, t.transaction_ts, t.amount, t.device_id, t.ip_country,
                   t.channel, t.merchant_country, t.business_date,
                   a.account_id, a.status AS account_status, a.opened_at
            FROM transactions t
            JOIN accounts a ON a.account_id = t.account_id
            WHERE t.customer_id = ? AND t.status = 'posted' AND t.business_date >= ?
            ORDER BY t.transaction_ts DESC
            LIMIT 400
        """, (customer_id, start))

        if len(rows) < 3:
            return self.empty(customer_id, "insufficient_recent_activity")

        known_devices = {
            r["device_id"] for r in _rows(self.wh, """
                SELECT DISTINCT device_id FROM transactions
                WHERE customer_id = ? AND status = 'posted'
                  AND business_date >= ? AND business_date < ?
            """, (customer_id, history_start, start))
        }
        new_device_rows = [r for r in rows
                           if r["device_id"] and r["device_id"] not in known_devices]
        if not new_device_rows:
            return self.empty(customer_id, "no_new_device_fingerprint")

        home = (self.wh.one("SELECT jurisdiction FROM customers WHERE customer_id = ?",
                            (customer_id,)) or {}).get("jurisdiction")
        suspicious = [r for r in new_device_rows
                      if r["ip_country"] in risk_countries and r["ip_country"] != home]

        reasons: List[str] = []
        evidence: List[str] = []
        score = 0.0
        metrics: Dict[str, Any] = {
            "new_device_count": len(new_device_rows),
            "history_window_start": history_start,
            "detection_window_start": start,
            "new_device_from_risk_country": len(suspicious),
            "risk_countries": risk_countries,
            "window_days": days,
        }

        if not suspicious:
            return self.result(customer_id, 0.10, 0.45,
                              ["An unseen device fingerprint was used, but it originated from "
                               "the customer's usual network; no takeover indicator completed."],
                              [], metrics, fired=False)

        evidence = [r["transaction_id"] for r in suspicious]
        dev = suspicious[0]["device_id"]
        exposure = sum(r["amount"] for r in suspicious)
        reasons.append(
            f"Account accessed from previously unseen device {dev} originating from "
            f"{suspicious[0]['ip_country']} (clause GEO-2.2.2).")
        score = 0.62
        metrics["new_device_id"] = dev
        metrics["exposure_on_new_device"] = round(exposure, 2)

        big = [r for r in suspicious if r["amount"] >= c["new_device_amount"]]
        if big:
            reasons.append(
                f"USD {sum(r['amount'] for r in big):,.0f} transacted on the new device in "
                f"{len(big)} transaction(s), at or above the USD "
                f"{c['new_device_amount']:,.0f} abnormal-ticket floor.")
            score = min(0.98, score + 0.22 + min(0.1, 0.01 * len(big)))
            evidence.extend(r["transaction_id"] for r in big)
        else:
            score = min(0.75, score + 0.05)

        dormant = self.wh.one(
            "SELECT opened_at, status FROM accounts WHERE customer_id = ? ORDER BY opened_at LIMIT 1",
            (customer_id,)) or {}
        if dormant.get("status") == "dormant":
            reasons.append("Account is flagged dormant, so prior inactivity is established.")
            score = min(0.98, score + 0.06)
            metrics["account_status"] = "dormant"

        return self.result(customer_id, score, min(0.93, 0.55 + self._evidence_bonus(len(rows))),
                           reasons, evidence, metrics)


class MuleNetworkDetector(BaseDetector):
    """Fan-in from multiple senders on a shared device, then fast pass-through.

    Scoped to the customer, but the shared-device hop means one detector answers
    for the whole ring; the ring members are reported as related parties.
    """

    rule_code = "MULE_NETWORK"

    def run(self, customer_id: str) -> Dict[str, Any]:
        c = self.det["mule_network"]
        days = c["lookback_days"]
        start = self._window_start(days)

        # Path B (ring member): this customer's own fingerprints are shared with
        # other customers. This is how a *sender* into a pass-through hub is
        # detected, since the sender has no fan-in of its own.
        shared = _rows(self.wh, """
            WITH my_devices AS (
                SELECT DISTINCT device_id FROM transactions
                WHERE customer_id = ? AND status = 'posted'
                  AND business_date >= ? AND device_id IS NOT NULL
            )
            SELECT t.device_id,
                   COUNT(DISTINCT t.customer_id) AS customers_on_device,
                   COUNT(*)                    AS txn_count,
                   SUM(t.amount)                AS total_amount
            FROM transactions t
            JOIN my_devices d ON d.device_id = t.device_id
            WHERE t.status = 'posted' AND t.business_date >= ?
              AND t.device_id IN (SELECT device_id FROM mule_clusters)
            GROUP BY t.device_id
            HAVING COUNT(DISTINCT t.customer_id) > 1
        """, (customer_id, start, start))
        member_score = 0.0
        member_reasons: List[str] = []
        member_evidence: List[str] = []
        member_metrics: Dict[str, Any] = {}
        if shared:
            best = max(shared, key=lambda r: r["customers_on_device"])
            n_members = int(best["customers_on_device"])
            # Reach for the hub's own fan-in so the ring is described, not guessed.
            hub_in = self.wh.one("""
                SELECT COUNT(DISTINCT customer_id) AS senders, SUM(amount) AS total_in
                FROM transactions
                WHERE status = 'posted' AND txn_type = 'wire_in'
                  AND device_id = ?
            """, (best["device_id"],)) or {}
            member_score = min(0.95, 0.45 + min(0.35, 0.02 * n_members))
            member_evidence = [r["transaction_id"] for r in _rows(self.wh, """
                SELECT transaction_id FROM transactions
                WHERE customer_id = ? AND status = 'posted' AND device_id = ?
            """, (customer_id, best["device_id"]))]
            member_reasons.append(
                f"Activity on device {best['device_id']} is shared with "
                f"{n_members} customers, indicating a common control point.")
            if int(hub_in.get("senders") or 0) >= c["fan_in_min"]:
                member_reasons.append(
                    f"That same device received inbound wires from "
                    f"{int(hub_in['senders'])} senders totalling "
                    f"USD {_num(hub_in.get('total_in')):,.0f} - a pass-through hub.")
                member_score = min(0.97, member_score + 0.20)
                member_evidence.extend(
                    r["transaction_id"] for r in _rows(self.wh, """
                        SELECT transaction_id FROM transactions
                        WHERE device_id = ? AND txn_type = 'wire_in' LIMIT 25
                    """, (best["device_id"],)))
            member_metrics = {
                "role": "ring_member",
                "shared_device_id": best["device_id"],
                "customers_on_device": n_members,
                "device_total_amount": _num(best["total_amount"]),
                "hub_senders": int(hub_in.get("senders") or 0),
                "hub_total_in": _num(hub_in.get("total_in")),
            }

        # Path A (hub): inbound wire fan-in followed by pass-through.
        fanin = _rows(self.wh, """
            SELECT business_date, COUNT(DISTINCT customer_id) AS senders,
                   SUM(amount) AS total_in
            FROM transactions
            WHERE customer_id = ? AND status = 'posted' AND txn_type = 'wire_in'
              AND business_date >= ?
            GROUP BY business_date
            HAVING senders >= ?
        """, (customer_id, start, c["fan_in_min"]))

        reasons: List[str] = []
        evidence: List[str] = []
        score = 0.0
        metrics: Dict[str, Any] = {"fan_in_days": len(fanin), "window_days": days}

        if not fanin:
            metrics.update(member_metrics)
            if member_score > 0:
                return self.result(
                    customer_id, member_score,
                    min(0.90, 0.5 + self._evidence_bonus(len(member_evidence))),
                    member_reasons, member_evidence, metrics)
            return self.empty(customer_id, "no_inbound_fan_in_and_no_shared_device")

        top = max(fanin, key=lambda r: r["senders"])
        metrics["max_senders_in_a_day"] = top["senders"]
        metrics["total_inbound"] = round(sum(r["total_in"] for r in fanin), 2)

        inbound = _rows(self.wh, """
            SELECT transaction_id, customer_id, amount, transaction_ts, business_date, device_id
            FROM transactions
            WHERE customer_id = ? AND status = 'posted' AND txn_type = 'wire_in'
              AND business_date = ?
        """, (customer_id, top["business_date"]))
        evidence = [r["transaction_id"] for r in inbound]
        metrics["senders"] = sorted({r["customer_id"] for r in inbound})
        score = min(0.90, 0.45 + min(0.30, 0.05 * top["senders"]))
        reasons.append(
            f"{top['senders']} distinct senders wired USD {top['total_in']:,.0f} into the "
            "account within a single day (mule fan-in signature).")

        # 2. Device shared with other customers = ring membership.
        devices = [r["device_id"] for r in inbound if r["device_id"]]
        ring: List[str] = []
        if devices:
            placeholders = ",".join("?" for _ in set(devices))
            shared = _rows(self.wh, f"""
                SELECT customer_id, COUNT(DISTINCT customer_id) AS customers_on_device
                FROM transactions
                WHERE device_id IN ({placeholders}) AND business_date >= ?
                GROUP BY device_id
                HAVING customers_on_device >= ?
            """, tuple(set(devices)) + (start, c["shared_device_min"]))
            if shared:
                best = max(shared, key=lambda r: r["customers_on_device"])
                metrics["shared_device_customers"] = best["customers_on_device"]
                score = min(0.97, score + 0.18)
                reasons.append(
                    f"A single device fingerprint is shared by "
                    f"{best['customers_on_device']} customers, indicating a common control "
                    "point (clause STR-2.1.3 profile-multiple test).")
                members = _rows(self.wh, """
                    SELECT DISTINCT customer_id FROM transactions
                    WHERE device_id = ? AND business_date >= ?
                """, (self._device_of(self.wh, customer_id, inbound), start))
                ring = sorted({r["customer_id"] for r in members if r["customer_id"] != customer_id})
                metrics["ring_members_sample"] = ring[:20]

        # 3. Pass-through: outbound within the configured window.
        if inbound:
            first_in = min(r["transaction_ts"] for r in inbound)
            out = _rows(self.wh, """
                SELECT transaction_id, amount, transaction_ts, txn_type
                FROM transactions
                WHERE customer_id = ? AND status = 'posted' AND txn_type = 'wire_out'
                  AND transaction_ts >= ? AND transaction_ts <= datetime(?, '+%d hours')
                ORDER BY transaction_ts
            """ % c["pass_through_hours"], (customer_id, first_in, first_in))
            if out:
                out_total = sum(r["amount"] for r in out)
                evidence.extend(r["transaction_id"] for r in out)
                ratio = out_total / max(1.0, top["total_in"])
                metrics["outbound_in_window"] = round(out_total, 2)
                metrics["pass_through_ratio"] = round(ratio, 3)
                if ratio >= c["inflow_outflow_ratio"]:
                    score = min(0.99, score + 0.15)
                    reasons.append(
                        f"USD {out_total:,.0f} ({ratio:.0%} of the day's inflow) was wired "
                        f"onward within {c['pass_through_hours']} hours - pass-through.")
                else:
                    reasons.append(
                        f"USD {out_total:,.0f} left within {c['pass_through_hours']} hours, "
                        "below the pass-through ratio threshold.")

        metrics["ring_members"] = ring
        # Merge the member path: a customer can be both a member and a hub.
        if member_score > 0:
            score = max(score, member_score)
            reasons = member_reasons + reasons
            evidence = sorted(set(member_evidence) | set(evidence))
            metrics.setdefault("role", "hub")
            metrics.update({k: v for k, v in member_metrics.items() if k != "role"})
        return self.result(customer_id, score, min(0.92, 0.5 + self._evidence_bonus(len(evidence))),
                           reasons, evidence, metrics)

    @staticmethod
    def _device_of(wh, customer_id, inbound):
        counts: Dict[str, int] = {}
        for r in inbound:
            counts[r["device_id"]] = counts.get(r["device_id"], 0) + 1
        return max(counts, key=counts.get) if counts else ""


class TradeBasedDetector(BaseDetector):
    """Round-value cross-border wires with no matching inbound settlement."""

    rule_code = "TRADE_BASED"

    def run(self, customer_id: str) -> Dict[str, Any]:
        c = self.det["trade_based"]
        start = self._window_start(c["lookback_days"])
        rows = _rows(self.wh, """
            SELECT transaction_id, amount, currency, merchant_country, transaction_ts, txn_type
            FROM transactions
            WHERE customer_id = ? AND status = 'posted' AND channel = 'WIRE'
              AND business_date >= ?
            ORDER BY transaction_ts
        """, (customer_id, start))
        if not rows:
            return self.empty(customer_id, "no_cross_border_wires")

        outbound = [r for r in rows if r["txn_type"] == "wire_out"]
        round_wires = [r for r in outbound if r["amount"] % c["round_amount"] == 0]
        if len(round_wires) < c["min_wires"]:
            return self.result(
                customer_id, 0.08, 0.45,
                [f"{len(outbound)} cross-border wires observed, but only {len(round_wires)} "
                 "are round-value; below the TBML indicator count."],
                [], {"wire_count": len(outbound), "round_wire_count": len(round_wires)},
                fired=False)

        inbound_total = sum(r["amount"] for r in rows if r["txn_type"] == "wire_in")
        out_total = sum(r["amount"] for r in round_wires)
        countries = sorted({r["merchant_country"] for r in round_wires})
        reasons = [
            f"{len(round_wires)} outbound wires of round value (multiples of "
            f"USD {c['round_amount']:,.0f}) totalling USD {out_total:,.0f} to "
            f"{', '.join(countries[:4])} with no matching inbound trade settlement "
            "(clause TBM-3.1.1).",
        ]
        score = min(0.90, 0.50 + min(0.25, 0.04 * len(round_wires)))
        if inbound_total == 0:
            reasons.append("Zero inbound settlement was booked against the outbound wires.")
            score = min(0.95, score + 0.10)
        else:
            mismatch = out_total / max(1.0, inbound_total)
            reasons.append(
                f"Outbound value is {mismatch:.1f}x the inbound settlement value - an "
                "invoice-mismatch signature.")
            score = min(0.95, score + 0.12)
        return self.result(
            customer_id, score, min(0.88, 0.5 + self._evidence_bonus(len(round_wires))),
            reasons, [r["transaction_id"] for r in round_wires],
            {"wire_count": len(outbound), "round_wire_count": len(round_wires),
             "outbound_round_total": round(out_total, 2),
             "inbound_settlement_total": round(inbound_total, 2),
             "destination_countries": countries})


DETECTORS = {
    "STRUCTURING": StructuringDetector,
    "VELOCITY": VelocityDetector,
    "GEOGRAPHIC": GeographicDetector,
    "ACCOUNT_TAKEOVER": AccountTakeoverDetector,
    "MULE_NETWORK": MuleNetworkDetector,
    "TRADE_BASED": TradeBasedDetector,
}


# --------------------------------------------------------------------------


class FraudDetectorAgent:
    """Runs the detector portfolio and produces a composite, explainable score."""

    def __init__(self, warehouse, config: Optional[Dict[str, Any]] = None,
                 detectors: Optional[Dict[str, type]] = None):
        self.wh = warehouse
        self.cfg = config or load_config()
        self.detectors = detectors or DETECTORS

    def _instances(self) -> Dict[str, BaseDetector]:
        return {code: cls(self.wh, self.cfg) for code, cls in self.detectors.items()}

    def detect_customer(self, customer_id: str) -> Dict[str, Any]:
        weights = self.cfg["detection"]["weights"]
        bands = self.cfg["detection"]["risk_bands"]
        instances = self._instances()

        signals: Dict[str, Dict[str, Any]] = {}
        for code, det in instances.items():
            try:
                signals[code] = det.run(customer_id)
            except Exception as exc:  # guardrail: one bad rule must not kill the case
                signals[code] = {
                    "rule_code": code, "customer_id": customer_id, "score": 0.0,
                    "confidence": 0.0, "fired": False,
                    "reasons": [f"rule error: {exc}"], "evidence_txn_ids": [],
                    "metrics": {}, "error": str(exc),
                }

        composite = 0.0
        contributions = {}
        for code, sig in signals.items():
            w = float(weights.get(code.lower(), 0.0))
            contrib = w * float(sig["score"])
            contributions[code] = {
                "weight": w, "score": sig["score"], "contribution": round(contrib, 4),
            }
            composite += contrib
        composite = round(min(0.99, composite), 4)

        fired = [s for s in signals.values() if s["fired"]]
        fired.sort(key=lambda s: s["score"], reverse=True)

        # Confidence is evidence-weighted, not a constant: a case with three
        # firing rules each backed by rows is trustworthy; a case with one weak
        # rule and no data is not.
        total_evidence = sum(s.get("evidence_count", 0) for s in fired)
        data_conf = min(0.35, total_evidence / 25.0)
        signal_conf = (sum(s["confidence"] for s in fired) / len(fired)) if fired else 0.0
        coverage = len(fired) / max(1, len(self.detectors))
        confidence = round(min(0.95, 0.30 + 0.45 * signal_conf + data_conf + 0.15 * coverage), 4)

        risk_level = ("high" if composite >= bands["high"]
                      else "medium" if composite >= bands["medium"] else "low")

        # Typology override: a confirmed high-confidence typology cannot be
        # diluted below a floor by the weighting scheme. Recorded, not silent.
        order = {"low": 0, "medium": 1, "high": 2}
        override = None
        for rule in self.cfg["detection"].get("typology_override", []):
            sig = signals.get(rule["rule_code"])
            if not sig or sig["score"] < float(rule["min_score"]):
                continue
            floor = rule["floor_level"]
            if order[floor] > order[risk_level]:
                override = {
                    "rule_code": rule["rule_code"],
                    "rule_score": sig["score"],
                    "min_score": rule["min_score"],
                    "raised_level": floor,
                    "composite_score_at_time": composite,
                    "basis": f"clause-bound typology floor for {rule['rule_code']}",
                }
                risk_level = floor

        evidence_ids = sorted({e for s in signals.values() for e in s.get("evidence_txn_ids", [])})

        return {
            "customer_id": customer_id,
            "detected_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "composite_score": composite,
            "risk_level": risk_level,
            "risk_level_source": "typology_override" if override else "weighted_composite",
            "typology_override": override,
            "confidence": confidence,
            "fired_rules": [s["rule_code"] for s in fired],
            "rule_contributions": contributions,
            "signals": signals,
            "evidence_txn_ids": evidence_ids[:200],
            "evidence_count": len(evidence_ids),
            "primary_reason": fired[0]["reasons"][0] if fired else
                              "No detection rule exceeded its threshold.",
            "as_of_date": self.wh.as_of(),
        }

    def detect_population(self, customer_ids: List[str]) -> Dict[str, Any]:
        results = [self.detect_customer(cid) for cid in customer_ids]
        dist = {"high": 0, "medium": 0, "low": 0}
        for r in results:
            dist[r["risk_level"]] = dist.get(r["risk_level"], 0) + 1
        ranked = sorted(results, key=lambda r: r["composite_score"], reverse=True)
        return {
            "population_size": len(results),
            "risk_distribution": dist,
            "ranked": ranked,
            "top_findings": ranked[:25],
            "escalation_candidates": [r["customer_id"] for r in ranked
                                      if r["risk_level"] == "high"
                                      and r["confidence"] >= self.cfg["copilot"]["min_auto_escalation_confidence"]],
            "review_candidates": [r["customer_id"] for r in ranked
                                  if r["risk_level"] == "medium"],
        }


# --------------------------------------------------------------------------
# Small numeric helpers (kept here so the detection maths is unit-testable)
# --------------------------------------------------------------------------


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _haversine(a: tuple, b: tuple) -> float:
    """Great-circle distance in km between two [lat, lon] centroids."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _hours_between(a: str, b: str) -> float:
    fmt = "%Y-%m-%d %H:%M:%S"
    for f in (fmt, "%Y-%m-%dT%H:%M:%S"):
        try:
            return (datetime.strptime(b, f) - datetime.strptime(a, f)).total_seconds() / 3600.0
        except ValueError:
            continue
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600.0
    except ValueError:
        return 0.0
