"""Cron receipt admission and the native archive reader agree on delivered content."""
import pytest

from cron.scheduler import _verification_delivery_content
from cron.scheduler_prompt import _archive_answer


def _result():
    return {"completed": True, "final_response": "AD_HOC_PASS", "verification_delivery": {
        "source": "verify_on_stop/ad_hoc/v1", "receipt": "AD_HOC_PASS", "content": "Rapport métier"}}


@pytest.mark.parametrize("flag", ["failed", "interrupted", "partial", "response_transformed"])
def test_incomplete_or_transformed_turn_cannot_exchange_its_receipt(flag):
    result = _result()
    result[flag] = True
    assert _verification_delivery_content(result) is None


def test_receipt_audit_before_framed_archive_is_not_reinjected():
    content = _verification_delivery_content(_result())
    prompt = "synthetic prompt"
    archive = ("## Verification receipt (local audit)\n\nAD_HOC_PASS\n\n"
               f"**Prompt Characters:** {len(prompt)}\n## Prompt\n\n{prompt}\n\n"
               f"**Response Characters:** {len(content)}\n## Response\n\n{content}\n")
    assert _archive_answer(archive) == "Rapport métier"
    assert _archive_answer(archive + "extraneous audit") is None
