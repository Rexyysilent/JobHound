# Reviewed outbound observations

Record a bounce and an alternate send without establishing a buyer reply,
conversion or verified generated address. This uses the existing offline `review outcomes`
import and engine preview. It does not connect a mailbox or send messages.

Schema version 4 adds `outbound: {message_id, contact_id}` with opaque local IDs,
plus `outbound_state` (`sent`, `provider_accepted`, `bounced`) and `contact_origin`
(`generated`, `observed`). Existing exact provider/portal/role/account/attempt
bindings, explicit review, evidence support hash/path, dates and private
confidentiality are mandatory. Addresses and bodies do not belong in these IDs.

`sent` requires actor `user`; acceptance and bounce require actor `provider`.
Facts must come from an explicitly reviewed `mail` or `user_report` observation.
Existing actor/source rules still apply: user reports have actor `user`.
Automatic or reviewed-seed imports cannot establish these observations. An
alternate message/contact has its own identity and cannot supersede another
message's bounce. Existing cross-actor supersession requires an explicit reviewed
edge; a provider acceptance does not silently resolve a user sent observation.
An old bounce does not disappear because a newer event happens to exist.

`outcome_actions.json` exposes `outbound_messages` with current state, origin,
opaque identities and dated evidence references. `verification_state` is always
`unverified_by_outbound_event`: these events establish no contact verification
and say nothing about verification established independently elsewhere. An
acceptance is a reviewed claim, not an authenticated provider receipt or proof
of delivery. Genuine buyer replies keep the existing separate `buyer_reply`
predicate and authority checks.

Outbound evidence and its conflicts remain in journal/projection audit artifacts,
but do not enter action material, discovery reservation or status audiences.
The existing decision, allocation, payment and buyer-reply paths remain separate.
This implementation adds no conversion metric, contact-verification facility,
mailbox authentication, automatic seed adapter or live/runtime integration.
