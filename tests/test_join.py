"""Join-checklist fixtures (HANDOFF v2 §10) — priority ordering from data,
the low-trust warning rule, and status round-trips on a tmp copy."""
import shutil

import pytest

from jobhound.join import _DEFAULT_PATH, load_checklist, pending, render_nudges, set_status


@pytest.fixture
def tmp_checklist(tmp_path):
    dst = tmp_path / "platforms_to_join.yaml"
    shutil.copy(_DEFAULT_PATH, dst)
    return dst


def test_ordering_is_trust_times_language_fit():
    items = load_checklist()
    assert [c.priority for c in items] == sorted((c.priority for c in items), reverse=True)
    by_key = {c.key: c for c in items}
    # The Bengali edge reorders the registry: TELUS (0.76×0.9) and Appen
    # (0.64×0.85) outrank higher-trust-but-low-fit Prolific (0.80×0.3).
    assert by_key["telus_oneforma"].priority > by_key["mercor"].priority
    assert by_key["appen_crowdgen"].priority > by_key["prolific"].priority


def test_low_trust_platforms_carry_warning():
    by_key = {c.key: c for c in load_checklist()}
    assert by_key["remotasks"].warn        # trust ~0.42 < 0.5
    assert not by_key["mercor"].warn
    assert not by_key["telus_oneforma"].warn


def test_pending_excludes_joined(tmp_checklist):
    # Pin a status ourselves — the live file's statuses change as the user
    # signs up, so the test can't assume who is still pending.
    set_status("rws_trainai", "joined", path=tmp_checklist)
    keys = [c.key for c in pending(tmp_checklist)]
    assert "rws_trainai" not in keys
    assert all(c.status == "todo" for c in pending(tmp_checklist))


def test_nudges_are_capped_and_lead_with_top_priority():
    top = pending()[0]                     # whoever currently leads the pool
    nudges = render_nudges(2)
    assert top.display_name in nudges[0]
    heads = [l for l in nudges if l.startswith("•")]
    assert len(heads) == 2


def test_warned_platform_is_nudged_with_badge_never_bare():
    # §10: never nudge a trust<0.5 platform without a warning badge.
    lines = render_nudges(len(load_checklist()))
    remotasks = [l for l in lines if l.startswith("•") and "Remotasks" in l]
    assert remotasks and "⚠" in remotasks[0]


def test_set_status_roundtrip(tmp_checklist):
    set_status("mercor", "joined", path=tmp_checklist)
    by_key = {c.key: c for c in load_checklist(tmp_checklist)}
    assert by_key["mercor"].status == "joined"
    assert "mercor" not in [c.key for c in pending(tmp_checklist)]
    # header survives the rewrite
    assert tmp_checklist.read_text(encoding="utf-8").startswith("# Tier B signup checklist")


def test_set_status_validates(tmp_checklist):
    with pytest.raises(ValueError, match="unknown status"):
        set_status("mercor", "maybe", path=tmp_checklist)
    with pytest.raises(ValueError, match="unknown platform"):
        set_status("clickfarm9000", "joined", path=tmp_checklist)
