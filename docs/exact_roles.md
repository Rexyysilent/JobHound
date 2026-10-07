# Exact public Turing and micro1 roles

V5.5 hydration has two independent, narrow native contracts:
`https://work.turing.com/r/<role-id>` and
`https://jobs.micro1.ai/post/<uuid>`. Discovery must already name that exact
public leaf. No search collector, corporate-feed expansion or account route is
enabled by this change. Existing feature-off and unrelated-host behavior stays
covered by the regression suite.

Turing requires the role heading, matching native metadata URL, one role-column
Overview card and all six role sections. Completed React stream fragments are
resolved as bounded data moves; unfinished outer boundaries remain partial.
The role's privacy, schedule, desktop and evaluation terms remain literal.
Referrer banners, signup panels and general company prose do not enter the role.
The metadata's developers.turing.com alias is accepted only for the same role ID.

micro1 requires the exact public role component, matching component and job IDs,
loaded/error-free state, role-title agreement, scope, preferred qualifications
and native required skills. Flight JSON and length-framed UTF-8 text records are
decoded without running scripts or inventing an API endpoint. A JavaScript boot
shell stays partial. Malformed, conflicting or unsupported templates cannot
fall through to a generic JobPosting and claim completeness.

The typed micro1 role status `closed` produces `explicitly_closed`, even if
generic JobPosting metadata advertises a future validThrough date. Other status
strings remain unknown. A complete public description supplies neither an
individual invitation nor an allocated task: application-route state remains
`account_unknown` and task availability remains `advertised_only`.

Native section labels are interpreted only for these two sources. Turing's
qualifications and micro1's required skills enter requirements; micro1's
preferred qualifications are retained separately. Default section extraction
does not acquire these provider-specific aliases. A displayed hourly interval
is an advertised public range, never an assigned individual rate. A timestamp
without a timezone is retained verbatim in the native payload; posted_at stays
unknown rather than becoming capture time or an assumed UTC timestamp.

The public regression suite exercises native role fragments through the actual
hydrator and engine using mocked HTTP responses and cache reads. It covers
complete/exact roles, closed roles, mismatched identities, ambiguous sections,
partial shells and cached inspection accounting. No live capture or operator
profile is distributed. These fixtures are not coverage of either platform or
proof of an individual offer, allocation or payment. Role extraction excludes
unrelated public referral identity.

Enable collection only after a separate logged source-plan admission, explicit
route scope and an adequately covered bounded check. This parser implementation
does not itself admit a new daily source family or establish source yield.
