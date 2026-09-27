"""Reported prefix-language gap, with task/market and profile counter-controls."""
import pytest

from jobhound.config import CONFIG
from jobhound.v41.engine import evaluate_observations
from test_v55_action_policy import observation, NOW


def assessed(title):
    row=observation()
    row.job.title=title
    return evaluate_observations([row],as_of=NOW).evaluated[0]


@pytest.mark.parametrize('language',['Korean','Dutch','Japanese'])
def test_prefix_language_binds_to_document_annotation(monkeypatch,language):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    row=assessed(language+' PDF Annotation Specialist')
    assert any('lang_mismatch:'+language.lower() in b for b in row.assessment.blockers)
    assert row.decision.action_band.value=='reject'


@pytest.mark.parametrize('title',[
    'Bengali PDF Annotation Specialist',
    'English Document Annotation Specialist',
    'PDF Annotation Specialist serving Korean clients',
    'Korean Culture Research Assistant',
    'Software Engineer for Dutch customers',
])
def test_language_prefix_rule_preserves_supported_and_market_controls(monkeypatch,title):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    row=assessed(title)
    assert not any('lang_mismatch:' in b for b in row.assessment.blockers)


def test_legacy_title_rule_is_unchanged(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',False)
    row=assessed('Korean PDF Annotation Specialist')
    assert not any('lang_mismatch:korean' in b for b in row.assessment.blockers)
