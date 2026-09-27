from copy import deepcopy
import pytest
from jobhound.review_audit import AuditError, validate_audit


def audit():
    return {'schema': 'jobhound-compact-audit/v1', 'metadata': {},
            'counts': {'canonical_jobs': 0}, 'accounting_ok': True,
            'decisions': [], 'completeness': {'all_decisions': True, 'decision_count': 0}}


@pytest.mark.parametrize('section,key,value', [
    ('counts', 'canonical_jobs', 1), ('counts', 'canonical_jobs', False),
    ('completeness', 'all_decisions', False), ('completeness', 'decision_count', True),
    ('completeness', 'decision_count', 1),
])
def test_compact_count_contract(section, key, value):
    data = deepcopy(audit())
    data[section][key] = value
    with pytest.raises(AuditError):
        validate_audit(data)


def test_empty_compact_is_valid_not_missing():
    assert validate_audit(audit()) == []
