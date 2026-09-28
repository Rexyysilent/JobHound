"""Email offers: platform trust requires an authenticated sender.

Review (2026-09-28): a platform was recognised from the spoofable From header
alone and then given 0.9 listing confidence, and the "platform link" check
chopped the sender host to its last two labels, so for a sender under
example.co.uk any link on attacker.co.uk counted as the platform's.
"""
import email
import email.policy

from jobhound.sources.email_offers import _platform_link, _sender_authenticated, offer_from_message

MAPPING = {'example.com': 'platform_a', 'example.co.uk': 'example_uk'}
TRUSTED = ('mx.google.com',)
GMAIL_PASS = ('mx.google.com;\n dkim=pass header.i=@example.com header.s=s1 header.b=abc;\n'
              ' spf=pass (google.com: domain of bounce@example.com) smtp.mailfrom=bounce@example.com;\n'
              ' dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=example.com')
GMAIL_FAIL = ('mx.google.com;\n spf=fail smtp.mailfrom=spoof@example.net;\n'
              ' dmarc=fail (p=REJECT) header.from=example.com')


def message(sender='Platform <jobs@example.com>', results=(GMAIL_PASS,),
            body='Bengali project at USD 12 per hour. Apply: https://jobs.example.com/p/1'):
    headers = ''.join(f'Authentication-Results: {r}\n' for r in results)
    raw = f'{headers}From: {sender}\nSubject: New project\nContent-Type: text/plain\n\n{body}\n'
    return email.message_from_string(raw, policy=email.policy.default)


def test_dmarc_pass_from_the_trusted_receiver_authenticates():
    assert _sender_authenticated(message(), 'example.com', TRUSTED)


def test_dmarc_fail_or_missing_results_do_not_authenticate():
    assert not _sender_authenticated(message(results=(GMAIL_FAIL,)), 'example.com', TRUSTED)
    assert not _sender_authenticated(message(results=()), 'example.com', TRUSTED)


def test_only_the_topmost_result_from_a_trusted_receiver_counts():
    # A spoofer can plant a "pass" header lower in the message.
    planted = message(results=(GMAIL_FAIL, GMAIL_PASS))
    assert not _sender_authenticated(planted, 'example.com', TRUSTED)
    other_server = message(results=(GMAIL_PASS.replace('mx.google.com', 'mx.attacker.example'),))
    assert not _sender_authenticated(other_server, 'example.com', TRUSTED)


def test_aligned_dkim_pass_authenticates_but_unaligned_does_not():
    aligned = 'mx.google.com;\n dkim=pass header.i=@mail.example.com header.s=s1;\n dmarc=none header.from=example.com'
    assert _sender_authenticated(message(results=(aligned,)), 'example.com', TRUSTED)
    unaligned = 'mx.google.com;\n dkim=pass header.d=attacker.example.net;\n dmarc=none header.from=example.com'
    assert not _sender_authenticated(message(results=(unaligned,)), 'example.com', TRUSTED)


def test_spoofed_platform_email_produces_no_offer():
    offer, reason = offer_from_message(message(results=(GMAIL_FAIL,)), MAPPING,
                                       require_auth=True, trusted=TRUSTED)
    assert offer is None and reason == 'unauthenticated'


def test_authenticated_platform_email_produces_an_offer_with_its_own_link():
    offer, reason = offer_from_message(message(), MAPPING, require_auth=True, trusted=TRUSTED)
    assert reason == 'accepted'
    assert offer['platform_key'] == 'platform_a'
    assert offer['url'] == 'https://jobs.example.com/p/1'


def test_unknown_sender_is_ignored():
    offer, reason = offer_from_message(message(sender='x@example.org'), MAPPING,
                                       require_auth=True, trusted=TRUSTED)
    assert offer is None and reason == 'unknown_sender'


def test_platform_link_uses_the_configured_domain_not_the_last_two_labels():
    body = 'See https://attacker.co.uk/steal and https://jobs.example.co.uk/role/7'
    assert _platform_link(body, 'example.co.uk') == 'https://jobs.example.co.uk/role/7'
    assert _platform_link('Only https://attacker.co.uk/steal', 'example.co.uk') == 'https://example.co.uk'
