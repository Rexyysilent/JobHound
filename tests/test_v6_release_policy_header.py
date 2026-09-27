"""Snapshot release_policy headers carry decision policy, not transport limits.

Regression: restoring a header with plain setattr put the nested
production_fetch block back as a raw dict, so model_dump warned and any
attribute read (production_fetch.requests) during a replay would fail.
Snapshots from the 2026-09-27 live capture already contain that block.
"""
import warnings

from jobhound.config import CONFIG, ProductionFetchCfg
from jobhound.v41.replay import release_policy_payload, restore_release_policy


def test_release_policy_payload_omits_transport_limits():
    policy = release_policy_payload()
    assert 'production_fetch' not in policy
    assert policy['enabled'] is True


def test_restore_ignores_transport_limits_in_older_headers():
    config = CONFIG.model_copy(deep=True)
    header_policy = {
        'enabled': True,
        'request_budget': 17,
        'production_fetch': {'requests': 5, 'decoded_bytes': 1_000_000},
    }
    restore_release_policy(config, header_policy)
    assert config.v55.enabled is True and config.v55.request_budget == 17
    assert isinstance(config.v55.production_fetch, ProductionFetchCfg)
    assert config.v55.production_fetch.requests == CONFIG.v55.production_fetch.requests
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        config.model_dump()
