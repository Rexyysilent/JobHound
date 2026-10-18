# Dated language declarations

The optional profile `language_evidence` list separates language capability from
task preferences. It does not infer either from a biography or a job posting, and
existing profiles need no changes.

Each record contains:

- `language`: a language name.
- `modality`: `general`, `spoken` or `written`.
- `level`: `unknown`, `basic`, `fluent`, `advanced` or `native`.
- `stance`: `capability`, `avoid` or `allow`.
- `evidence_id` and `source`: explicit local provenance references.
- `event_date`: an ISO calendar date, or null when the date is unknown.

For example, a fictional profile can retain an older general Hindi declaration
and a newer preference to avoid advanced written Hindi work. That preference does
not revoke spoken Hindi capability. A later capability statement does not silently
retract a task preference; use a separately dated preference declaration.

The V5.5 engine evaluates supported language-linked requirements at the frozen
run date. Spoken or general ability does not establish advanced editorial writing.
An explicitly supported alternative in an OR-language requirement remains usable.
Same-date contradictions and undated uncertainty require verification. Future
declarations do not change past decisions. Evidence order does not change decisions.

The requirements assessment retains applicable records, dates, selected IDs and
the interpreted task scope. Re-evaluation rebuilds that projection for the current
profile. Immutable run contexts preserve the records without sharing mutable state.

The modality reader handles bounded explicit constructions, including editor,
writer and proofreader roles. It is not a general natural-language proficiency
assessment. No real profile evidence is supplied or imported automatically; public
defaults remain neutral. The legacy policy does not use these new declarations.
