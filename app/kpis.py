"""The numbers on the dashboard, computed from what really happened."""

from __future__ import annotations

from typing import Any

from app import db


def compute() -> dict[str, Any]:
    signups = db.list_signups(1000)
    served = [s for s in signups if s.get("ttfr_ms") is not None]  # Set when a first result goes out, search or not.
    queried = [s for s in signups if "attempts" in s]
    found = [s for s in queried if s.get("result_rows")]
    timings = [s["ttfr_ms"] for s in served]
    served_credits = [db.credits_for(s["id"]) for s in served]
    return {
        "signups": len(signups),
        "avg_ttfr_s": round(sum(timings) / len(timings) / 1000, 1) if timings else None,
        "non_empty_rate": round(100 * len(found) / len(queried)) if queried else None,
        "credits_per_served": round(sum(served_credits) / len(served_credits), 2) if served_credits else None,
        "activations": sum(1 for s in signups if s.get("activated_at")),
        "served": len(served),
        "briefs_sent": sum(1 for s in signups if s.get("sales_alert") == "sent"),
        "alerts_suppressed": sum(1 for s in signups if s.get("sales_alert") == "suppressed"),
        "abuse_catches": len(db.list_human_items()),
        "fallbacks": sum(1 for s in signups if s.get("fallback_reason")),
        "total_credits": round(db.total_credits(), 2),
    }
