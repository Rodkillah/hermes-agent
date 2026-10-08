"""Separate a proven technical receipt from a cron's pre-verification answer.

No natural-language classification. Unknown/mixed output stays visible. Evidence
and the synthetic stop-gate provenance are mandatory; a marker is never proof.
"""
from pathlib import Path

_RECEIPTS = frozenset(("AD_HOC_PASS", "[AD_HOC_PASS]"))


def verification_delivery(agent, final_response, messages):
    if not isinstance(final_response, str) or final_response.strip() not in _RECEIPTS:
        return None
    if getattr(agent, "platform", None) != "cron":
        return None
    # Select only the first verify-on-stop candidate of this turn, and refuse
    # steering or another stop gate after it: those can introduce new obligations.
    start = next((i for i, m in enumerate(messages) if m.get("_verification_stop_synthetic")), None)
    if start is None or start == 0 or messages[start].get("role") != "user":
        return None
    candidate = messages[start - 1]
    if candidate.get("role") != "assistant" or candidate.get("finish_reason") != "verification_required":
        return None
    content = candidate.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    if any(m.get("role") == "user" for m in messages[start + 1:]):
        return None
    try:
        from agent.verification_evidence import verification_status
        from agent.verification_stop import _is_non_code_path
        paths = [p for p in getattr(agent, "_turn_file_mutation_paths", ()) if not _is_non_code_path(p)]
        if not paths:
            return None
        for parent in {str(Path(p).resolve().parent) for p in paths}:
            status = verification_status(session_id=agent.session_id, cwd=parent)
            state = status.get("status", "unverified")
            if state != "passed":
                # A model's PASS marker cannot hide missing, stale or failed proof.
                return {"source": "verify_on_stop/ad_hoc/v1", "receipt": final_response,
                        "content": f"[CRON_FAILURE]\nAd-hoc verification is {state}; PASS is not established.\n\n{content}"}
            if (status.get("evidence") or {}).get("kind") != "ad_hoc":
                return None
    except Exception:
        return None
    return {"source": "verify_on_stop/ad_hoc/v1", "receipt": final_response,
            "content": "[SILENT]" if content.strip() in _RECEIPTS else content}
