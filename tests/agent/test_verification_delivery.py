"""A cron receipt replaces its answer only with fresh proof and stop provenance."""
from types import SimpleNamespace

import pytest

from agent.verification_delivery import verification_delivery


def _agent_and_messages(tmp_path):
    agent = SimpleNamespace(platform="cron", session_id="synthetic", _turn_file_mutation_paths=[str(tmp_path / "code.py")])
    messages = [
        {"role": "assistant", "content": "Le rapport métier", "finish_reason": "verification_required"},
        {"role": "user", "content": "verify", "_verification_stop_synthetic": True},
        {"role": "assistant", "content": "AD_HOC_PASS"},
    ]
    return agent, messages


@pytest.mark.parametrize("state", ["passed", "stale", "failed", "unverified"])
def test_delivery_requires_fresh_ad_hoc_proof(tmp_path, monkeypatch, state):
    from agent import verification_evidence
    agent, messages = _agent_and_messages(tmp_path)
    monkeypatch.setattr(verification_evidence, "verification_status", lambda **kw: {"status": state, "evidence": {"kind": "ad_hoc"}})
    delivery = verification_delivery(agent, "AD_HOC_PASS", messages)
    assert delivery["receipt"] == "AD_HOC_PASS"
    assert "Le rapport métier" in delivery["content"]
    assert delivery["content"].startswith("[CRON_FAILURE]") is (state != "passed")


@pytest.mark.parametrize("mutation", ["steering", "no_provenance", "non_cron", "mixed_receipt", "no_paths"])
def test_receipt_alone_cannot_hide_a_message(tmp_path, monkeypatch, mutation):
    from agent import verification_evidence
    agent, messages = _agent_and_messages(tmp_path)
    monkeypatch.setattr(verification_evidence, "verification_status", lambda **kw: {"status": "passed", "evidence": {"kind": "ad_hoc"}})
    receipt = "AD_HOC_PASS"
    if mutation == "steering":
        messages.append({"role": "user", "content": "nouvelle obligation"})
    elif mutation == "no_provenance":
        messages[1].pop("_verification_stop_synthetic")
    elif mutation == "non_cron":
        agent.platform = "cli"
    elif mutation == "mixed_receipt":
        receipt += " et nouveau résultat"
    elif mutation == "no_paths":
        agent._turn_file_mutation_paths = []
    assert verification_delivery(agent, receipt, messages) is None
