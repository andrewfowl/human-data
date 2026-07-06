from factory import validators
from tests.conftest import GOOD_SFT


def codes(result):
    return {f.code for f in result.findings}


def test_good_submission_passes():
    result = validators.run_all(GOOD_SFT, "sft")
    assert result.passed, result.as_dict()


def test_missing_required_field_fails():
    result = validators.run_all({"response": ""}, "sft")
    assert not result.passed
    assert "SCHEMA_MISSING_FIELD" in codes(result)


def test_short_response_fails():
    result = validators.run_all({"response": "Too short.", "citations": ["ASC 606"]}, "sft")
    assert "SCHEMA_TOO_SHORT" in codes(result)


def test_pii_ssn_blocks():
    content = dict(GOOD_SFT, response=GOOD_SFT["response"] + " Client SSN 123-45-6789.")
    result = validators.run_all(content, "sft")
    assert "PII_SSN" in codes(result)
    assert not result.passed


def test_pii_email_blocks():
    content = dict(GOOD_SFT, response=GOOD_SFT["response"] + " Contact cfo@client.com.")
    result = validators.run_all(content, "sft")
    assert "PII_EMAIL" in codes(result)


def test_placeholder_text_blocks():
    content = dict(GOOD_SFT, response=GOOD_SFT["response"] + " TODO: finish this section")
    result = validators.run_all(content, "sft")
    assert "PLACEHOLDER_TEXT" in codes(result)


def test_refusal_marker_blocks():
    content = dict(GOOD_SFT, response="As an AI, I cannot assist with accounting. " * 20)
    result = validators.run_all(content, "sft")
    assert "PLACEHOLDER_TEXT" in codes(result)


def test_fabricated_asc_topic_blocks():
    content = dict(GOOD_SFT, response=GOOD_SFT["response"] + " See ASC 999 for details.")
    result = validators.run_all(content, "sft")
    assert "CITATION_INVALID_ASC" in codes(result)
    assert not result.passed


def test_valid_ifrs_and_ias_pass():
    content = dict(GOOD_SFT, response=GOOD_SFT["response"] + " Compare IFRS 15 and IAS 38.")
    result = validators.run_all(content, "sft")
    assert result.passed


def test_invalid_ifrs_blocks():
    content = dict(GOOD_SFT, response=GOOD_SFT["response"] + " Under IFRS 42 this differs.")
    result = validators.run_all(content, "sft")
    assert "CITATION_INVALID_IFRS" in codes(result)


def test_unbalanced_balance_sheet_blocks():
    content = dict(GOOD_SFT)
    content["financials"] = {"balance_sheet": {"assets": 100.0, "liabilities": 70.0, "equity": 40.0}}
    result = validators.run_all(content, "sft")
    assert "FIN_BALANCE_SHEET" in codes(result)


def test_balanced_journal_entries_pass():
    content = dict(GOOD_SFT)
    content["financials"] = {"journal_entries": [
        {"account": "Cash", "debit": 500, "credit": 0},
        {"account": "Revenue", "debit": 0, "credit": 500},
    ]}
    assert validators.run_all(content, "sft").passed


def test_unbalanced_journal_entries_block():
    content = dict(GOOD_SFT)
    content["financials"] = {"journal_entries": [
        {"account": "Cash", "debit": 500, "credit": 0},
        {"account": "Revenue", "debit": 0, "credit": 450},
    ]}
    result = validators.run_all(content, "sft")
    assert "FIN_JE_UNBALANCED" in codes(result)


def test_duplicate_detection():
    fp = validators.content_fingerprint(GOOD_SFT, "sft")
    result = validators.run_all(GOOD_SFT, "sft", existing_fingerprints={fp})
    assert "DUPLICATE_CONTENT" in codes(result)


def test_no_citations_is_warning_not_error():
    content = dict(GOOD_SFT, citations=[])
    result = validators.run_all(content, "sft")
    # missing field error because empty list counts as empty required field
    assert "SCHEMA_MISSING_FIELD" in codes(result)
