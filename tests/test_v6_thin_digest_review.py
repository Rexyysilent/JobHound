"""Review of commit 0836247 (thin-digest fixes): accounting, Connects and seller routes."""
import json

from jobhound.delivery_transport import render_job_summary
from jobhound.v41.documents import classify_document
from test_v6_delivery_recovery import EMAIL, TELEGRAM, release, stage, store  # noqa: F401
from test_v6_thin_digest_fixes import _cost, _many_eligible


def ledger(eligible=10, already=0, selected=10, listed=0, overflow=None, status_already=0):
    return {"counts": {"already_recorded": already + status_already, "selected_cards": selected,
                       "overflow_cards": listed},
            "cards": {"eligible": eligible, "already_recorded": already, "selected": selected,
                      "listed": listed},
            "overflow": [] if overflow is None else overflow}


def test_eligible_counts_cards_only_not_recorded_status_items():
    # Finding 1: 10 new cards plus 6 already-recorded status items is 10 eligible.
    text = render_job_summary(ledger(status_already=6), carded_here=10,
                              card_states={"pending": 10})
    assert "10 eligible" in text and "16 eligible" not in text


def test_held_status_items_never_make_the_card_count_negative():
    # Finding 2: holds are counted from this run's card intents only.
    text = render_job_summary(ledger(eligible=2, selected=2), carded_here=2,
                              card_states={"pending": 2})
    assert "2 newly queued" in text and "-" not in text.split("Jobs this run")[1].split(";")[1]


def test_adopted_legacy_cards_are_reported_as_suppressed_not_held():
    # Finding 3: production adopts legacy holds (status accepted) before rendering.
    text = render_job_summary(ledger(eligible=8, selected=8), carded_here=3,
                              card_states={"accepted": 5, "pending": 3})
    assert "3 newly queued" in text
    assert "5 suppressed as already sent before V6" in text
    assert "held for" not in text
    held = render_job_summary(ledger(eligible=3, selected=3), carded_here=1,
                              card_states={"legacy_hold": 2, "pending": 1})
    assert "2 held for legacy review" in held


def test_cards_in_the_email_are_counted_from_the_email_itself():
    # Finding 4: an email can carry cards left pending by an earlier run.
    text = render_job_summary(ledger(eligible=3, already=3, selected=0), carded_here=3,
                              card_states={})
    assert "This email carries 3 job cards" in text
    assert "3 already queued or sent in an earlier run" in text
    assert "carded above" not in text


def test_reports_without_card_accounting_make_no_run_claims():
    old = {"counts": {"already_recorded": 2, "selected_cards": 3, "overflow_cards": 74},
           "states": {"legacy_hold": 1}}
    text = render_job_summary(old, carded_here=2, card_states={})
    assert "This email carries 2 job cards" in text
    assert "Jobs this run" not in text and "held for" not in text
    assert "74 more eligible jobs are in the local run audit" in text


def test_overflow_line_marks_unsafe_urls_and_uppercases_any_band():
    # Finding 10.
    entries = [{"title": "Role", "company": "Co", "host": "", "url": "", "band": "primary",
                "source": "original_ats"}]
    text = render_job_summary(ledger(eligible=2, selected=1, listed=1, overflow=entries),
                              carded_here=1, card_states={"pending": 1})
    line = next(l for l in text.splitlines() if l.startswith("• "))
    assert line.startswith("• PRIMARY | Role | Co")
    assert line.endswith("URL unavailable")


def test_staged_ledger_accounts_cards_and_renders_from_current_states(store):
    value = _many_eligible()
    stage(store, value, card_cap=15, status_cap=5)
    report = json.loads(store.conn.execute("SELECT report FROM delivery_runs").fetchone()[0])
    card = report["destinations"][0]
    assert card["cards"] == {"eligible": 12, "already_recorded": 0, "selected": 5, "listed": 7}
    assert len(card["card_intent_ids"]) == 5
    # Mimic production's legacy adoption of one selected card before rendering.
    store.conn.execute("UPDATE delivery_intents SET status='accepted' WHERE id=?",
                       (card["card_intent_ids"][0],))
    from jobhound.delivery_transport import DigestTransport
    transport = DigestTransport(store.delivery)
    ids = [r["id"] for r in store.delivery.inspect() if r["status"] == "pending"]
    body = transport.inspect(transport.prepare(EMAIL, ids, now=101,
                                               summary_run_id=value.metadata.run_id))["body"]
    assert ("Jobs this run: 12 eligible; 4 newly queued, 1 suppressed as already sent "
            "before V6, 7 listed below, 0 already queued or sent in an earlier run.") in body
    assert "This email carries 4 job cards" in body


def test_overflow_order_is_the_digest_order_and_entries_are_built_once(store, monkeypatch):
    # Findings 8 and 9.
    from jobhound import delivery_outbox
    from jobhound.v41.digest import release_order_key
    calls = []
    real = delivery_outbox.overflow_entry
    monkeypatch.setattr(delivery_outbox, "overflow_entry",
                        lambda item: calls.append(item.canonical.canonical_id) or real(item))
    value = _many_eligible()
    stage(store, value, targets=(EMAIL, TELEGRAM), card_cap=15, status_cap=5)
    report = json.loads(store.conn.execute("SELECT report FROM delivery_runs").fetchone()[0])
    assert len(calls) == len(set(calls)) == 7
    by_title = {row.job.title: row for row in value.evaluated}
    listed = [by_title[e["title"]] for e in report["destinations"][0]["overflow"]]
    assert listed == sorted(listed, key=release_order_key)


def test_upwork_connects_wordings_count_as_bid_cost():
    # Finding 5: Upwork's own phrasings, reached through a non-Upwork URL.
    for text in ("Required Connects to submit a proposal: 16",
                 "Connects: 16",
                 "Sending a proposal costs 1 Connect.",
                 "This job requires 6 Connects to apply."):
        assert _cost(f"Looking for an n8n expert. {text}").action_cost_acceptable is False, text
    for text in ("Mercor connects elite creative and technical talent with AI labs.",
                 "Prolific connects researchers with participants.",
                 "We connect talent with meaningful work."):
        assert _cost(text).action_cost_acceptable is True, text


def test_other_upwork_profile_routes_are_sellers():
    # Finding 6.
    for path in ("/agencies/~01abc", "/ag/acme-automation", "/o/profiles/users/~01abc",
                 "/freelancers/juliansmith"):
        result = classify_document("Acme - n8n automation agency", "Top Rated Plus.",
                                   "https://www.upwork.com" + path)
        assert (result.document_type, result.actionable) == ("seller_service", False), path
    assert classify_document("Find freelancers", "", "https://www.upwork.com/freelancers").document_type \
        == "talent_directory"
    assert classify_document("n8n job", "Need a developer",
                             "https://www.upwork.com/freelance-jobs/apply/n8n_~01/").document_type \
        == "buyer_request"
