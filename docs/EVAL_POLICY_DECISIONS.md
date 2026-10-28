# Evaluation policy decisions for the product owner

**Audience: the product owner and the review lead. Reviewers must not read this file before they have submitted their sheets**: it quotes the dataset's current expected outcomes, which would bias their labels (see `EVAL_REVIEW_GUIDE.md`).

Two decisions block the first *validated* baseline. Neither blocks the review itself: the 111 knowledge-base cases (51 golden + 60 candidates) can be labelled now; only the 24 lead and consent cases (18 `lead_gate`, 6 `lead_llm`, all in the golden set) wait for decision A. Nothing here was decided by the tooling, and no dataset label was changed.

Everything below marked *observed* was produced offline by calling the repository's own `api/leads.py` with hypothetical conversations; no model, network or database was involved.

---

## Owner decision questionnaire and record (Phase 4J, corrected Phase 4K)

**Correction (Phase 4K):** an earlier draft of this section said "each in the same eight fields," which miscounted its own cards against the nine fields the brief actually specifies (Decision ID, Current status, Exact question, Available options, Owner's selected answer, Rationale, Approval identity and date, Downstream implementation impact, Tests and evidence required). No field was ever missing from the cards below — only the prose describing them was wrong, and it is fixed here rather than silently left standing. Each card below carries all nine required items: the **heading above the card** (e.g. "A-Q1. Permission wording") *is* its Decision ID; **Status** is Current status; **Question** is Exact question; **Options** is Available options; **Owner's answer**, **Rationale**, **Approved by / date**, **Downstream impact** and **Tests & evidence required** map one-to-one to the remaining five required fields. Two further bold fields — **Current behaviour** and **Recommendation** — appear on most cards for context; they are not part of the required nine and a card is not incomplete without them.

Read **Question** and **Options**, write your answer and reasoning into **Owner's answer** and **Rationale**, sign **Approved by / date**, and the card is done — everything else (the full analysis, the code, the consequences) is already filled in and lives in the detailed section this card points to. Filling in a card here is the record of the decision in prose; it is **not** the same as writing a row to `evals/review/policy_approvals.csv` (see "How to record a decision once you have made it" below) — the CSV is a separate, optional machine-readable attestation that a person later made this same choice, and no runtime code reads this document either way. **No option below has been selected.** A "Recommendation" line is this report's own proposal, never an approved decision, and is never itself sufficient to mark a card answered.

Nothing here is new policy invented by the tooling — every option, consequence, and recommendation already existed in the detailed sections below (B, written in Phase 4G/4H; A, written earlier and expanded in Phase 4H/4I) and is only gathered into one place here for the owner's convenience.

**Once these cards are answered, the exact steps to start genuine reviews — real reviewer registration, identity verification, assignment of the 111 in-scope knowledge-base cases (kept separate from the 24 lead/consent cases), review-sheet generation, independent review, dispute resolution, readiness re-checks, and when validated metrics may finally be calculated — are already written down, command-by-command, in `EVAL_REVIEW_GUIDE.md`, section 19 ("Review-lead runbook: end to end, with exact commands"). That section is not duplicated here; it is the single place that workflow lives.**

---

### A-Q1. Permission wording

- **Status:** `OPEN`
- **Question:** Must the agent explicitly ask permission to save the lead's details, or is a read-back followed by a clear yes sufficient?
- **Current behaviour:** `api/leads.py::consent_problem()` never checks for a permission question; it only checks that the prior assistant message contains the proposed email, then runs `is_clear_yes()` on the reply. See "What the system does today" under Decision A below.
- **Options:** (a) keep today's behaviour — no explicit permission-question check; (b) require the assistant's message to contain an explicit permission phrase before a "yes" counts.
- **Recommendation (proposal only):** (a), unless the owner has a specific wording requirement in mind — the gap it leaves (a confirmed detail read back without ever being asked "may we contact you") is narrow and already named under "Where the code and the stated intent can disagree."
- **Owner's answer:** `_______________`
- **Rationale:** `_______________`
- **Approved by / date:** `_______________`
- **Downstream impact:** `api/leads.py::consent_problem()`; one new `lead_gate` case if (b) is chosen. Full detail: Decision worksheet, Q1, below.
- **Tests & evidence required before "implemented":** a passing new case for the scenario named above, plus every existing `gate-*`/`lead-*` case re-confirmed unchanged (no case tests this distinction today).

### A-Q2. Read-back content

- **Status:** `OPEN`
- **Question:** Must the agent repeat the email only, or the person's name and email?
- **Current behaviour:** only email containment is checked, case-insensitively; the name is never compared to anything.
- **Options:** (a) keep email-only (today); (b) require both name and email to appear in the read-back and match the proposal.
- **Recommendation (proposal only):** (a) if speed-to-ready matters more than the narrow name-mismatch gap; (b) if the owner considers "confirmed the wrong person's name" a real risk worth a code change and a new test case.
- **Owner's answer:** `_______________`
- **Rationale:** `_______________`
- **Approved by / date:** `_______________`
- **Downstream impact:** `api/leads.py::consent_problem()`; one new `lead_gate` case (name mismatch) if (b) is chosen. Full detail: Decision worksheet, Q2, below.
- **Tests & evidence required before "implemented":** a passing new name-mismatch case, plus `gate-email-case-insensitive` re-confirmed unaffected.

### A-Q3. Clear affirmative

- **Status:** `OPEN`
- **Question:** Which responses qualify as consent? Define how ambiguous affirmatives, trailing content, corrections, and negative responses should be handled.
- **Current behaviour:** `AFFIRMATIVE_START` is a fixed whitelist (`yes, yeah, yep, yup, sure, correct, right, ok, okay, confirm, confirmed, go ahead, please do, that's right, that is right, haan, han, ji`), `NEGATIVE_WORDS` a fixed blacklist (`no, not, don't, dont, wait, wrong, change, stop`); the reply must be ≤80 characters and **start with** a whitelisted word.
- **Options:** (a) keep the list and the 80-character cutoff as-is; (b) extend the whitelist (the owner names exactly which new words/phrases); (c) drop or raise the 80-character cutoff so a leading "yes" is accepted regardless of trailing content.
- **Recommendation (proposal only):** (a) is the conservative default — it already rejects plausible-but-unlisted affirmatives ("absolutely", "bilkul") in favour of never saving on an ambiguous reply. Extending the list (b) is reasonable but the exact words must come from the owner, not be guessed. Loosening the cutoff (c) trades missed leads for a higher false-save risk and is not recommended without a specific business reason.
- **Owner's answer:** `_______________`
- **Rationale:** `_______________`
- **Approved by / date:** `_______________`
- **Downstream impact:** `api/leads.py::is_clear_yes()` (`AFFIRMATIVE_START`/`NEGATIVE_WORDS`/the cutoff). Full detail: Decision worksheet, Q3, below.
- **Tests & evidence required before "implemented":** one positive and one negative case per newly-accepted or newly-rejected phrase; `gate-long-ramble` re-tested explicitly if the cutoff changes; `gate-visitor-change`/`gate-correction-no-save`-style correction cases re-confirmed still rejected.

### A-Q4. Consent ordering

- **Status:** `OPEN`
- **Question:** Must permission be requested after the read-back, or may a single utterance contain both the details and consent?
- **Current behaviour:** `consent_problem()` requires the second-to-last message to be the assistant's read-back and the last to be the visitor's clear yes; consent in the same message as the details, with no prior read-back, is rejected today (`gate-no-readback`, `lead-details-without-readback`).
- **Options:** (a) keep the strict ordering (today); (b) accept same-message consent-plus-details under a condition the owner specifies exactly (e.g. only when the visitor explicitly writes "please contact me").
- **Recommendation (proposal only):** (a) — it is the safer default against saving data the visitor didn't clearly agree to in the moment, and no case currently exercises a safe version of (b).
- **Owner's answer:** `_______________`
- **Rationale:** `_______________`
- **Approved by / date:** `_______________`
- **Downstream impact:** `api/leads.py::consent_problem()`; `gate-no-readback`/`lead-details-without-readback` expectations would flip under (b). Full detail: Decision worksheet, Q4, below.
- **Tests & evidence required before "implemented":** a new case proving the owner's exact (b) condition, plus a case confirming the strict rule still applies when that condition isn't met.

### A-Q5. Language scope

- **Status:** `OPEN`
- **Question:** Which languages are supported, and what should happen when consent cannot be confidently interpreted?
- **Current behaviour:** `AFFIRMATIVE_START` happens to include `haan`, `han`, `ji` (Hindi/Hinglish) alongside English, but no language-scope decision has ever been recorded — this is incidental, not declared policy. Spanish "si" and German "ja" are named as **not** recognised today (a lost lead, never a false save).
- **Options:** (a) declare the scope is "English and Hindi/Hinglish only" (names today's code, changes nothing); (b) add a defined list of further languages, each with its own word lists and at least one new case; (c) add an explicit "cannot be interpreted" fallback (e.g. always reject rather than guess) for anything outside the declared scope.
- **Recommendation (proposal only):** (a) now, with (c) added if the owner wants an explicit, auditable fallback rather than the current silent "just doesn't match, so it's rejected" behaviour — these two are compatible and can be adopted together.
- **Owner's answer:** `_______________`
- **Rationale:** `_______________`
- **Approved by / date:** `_______________`
- **Downstream impact:** `api/leads.py::is_clear_yes()`. Full detail: Decision worksheet, Q5, below.
- **Tests & evidence required before "implemented":** one case per newly-supported language's affirmative and negative reply; one case for the fallback behaviour if (c) is chosen.

### A-6. Overall approach (which packaged option, 1-4)

- **Status:** `OPEN`
- **Question:** Independently of Q1-Q5's individual answers, which of the four packaged options under "Options and consequences" (below) describes the intended direction: keep as-is, tighten, loosen, or tighten-and-extend?
- **Options:** see "Options and consequences" under Decision A below (four options, each cross-referencing Q1-Q5).
- **Owner's answer:** `_______________`
- **Rationale:** `_______________`
- **Approved by / date:** `_______________`
- **Downstream impact:** none beyond Q1-Q5's individual impacts — this card exists only to record the overall direction in one place, not to add a new code path.
- **Tests & evidence required before "implemented":** none beyond what Q1-Q5 individually require.

### B. Correctness verification policy

- **Status:** `OPEN`
- **Question:** Who verifies `required_facts` (independence requirement), what evidence is needed per fact (e.g. a quoted sentence in a named heading), how many verifiers, how are fact disagreements settled, and at what threshold does a fact count as verified?
- **Current behaviour:** `required_facts`/`forbidden_facts`/`forbidden_regex` are author-written keywords; no reviewer sheet checks them against the knowledge base. The correctness judge already runs only for cases where `facts_review_status == "reviewed"` (fixed in Phase 4G/4H) — but no evidence format for *performing* that review exists yet, so no case can actually reach `facts_review_status: reviewed` through a real, evidenced process today.
- **Options:** see "Options" under Decision B below — (1) keep keyword facts as smoke checks only (cheapest, correctness stays provisional permanently); (2) add a facts-review stage after the label review (recommended order); (3) replace facts with written reference answers (highest quality, most reviewer effort).
- **Recommendation (proposal only):** option 2, sequenced after the label review — this is the existing recommendation in the detailed section below, restated here, and is still only a proposal.
- **Owner's answer:** `_______________`
- **Rationale:** `_______________`
- **Approved by / date:** `_______________`
- **Downstream impact:** a new facts-review sheet/stage and a `evals.review_status` extension to verify it (option 2), or a reference-answer authoring effort (option 3). No existing code path is torn out; this is new, additive work either way.
- **Tests & evidence required before "implemented":** new tests for whichever stage is built, plus confirmation that `facts_review.validated` only becomes true through real recorded evidence, never a bare flag (already enforced for the flag-alone case; the new evidence format needs its own test once designed).

### C1. Self-adjudication

- **Status:** `OPEN`
- **Question:** May the two reviewers settle their own disputes when no third person is available? The tooling currently refuses unconditionally: a resolver must hold no sheet.
- **Options:** keep the strict rule (a resolver always needs to be a third, uninvolved person); or allow self-adjudication explicitly for named cases, with that fact recorded in the resolution (a code change).
- **Recommendation (proposal only):** keep the strict rule — it is already unconditionally enforced and is the safer default for evaluation integrity; relaxing it needs a specific, recorded justification from the owner, not a default.
- **Owner's answer:** `_______________`
- **Rationale:** `_______________`
- **Approved by / date:** `_______________`
- **Downstream impact:** `evals/review_identity.py`'s `resolver_has_sheets` check would need a deliberate, recorded exception path if relaxed. No change needed to keep the strict rule.
- **Tests & evidence required before "implemented":** if relaxed, a new test proving the exception applies only to the named, recorded cases and not generally; the existing strict-rule tests must keep passing for every other case.

### C2. Resolution evidence

- **Status:** `OPEN`
- **Question:** What counts as sufficient `resolution_evidence`? The tooling currently only requires it to be non-blank; the content is not judged.
- **Options:** leave it as a free-text citation (today); or require a heading that provably exists in the knowledge files (a code change, cross-checked against `knowledge/nimbuscrm/`).
- **Recommendation (proposal only):** leave as free text for now, given no dispute has ever been recorded in the real `evals/review/` folder to test this against — revisit once real disputes exist and a pattern of weak evidence (if any) is observed.
- **Owner's answer:** `_______________`
- **Rationale:** `_______________`
- **Approved by / date:** `_______________`
- **Downstream impact:** `evals/review_status.py`'s `row_problems()` non-blank check would need a heading-existence check added if tightened.
- **Tests & evidence required before "implemented":** a new test asserting a resolution citing a nonexistent heading is rejected, plus the existing non-blank tests kept passing.

### C3. Identity attestation

- **Status:** `OPEN`
- **Question:** Who may attest that reviewer ids correspond to distinct real people, given that no cryptographic or external identity system exists? `reviewers.csv`'s `attested_by` is currently one person's word.
- **Options:** accept a single named attester (today's `attested_by` field, unconditionally); or require a second person to co-sign each identity.
- **Recommendation (proposal only):** accept a single attester for now, proportionate to the stakes of an internal evaluation review — revisit if the review process is ever opened to reviewers outside a trusted, known group.
- **Owner's answer:** `_______________`
- **Rationale:** `_______________`
- **Approved by / date:** `_______________`
- **Downstream impact:** `evals/review_identity.py::load_registry()` would need a second `co_attested_by` column and a check if tightened.
- **Tests & evidence required before "implemented":** a new test requiring both signatures when the stricter rule is adopted; existing single-attester tests would need deliberate updating, not silent removal.

**Recording an answer in a card above is the decision; it is not an implementation, a code change, a label change, or a completed review.** Each card's "Downstream impact" and "Tests & evidence required" rows are the minimum that must *additionally* happen, separately, before that decision is "implemented" in the sense `evals/review_policy.py`'s `DECISIONS["<id>"]["implemented"]` field or this document's language use that word.

### Is A-6 redundant with A-Q1–A-Q5? (Phase 4K)

**No — they answer different questions, but the structure as written lets someone pick inconsistent answers, and that risk needs a stated guard rather than silent trust.**

A-Q1 through A-Q5 are the five binding, individually-testable decisions: each one maps to a specific function, a specific set of options, and a specific set of new test cases. A-6 is not a sixth independent decision about behaviour — it is a one-line *summary label* for whichever combination of Q1–Q5 answers the owner ends up choosing, drawn from the pre-existing "Options and consequences" table under Decision A below (option 1 "keep as is" ≈ every sub-question answered "(a)"; option 2 "tighten" ≈ Q1 and/or Q2 answered "(b)"; option 3 "loosen" ≈ Q3 answered "(b)"/"(c)"; option 4 "tighten and extend" ≈ both). Nothing in `evals/review_policy.py` reads an "A-6" id at all — the code only ever recognises one lumped id, `"A"` — so A-6 has no independent mechanical effect; its only purpose is to let the owner and the review lead sanity-check, at a glance, that the five detailed answers add up to a coherent overall direction before anyone starts implementing.

**The conflict this allows:** because A-6 and Q1–Q5 are recorded as six separate blank fields with nothing that cross-checks them, an owner could, for example, answer A-6 "Option 1 — keep as is" while also answering A-Q3 "(b) extend the whitelist" — a change that is not "keep as is" under any reading of the options table. Nothing in this document or in `evals/review_policy.py` would catch that contradiction; a CSV row for decision `A` would be accepted by `review_policy.py` as long as `approved_by`/`approved_on`/`reference` are filled in, regardless of whether the prose behind it is self-consistent.

**Proposed guard (procedural, not a code change, and not required before the owner answers):** before any `evals/review/policy_approvals.csv` row for decision `A` is written, the review lead re-reads all six A-cards together and confirms the A-6 label is the one the "Options and consequences" table would actually assign to the Q1–Q5 answers as written; if it is not, the lead asks the owner to reconcile them (either change A-6's label or change the sub-question that doesn't fit) before recording the approval. This is the same "no second row is merged, a mismatch reverts the decision to open" discipline `review_policy.py` already applies to duplicate rows — extended here to internal consistency within decision `A`, by a human check rather than a code check, since the code has no visibility into A-6 at all. No A-card is deleted, merged, or renumbered to produce this guard; all six stay separately traceable.

**Same granularity note applies to the CSV, not only to A-6:** `evals/review_policy.py`'s `DECISIONS` dict recognises exactly five ids — `A`, `B`, `C1`, `C2`, `C3` — never `A-Q1` … `A-Q5` or `A-6`. The six A-cards above are a human-readable breakdown for decision-making; the machine-readable record in `policy_approvals.csv` is still one row for all of `A` at once. Practically: the owner and review lead settle all six A-cards first, confirm them mutually consistent (the guard above), and only then is a single `A` row written, with its `reference` field pointing at this document so the six sub-answers remain inspectable behind that one row.

## Owner-vs-engineering traceability (Phase 4K)

One row per decision. "Owner decides" is the plain-language choice; everything to its right is what an engineer would need to act on that choice — none of it is a reason to answer the question any particular way, and none of it is itself an approval.

| Decision | Owner decides | Code/eval behaviour that depends on it | Files likely affected | Tests to add/update | Evidence of successful implementation | Independent, or depends on another decision? |
|---|---|---|---|---|---|---|
| A-Q1 Permission wording | Whether an explicit permission question is required before a "yes" counts | `consent_problem()`'s gate logic for every `lead_gate`/`lead_llm` case | `api/leads.py`; `evals/datasets/nimbuscrm_golden_v1.jsonl` (new case only, no existing label touched) | New `lead_gate` case for "detail read back, no permission asked, visitor says yes"; all existing `gate-*`/`lead-*` cases re-run unchanged | New case passes under the chosen rule; full `tests/test_leads.py` + eval-harness subset still green | Independent of Q2–Q5, A-6, B, C1–C3; **A-6 must end up consistent with whatever is chosen here** (see note above) |
| A-Q2 Read-back content | Whether the read-back must match the name as well as the email | Same `consent_problem()` gate | `api/leads.py`; one new case | New `lead_gate` name-mismatch case; `gate-email-case-insensitive` re-confirmed unaffected | New case passes; no existing case's expected outcome changes | Independent of Q1/Q3/Q4/Q5; feeds A-6 |
| A-Q3 Clear affirmative | The exact word list, the length cutoff, and how corrections/negatives are handled | `is_clear_yes()` | `api/leads.py`; new cases per added/removed word or cutoff change | One positive + one negative case per newly accepted/rejected phrase; `gate-long-ramble` re-tested if the cutoff changes; correction cases (`gate-visitor-change`-style) re-confirmed still rejected | All new and existing affected cases pass; no silent change to an unrelated case's outcome | Independent of Q1/Q2/Q4; **the owner's answer here is what most often decides which of Options 1–4 (A-6) applies**, since loosening the word list/cutoff is "Option 3/4" by definition |
| A-Q4 Consent ordering | Whether consent and the read-back may occur in one utterance | Same `consent_problem()` gate | `api/leads.py` | New case proving the owner's exact same-utterance condition; `gate-no-readback`/`lead-details-without-readback` re-confirmed for when the condition isn't met | New case passes under the stated condition; the strict-ordering cases keep their current expected outcome unless the owner explicitly says they should flip | Independent of Q1/Q2/Q3/Q5; feeds A-6 |
| A-Q5 Language scope | Which languages are in scope and what happens outside that scope | `is_clear_yes()`'s word lists; whether a fallback-reject path is added | `api/leads.py` | One case per newly supported language's affirmative and negative reply; one fallback-behaviour case if a "cannot be interpreted" path is added | All new language cases pass; `lead-hinglish-confirm` unaffected unless its language is explicitly redefined | Independent of Q1–Q4; feeds A-6 |
| A-6 Overall option (1–4) | Which packaged direction (keep/tighten/loosen/both) the five sub-answers add up to | Nothing by itself — no id "A-6" exists in `evals/review_policy.py` | None directly | None directly | Internal consistency with Q1–Q5 (the guard above), not a new test | **Depends on** Q1–Q5 being answered first; not independently implementable |
| B Correctness verification policy | Who verifies `required_facts`, what evidence per fact, how disputes over facts are settled | `evals/judge_suite.py`'s `_facts_ready()` gate (already built); the review stage/evidence format itself (not built) | New: a facts-review sheet format and reviewer workflow; `evals/review_status.py` (extension to verify the new evidence); possibly `evals/review_sheets.py` | New tests for whichever stage is chosen (option 2 or 3); a test that `facts_review.validated` only flips on real recorded evidence, never a bare flag | A real facts-review pass recorded for at least one case, verified against the knowledge base, with the dataset's `facts_review_status`/`facts_reviewer` fields set honestly | Independent of A and C1–C3; its own stage (option 2) is sequenced *after* the label review in the staged plan below, not before |
| C1 Self-adjudication | Whether a resolver may ever hold a sheet for a case they are resolving | `review_identity.py`'s `resolver_has_sheets` check (already enforced unconditionally) | `evals/review_identity.py` only if relaxed; otherwise none | If relaxed: a new test proving the exception applies only to named, recorded cases, never generally | If relaxed: at least one real exception case recorded and traceable; if kept strict: no new evidence needed, since the rule already runs | Independent of A, B, C2, C3 |
| C2 Resolution evidence | Whether free-text evidence is enough or a verifiable heading citation is required | `review_status.py`'s `row_problems()` non-blank check (already enforced); a heading-existence check (not built) | `evals/review_status.py` only if tightened | If tightened: a new test asserting a resolution citing a nonexistent heading is rejected | If tightened: at least one real resolution checked against `knowledge/nimbuscrm/` and accepted/rejected correctly; if kept as-is: no new evidence needed | Independent of A, B, C1, C3 |
| C3 Identity attestation | Whether a single named attester is sufficient or a co-signer is required | `review_identity.py::load_registry()` (single-attester today) | `evals/review_identity.py` only if a co-signer is required | If tightened: a new test requiring both signatures; existing single-attester tests updated deliberately, not silently removed | If tightened: at least one real registry entry with both signatures present and checked; if kept as-is: no new evidence needed | Independent of A, B, C1, C2 |

No policy answer was chosen on the owner's behalf anywhere in this table — every "Owner decides" cell states the open question, never a preferred answer, and every other column is conditional engineering consequence, not advocacy for one option over another.

## Staged implementation plan (Phase 4K — nothing below is started)

**This plan exists so the owner can see, before answering anything, what each decision actually costs to implement. No stage below has been started. No stage may begin until its own prerequisite decision(s) are answered and recorded — building a policy-dependent code path ahead of the owner's answer is exactly what this phase's standing constraints forbid.**

**Stage 1 — Lead consent behaviour (Decision A, all of A-Q1–A-Q5 plus the A-6 consistency check)**
- *Prerequisites:* A-Q1 through A-Q5 all answered and mutually consistent with the A-6 label (see the guard above); a single `policy_approvals.csv` row for `A` recorded once settled.
- *Acceptance criteria:* `api/leads.py::consent_problem()`/`is_clear_yes()` implement exactly the five answered rules, no more and no less; every pre-existing `gate-*`/`lead-*` case keeps its current expected outcome unless the owner explicitly said it should change (and if so, the dataset label change is itself called out as a separate, reviewed edit, not a side effect); each new rule has at least one new, passing case.
- *Likely files:* `api/leads.py`; `evals/datasets/nimbuscrm_golden_v1.jsonl` (new rows / explicit, named label changes only); `tests/test_leads.py`.
- *Tests:* the full existing `tests/test_leads.py` suite re-run and green; one new test per changed rule; the eval-harness subset re-run to confirm no case's mechanical pass/fail flipped unintentionally.

**Stage 2 — Verification of author-written `required_facts` (Decision B)**
- *Prerequisites:* B answered (option 1, 2 or 3).
- *Acceptance criteria:* if option 1 (keep as smoke checks), no code change is needed — this stage is "implemented" by documenting that correctness stays provisional and stopping there; if option 2 or 3, a real facts-review evidence format exists, is enforced by `evals/review_status.py`, and at least one case has gone through it with genuine, checkable evidence (a quoted sentence in a named heading, per the existing minimum-evidence spec below Decision B).
- *Likely files:* `evals/review_status.py`; a new facts-review sheet template (parallel to the existing blank label-review sheets); possibly `evals/review_sheets.py` if a generator is wanted; `docs/EVAL_REVIEW_GUIDE.md` for the new step in the runbook.
- *Tests:* new tests proving `facts_review.validated` only becomes `true` through the new evidence path, never a bare flag (extending the existing flag-alone test); a test that an unverified or missing-evidence fact keeps `facts_review_status` at `unreviewed`.

**Stage 3 — Dispute adjudication and evidence requirements (Decisions C1 and C2)**
- *Prerequisites:* C1 and C2 each answered. These two are independent of each other and of C3; either may be settled and implemented without waiting for the other.
- *Acceptance criteria:* if both are kept at today's strict defaults, no code change is required — this stage's "evidence" is simply the recorded approval rows plus the pre-existing passing tests for the unconditional rules; if either is relaxed/tightened, the corresponding new check in `evals/review_identity.py` (C1) or `evals/review_status.py` (C2) exists, is covered by a new test, and does not weaken the rule for any case the owner didn't name an exception for.
- *Likely files:* `evals/review_identity.py` (C1); `evals/review_status.py` (C2); `knowledge/nimbuscrm/` is read, never written, if C2's heading-existence check is added.
- *Tests:* new tests scoped exactly to the named exception/tightening; full `tests/test_review_identity.py` and the dispute-handling tests in `tests/test_review_workflow.py` re-run green.

**Stage 4 — Reviewer identity attestation (Decision C3)**
- *Prerequisites:* C3 answered.
- *Acceptance criteria:* if kept at a single attester, no code change — the existing unconditional `review_identity.py` check already matches that policy; if a co-signer is required, `reviewers.csv`'s schema gains the new column, `load_registry()` enforces it, and at least one real registry entry demonstrates both signatures present and checked.
- *Likely files:* `evals/review_identity.py`; `docs/reviewers.csv.example` (template updated if the schema changes); `docs/EVAL_REVIEW_GUIDE.md` section 17 (the column reference).
- *Tests:* `tests/test_review_identity.py` extended for the new column if added; existing single-attester tests updated deliberately (never silently deleted) if the policy tightens.

**Stage 5 — Regression tests and readiness verification**
- *Prerequisites:* Stages 1–4 completed for whichever decisions were actually changed (a decision kept at its default needs no stage-5 work beyond confirming nothing regressed).
- *Acceptance criteria:* the full offline suite (same `--deselect` set used throughout this project, for the GROQ-env-gated modules) passes with no net loss of passing tests versus the pre-change baseline; `python -m evals.review_status --require-ready` is re-run and its blockers list is read, not assumed, after every stage; no dataset fingerprint changes unless a label change was explicitly approved by the owner as part of that decision.
- *Likely files:* none beyond what Stages 1–4 already touched; this stage is verification, not new production code.
- *Tests:* full `pytest -q --deselect tests/test_auth.py --deselect tests/test_chat.py --deselect tests/test_create_api_key.py --deselect tests/test_voice_admission.py`; `review_identity`/`review_assign --check`/`review_preflight`/`review_status` re-run read-only.

**Stage 6 — Final validation: policy approvals and validated metrics represented accurately**
- *Prerequisites:* Stages 1–5 complete; real reviewers registered and real reviews completed per `EVAL_REVIEW_GUIDE.md` §19 (a separate, human-driven track that does not depend on Stages 1–5, but `ready_for_validated_metrics` depends on both tracks together).
- *Acceptance criteria:* every `policy_approvals.csv` row that exists has a real `approved_by`/`approved_on`/`reference`, matches a decision that was actually implemented (not just recorded) where implementation was needed, and `review_preflight`'s `policy_open` findings reflect the true remaining set, not a stale one; `review_status`'s `ready_for_validated_metrics` is read directly from a fresh run, never asserted from memory, before any number is called "validated"; any report produced at this stage states plainly which gates passed and which did not, with no rounding of "ready" language.
- *Likely files:* none — this is a verification stage, re-running the existing read-only tools (`review_status`, `review_preflight`, `review_identity`) against the real `evals/review/` folder.
- *Tests:* `python -m evals.review_status --require-ready` (exit code is the acceptance signal, not prose); `python -m evals.review_preflight --strict`.

---

## A. Lead consent: what counts as consent to be contacted?

### What the system does today

The gate in `api/leads.py` (`consent_problem`, `is_clear_yes`) saves a proposed lead only when all of these hold, looking at the **last two messages only**:

1. The name and email are valid (`name` non-empty, `email` well formed, `team_size` 1 to 100 000 if given).
2. The visitor's last message is the final message, and the assistant message just before it **contains the proposed email address** (case-insensitive).
3. The visitor's reply is at most **80 characters**, **starts with** one of `yes, yeah, yep, yup, sure, correct, right, ok, okay, confirm, confirmed, go ahead, please do, that's right, that is right, haan, han, ji`, and contains none of the words `no, not, don't, dont, wait, wrong, change, stop`.

The prompt (`LEAD_CAPTURE_RULES`) separately tells the model to repeat the name and email back and ask whether the sales team may contact them. The README says the lead is stored "only if the assistant read the name and email back and the visitor clearly agreed". The code does **not** check that the assistant asked for permission, nor the name.

### What the evaluation dataset expects (golden set, 24 cases)

| Cases | Conversation | Expected |
|---|---|---|
| `gate-yes`, `gate-yeah-sure`, `gate-haan`, `gate-okay`, `gate-email-case-insensitive` | Read-back with permission question, then "Yes" / "yeah sure" / "haan" / "okay, go ahead" (the last one reads the email back in capitals) | saved |
| `gate-no-readback`, `gate-last-turn-is-assistant` | No read-back before the proposal / the conversation ends on the assistant's question | rejected |
| `gate-assistant-omits-email` | Assistant asks "may our sales team contact you?" without repeating the email; visitor says "Yes" | rejected |
| `gate-email-not-read-back` | Assistant says "Confirming Rahul, rahul@example.com?"; visitor "Yes"; the model proposes a **different** email | rejected |
| `gate-visitor-declines`, `-wait`, `-change`, `-vague-reply`, `-long-ramble` | "No, don't contact me" / "wait, that email is wrong" / "yes but change the email" / "hmm maybe later" / a "yes and also..." message over 80 characters | rejected |
| `gate-malformed-json`, `-invalid-email`, `-missing-name`, `-zero-team-size` | Valid consent, invalid proposal | rejected |
| `lead-confirm-saves`, `lead-hinglish-confirm` | Live model; read-back, then "Yes" / "haan" | lead stored |
| `lead-decline-no-save`, `lead-correction-no-save`, `lead-wants-call-no-details`, `lead-details-without-readback` | Decline / correction / no details yet / **"Yes please have sales call me. I'm Rahul, ..." before any read-back** | no lead |

Reviewers will answer `should_save` for these rows from the rule you confirm. The measurements `lead.gate_correct` and `lead.gate_false_save` then show whether the code matches **your** rule, not merely the author's.

### Where the code and the stated intent can disagree (all *observed*)

| Situation | Today | Why it matters |
|---|---|---|
| Prompt "Your email is rahul@example.com, right?" then "Yes" | **saved** | The visitor confirmed an address, not permission to be contacted. No dataset case covers it; `gate-email-not-read-back` uses a similar prompt but is rejected only because the proposed email differs |
| Proposal uses a different **name** than the one read back | **saved** | The name is never checked |
| Prompt names two emails, either one proposed | **saved** | Only containment is checked |
| "Yes, and my number is 555" / "yes. also what does Pro cost?" | **saved** | Any short reply starting with "yes" is consent, whatever else it says, while `gate-long-ramble` (same shape, over 80 characters) is rejected: the cut-off, not the meaning, decides |
| "yes, no problem" / "yes, but not today" | rejected | The word list cannot tell the harmless "no problem" from a real refusal |
| "absolutely", "definitely", "sounds good", "bilkul", "y", "ja", "si" | rejected | Plausible consent not recognised (lost leads, never false saves) |
| "ji" alone, "ok", "right" | **saved** | "ji" and "ok" are often only acknowledgements ("I heard you") |

### The decision

State, in one sentence each, what the product accepts as consent. The five sub-decisions:

1. **Permission wording.** Must the assistant's read-back *ask* whether sales may contact the visitor, or is repeating the details and getting a "yes" enough?
2. **What must be read back.** Email only (today), or name and email both, matching what is saved?
3. **What counts as a clear yes.** Keep the strict short word list; extend it (e.g. "absolutely", Hindi/Hinglish variants, other languages); or accept a leading yes followed by something else.
4. **Consent before the read-back.** Is "Yes please have sales call me. I'm Rahul, ..." sufficient, or must the visitor confirm after hearing the details back (today: must confirm)?
5. **Languages in scope.** Which languages' affirmations must the gate recognise?

### Options and consequences

| Option | What changes | Consequence |
|---|---|---|
| **1. Keep the gate as is; the dataset is already consistent with it** | Nothing in code. Reviewers use the current behaviour as the rule | No false saves from ambiguity in the dataset's own cases; still the gaps in the table above, and any missed consent is a lost lead. The measured "gate correct" then describes the code's own rule |
| **2. Tighten (fewer false saves)** | Require a permission question and/or matching name; keep or shrink the word list | Fewer risky saves; more missed leads; new expected labels for the rows in the table above (e.g. `gate-email-not-read-back`'s prompt would be insufficient even with a matching email) |
| **3. Loosen (fewer missed leads)** | Accept more affirmations and "yes + more" replies | More leads; a higher chance of saving personal data without clear consent; `gate-long-ramble` and `gate-visitor-change` expectations would need a deliberate rewrite |
| **4. Tighten and extend recognition** | Both of the above | Needs new test cases (none exist for permission wording, name mismatch, extra content) and a new review of the consent rows |

Which option is right is a product and legal-risk decision (storing a name and email is personal data). I have not chosen one and have not changed any code or expected label.

### What happens meanwhile

* Reviewers label every non-consent row now.
* The 24 consent rows stay unlabelled; the datasets cannot reach `ready` until they are labelled (or deliberately excluded by two reviewers).
* If you choose option 2, 3 or 4, the code and the expected labels are changed in a separate, reviewed change **before** the consent rows are labelled, so reviewers see the final rule.

### Decision worksheet (Phase 4H — all five questions OPEN)

One row per sub-decision from "The decision" above. Nothing here was answered by the tooling; every `OPEN` field is the product owner's to fill in. Recording an answer in this worksheet is not the same as recording it in `evals/review/policy_approvals.csv` (see "How to record a decision once you have made it" below) — the worksheet is where the choice is made and written down in prose; the CSV is where a person later attests that it was made. Neither file is read by any runtime code: see "What the code guarantees now" under Decision B and the module docstring of `evals/review_policy.py` for why an approval row by itself changes no behaviour.

**Q1. Permission wording.** Must the assistant's read-back *ask* whether sales may contact the visitor, or is repeating the details and getting a "yes" enough?
- *Current state:* `consent_problem()` never checks for a permission question; it only checks that the prior assistant message contains the proposed email, then runs `is_clear_yes()` on the next reply. The prompt (`LEAD_CAPTURE_RULES`) instructs the model to ask, but nothing in code enforces it.
- *Options:* (a) keep today's behaviour — read-back + yes is sufficient, no permission-question check; (b) require the assistant's message to contain an explicit permission phrase (new `consent_problem()` check) before a "yes" counts.
- *Consequence:* (a) no code change, but "Prompt 'Your email is rahul@example.com, right?' then 'Yes'" keeps being **saved** even though no permission was asked (see "Where the code and the stated intent can disagree" above); (b) needs new dataset cases for "detail confirmed, no permission asked, visitor says yes" — none exist today, so the mechanical gate test suite would need new fixtures before the new rule could be measured.
- **Owner decision: `OPEN — _______________`**
- *Depends on this:* `api/leads.py::consent_problem()`; no existing case isolates this question from Q2, so a (b) decision needs at least one new `lead_gate` case before it can be tested.

**Q2. Read-back content.** Must the read-back repeat the email only (today), or the name and email both, matching what will be saved?
- *Current state:* only email containment is checked, case-insensitively; the name is never compared to anything.
- *Options:* (a) keep email-only; (b) require both name and email to appear in the read-back and match the proposal.
- *Consequence:* (a) "Proposal uses a different name than the one read back" keeps saving (documented gap, no case covers it); (b) needs a new `consent_problem()` name-match check and at least one new case exercising a name mismatch.
- **Owner decision: `OPEN — _______________`**
- *Depends on this:* `api/leads.py::consent_problem()`; no existing case tests a name mismatch.

**Q3. Clear-yes definition.** Keep the strict short word list, extend it (e.g. "absolutely", more Hindi/Hinglish forms, other languages), or accept a leading "yes" even when followed by more content?
- *Current state:* `AFFIRMATIVE_START` is a fixed whitelist; `NEGATIVE_WORDS` is a fixed blacklist; the reply must be ≤80 characters and **start with** a whitelisted word.
- *Options:* (a) keep as-is; (b) extend the whitelist (document which new words); (c) drop or raise the 80-character cutoff so a leading "yes" is accepted regardless of trailing content.
- *Consequence:* (a) "absolutely", "definitely", "sounds good", "bilkul", "y", "ja", "si" stay rejected (lost leads, never false saves) and `gate-long-ramble` (a "yes and also…" message over 80 characters) stays rejected; (b)/(c) need the exact new word list or cutoff rule from the owner, plus new cases, before `is_clear_yes()` can change — guessing the list is exactly the "invent consent labels" this phase must not do.
- **Owner decision: `OPEN — _______________`**
- *Depends on this:* `api/leads.py::is_clear_yes()`; cases `gate-haan`, `gate-long-ramble`, `gate-vague-reply`, `gate-visitor-change`, `gate-visitor-wait`, `lead-hinglish-confirm`.

**Q4. Consent ordering.** Must permission be established strictly *after* a read-back (today), or may a visitor's first message carry both the details and consent together?
- *Current state:* `consent_problem()` requires the second-to-last message to be the assistant's read-back and the last message to be the visitor's clear yes — consent in the same message as the details, with no prior read-back, is rejected.
- *Options:* (a) keep the strict ordering; (b) accept same-message consent-plus-details under conditions the owner specifies (e.g. only when the visitor explicitly says "please contact me").
- *Consequence:* (a) `gate-no-readback`, `gate-last-turn-is-assistant`, `lead-details-without-readback` stay rejected (today's behaviour, already covered by tests); (b) would flip the expected outcome of those cases and needs the owner's exact condition, not a guess.
- **Owner decision: `OPEN — _______________`**
- *Depends on this:* `api/leads.py::consent_problem()`; cases `gate-no-readback`, `gate-last-turn-is-assistant`, `lead-details-without-readback`.

**Q5. Languages in scope.** Which languages' or language combinations' affirmations must the gate recognise, and what should happen when consent cannot be reliably interpreted (e.g. an unsupported language, or a mixed-language reply)?
- *Current state:* `AFFIRMATIVE_START` happens to include `haan`, `han`, `ji` (Hindi/Hinglish tokens) alongside English words, but no language-scope decision has ever been recorded — this is incidental, not a declared policy. Exactly one dataset case (`lead-hinglish-confirm`) exercises a non-English reply. Spanish "si" and German "ja" are explicitly named as **not** recognised today (lost leads, not false saves).
- *Options:* (a) declare the scope is "English and Hindi/Hinglish only" (describes today's code, doesn't change it); (b) add a defined list of additional languages, each needing its own affirmative/negative word lists and at least one new case; (c) add an explicit "cannot be interpreted" fallback (e.g. always reject rather than guess) for anything outside the declared scope.
- **Owner decision: `OPEN — _______________`**
- *Depends on this:* `api/leads.py::is_clear_yes()` (`AFFIRMATIVE_START`/`NEGATIVE_WORDS`); case `lead-hinglish-confirm`; no case exists for an unsupported-language reply, so (c) would need a new case before it could be measured either.

### Traceability: the 24 lead and consent cases (Phase 4H)

None of the 24 cases below is policy-validated: every row is scored only against the *current code's own behaviour*, which itself has not been approved by the product owner (Decision A, above, is entirely open). "Mechanically correct" means the code does today exactly what the dataset expects; it does not mean the expectation itself is the business-approved rule. No label, fingerprint, or expected value was changed to produce this table.

| Case ID | Kind / category | Expected gate | Expected lead save | Decision this depends on | Mechanically validatable today? | Owner input still missing |
|---|---|---|---|---|---|---|
| `gate-yes` | lead_gate | saved | — | Q3 (clear-yes word "Yes") | yes — matches current code | Q1–Q5 policy approval |
| `gate-yeah-sure` | lead_gate | saved | — | Q3 ("yeah sure" start) | yes | Q1–Q5 policy approval |
| `gate-haan` | lead_gate | saved | — | Q3, Q5 (Hindi/Hinglish) | yes | Q1–Q5 policy approval |
| `gate-okay` | lead_gate | saved | — | Q3 ("okay, go ahead") | yes | Q1–Q5 policy approval |
| `gate-email-case-insensitive` | lead_gate | saved | — | Q2 (email read back in capitals) | yes | Q1–Q5 policy approval |
| `gate-no-readback` | lead_gate | rejected | — | Q4 (no read-back before consent) | yes | Q1–Q5 policy approval |
| `gate-assistant-omits-email` | lead_gate | rejected | — | Q1, Q2 (permission asked, email never repeated) | yes | Q1–Q5 policy approval |
| `gate-visitor-declines` | lead_gate | rejected | — | Q3 (clear "no") | yes | Q1–Q5 policy approval |
| `gate-visitor-wait` | lead_gate | rejected | — | Q3 (negative word "wait") | yes | Q1–Q5 policy approval |
| `gate-visitor-change` | lead_gate | rejected | — | Q3 (negative word "change") | yes | Q1–Q5 policy approval |
| `gate-vague-reply` | lead_gate | rejected | — | Q3 (no whitelisted start) | yes | Q1–Q5 policy approval |
| `gate-long-ramble` | lead_gate | rejected | — | Q3 (80-character cutoff) | yes | Q1–Q5 policy approval |
| `gate-last-turn-is-assistant` | lead_gate | rejected | — | Q4 (conversation ends before consent) | yes | Q1–Q5 policy approval |
| `gate-malformed-json` | lead_gate | rejected | — | none (structural: bad JSON) | yes — structural, not a policy question | none; already policy-independent |
| `gate-invalid-email` | lead_gate | rejected | — | none (structural: `EmailStr` validation) | yes — structural | none; already policy-independent |
| `gate-missing-name` | lead_gate | rejected | — | none (structural: required field) | yes — structural | none; already policy-independent |
| `gate-zero-team-size` | lead_gate | rejected | — | none (structural: `team_size` ≥ 1) | yes — structural | none; already policy-independent |
| `gate-email-not-read-back` | lead_gate | rejected | — | Q2 (proposed email differs from the one read back) | yes | Q1–Q5 policy approval |
| `lead-confirm-saves` | lead_llm | — | True | Q3 (clear-yes word "Yes") | yes (needs a live LLM to actually run) | Q1–Q5 policy approval |
| `lead-decline-no-save` | lead_llm | — | False | Q3 (clear "No thanks") | yes (needs a live LLM to actually run) | Q1–Q5 policy approval |
| `lead-wants-call-no-details` | lead_llm | — | False | none (structural: no name/email given yet) | yes (needs a live LLM to actually run) | none; already policy-independent |
| `lead-details-without-readback` | lead_llm | — | False | Q4 (consent before any read-back) | yes (needs a live LLM to actually run) | Q1–Q5 policy approval |
| `lead-hinglish-confirm` | lead_llm | — | True | Q3, Q5 (Hinglish "haan") | yes (needs a live LLM to actually run) | Q1–Q5 policy approval |
| `lead-correction-no-save` | lead_llm | — | False | Q4 (correction before confirming) | yes (needs a live LLM to actually run) | Q1–Q5 policy approval |

"Mechanically validatable today" for the `lead_gate` rows means `evals.evaluators.evaluate_lead_gate()` — pure code, already covered by `tests/test_leads.py` and the real dataset run — produces the expected outcome; it was not re-run against a live model for this table. The `lead_llm` rows additionally need a live LLM call to execute end to end (out of scope for this offline phase; see the standing constraint against live model calls) — their expectations were checked only by re-reading the schema and the code's documented rules, not by running them. The four structural `gate-*` rows (`gate-malformed-json`, `gate-invalid-email`, `gate-missing-name`, `gate-zero-team-size`) and `lead-wants-call-no-details` do not depend on any of Q1–Q5 at all — they test input validation and "nothing to propose yet", which the Phase 4H "policy-independent work" tests in this round already cover for the schema side (see `tests/test_eval_schema.py`'s new `expected_lead_email` checks, which close an unrelated structural gap in the schema, not in these cases).

### Post-approval implementation plan (Phase 4I — do not build ahead of the owner's answer)

This is the minimum work for **whichever** option the owner picks per question — not a set of pre-built branches for every possible choice. Until an answer lands in the worksheet above, none of this is started.

| Question | If the owner keeps today's behaviour | If the owner changes it | Always needed once answered |
|---|---|---|---|
| **Q1** Permission wording | No code change; worksheet entry records the choice | Add a permission-phrase check to `api/leads.py::consent_problem()`; needs the exact accepted phrasing from the owner, not a guess | New `lead_gate` case: detail confirmed, no permission asked, visitor says yes (today's only gap here is undocumented, not yet tested) |
| **Q2** Read-back content | No code change | Add a name-match check to `consent_problem()` alongside the existing email check | New `lead_gate` case: proposal uses a different name than the one read back |
| **Q3** Clear-yes definition | No code change | Edit `AFFIRMATIVE_START`/`NEGATIVE_WORDS`/the 80-character cutoff in `api/leads.py` to the owner's exact list/rule | Positive cases for each newly-accepted phrase, negative cases confirming the list isn't over-broadened, at least one case re-testing `gate-long-ramble`'s cutoff if it changes |
| **Q4** Consent ordering | No code change | Relax the `turns[-2]`/`turns[-1]` adjacency check in `consent_problem()` under the owner's exact condition | Ordering cases: consent-with-details-in-one-message (today's `gate-no-readback`/`lead-details-without-readback` expectations would flip), plus a case confirming the strict case still rejects when the condition isn't met |
| **Q5** Language scope | No code change (today's Hindi/Hinglish support stays incidental, now declared instead of accidental) | Add the owner's specific word lists per language to `is_clear_yes()`; add an explicit "cannot interpret" fallback if requested | One case per newly-supported language's affirmative and negative reply; one case for the fallback behaviour on an unsupported language |

**For every question, regardless of which option is picked:**
1. Update the expected outcome (`expected_gate` / `expected_lead_saved`) on exactly the cases the traceability table lists against that question — no other case's label changes.
2. Re-run `tests/test_leads.py` and the full offline suite; a changed expectation must change a test, never just a comment.
3. Re-run `evals.schema.load_dataset()` against both real datasets to confirm the edited `.jsonl` still parses and recompute the fingerprint (it **will** change — that is expected and must be reported, not hidden).
4. Record the approved decision as prose in this worksheet (the `OPEN` field becomes the answer) — this is separate from, and does not require, a `policy_approvals.csv` row; recording one there, if the owner also wants that record, still changes no runtime behaviour (see Decision B's "What the code guarantees now" and `evals/review_policy.py`'s docstring).
5. The 24 cases only become reviewer-assignable, and the datasets can only then include them toward `ready`, after this implementation work lands **and** two independent reviewers label them — policy approval and code implementation are necessary but not sufficient for that; see "What happens meanwhile" above.

Nothing in this plan is implemented yet. No hypothetical branch for an unchosen option exists in code.

---

## B. Correctness: are the `required_facts` trustworthy reference material?

### Finding (verified in the dataset files)

No. They are **author-written expectations**. Evidence:

* `required_facts`, `forbidden_facts` and `forbidden_regex` are fields the dataset author filled in from the three knowledge files. Golden: 40 of 51 knowledge-base cases have `required_facts` (54 facts, 14 written with `|` alternatives), 2 have `forbidden_facts`, 8 `forbidden_regex`. Candidates: 37 of 60 have `required_facts` (50 facts, 18 with alternatives), 9 `forbidden_regex`. All 135 cases are `unreviewed`; no case has a `reviewer`.
* **The reviewer sheets do not review these fields.** Reviewers label answerability, evidence sections, behaviour and (lead rows) `should_save`. `evals.review_status` compares only `expected_sources`, `expect_abstain`, `expected_gate` and `expected_lead_saved` with the dataset. So a case can be fully reviewed and approved while its `required_facts` remain a guess.
* The facts are matched as plain text (case-insensitive, whole word or number, `|` separates alternatives) or given to a judge as "the answer should convey ...". Neither validates them.

### What was wrong in the code (now fixed, with tests)

1. The correctness judge in `--judge-suite` treated any case marked `reviewed` as having a reference built from its `required_facts`. After the sheet-based review that would have given **author keywords the authority of reviewed ground truth**.
2. The older `--judge` mode scored correctness against `required_facts` for **every** case, reviewed or not.
3. The metric registry would have shown every metric, including these, as `measured` (validated) once a dataset was ready.

### What the code guarantees now

* A new optional dataset field pair, `facts_review_status` (`unreviewed` / `reviewed`) and `facts_reviewer`, records that a named human has checked the case's required facts. Absent means `unreviewed`.
* Correctness is judged **only** for a case that is `reviewed` **and** has `facts_review_status: reviewed` and `required_facts`. Otherwise the new judge records `skipped_missing_input` and the older `--judge` drops its correctness score. In both cases nothing unverified is sent to a model as truth.
* `answer.required_facts`, `answer.forbidden_clear`, `run.pass_rate` and `judge.correctness` / `eval.correctness` are **never** `measured` from dataset readiness alone: the registry reports `provisional` with the reason. Every LLM-judge score (faithfulness, hallucination, relevance, tone, safety, correctness) stays `provisional` because no judge-versus-human agreement exists.
* No evidence format for fact review exists yet, so `python -m evals.review_status` reports `facts_review.validated: false` and the registry keeps fact-based metrics provisional **even if** someone sets `facts_review_status: reviewed` by hand. A flag alone is not evidence.

### Minimum evidence to enable correctness scoring safely

All of these, recorded in the repository:

1. The case is `approved` or `resolved` and marked `reviewed` (the existing process).
2. For each case, a **written reference**: either an approved fact list or a short reference answer. Each fact traced to a quoted sentence in a named heading of the knowledge files.
3. Checked **independently by at least two people** who did not write the facts, with disagreements settled and recorded the same way as label disagreements (nothing overwritten).
4. Done **after** the label review, on approved cases only, so the facts do not bias the independent labels.
5. The dataset records `facts_review_status: reviewed` and the reviewers' names, **and** the status tool can verify that evidence (to be built); only then does the registry allow fact-based metrics to be `measured`.
6. For any LLM-judge score to be called more than a signal: a human-scored sample chosen in advance, with the judge's agreement with it reported next to the judge's score, and the judge preferably not from the same model family as the assistant.

### Options

| Option | Effect |
|---|---|
| **1. Keep keyword facts as smoke checks only** | Cheapest. `required_facts`, `pass_rate` and judge correctness stay provisional permanently; retrieval, abstention and consent-gate metrics can still be validated |
| **2. Add a facts-review stage (recommended order: after the label review)** | A second blank sheet per approved case listing each fact with "supported by this sentence: yes/no/partly, which heading, rewrite" columns; same two-reviewer rules; status tool extended to verify it. Enables correctness scoring on reviewed cases |
| **3. Replace facts by written reference answers** | Highest quality for the correctness judge; most reviewer effort; the keyword checks could then be derived from the reference |

**Recommendation:** option 2, sequenced after the label review (it needs approved cases anyway, and showing the facts earlier would bias reviewers). Until then treat every correctness-related number as a smoke signal. This is a recommendation; the choice is the product owner's.

---

## How to record a decision once you have made it

Recording a decision here in prose does not change any tool's behaviour; nothing reads this file. The
tooling (`evals/review_policy.py`, run by `review_preflight` and `review_status`) reads only
`evals/review/policy_approvals.csv`, which does not exist yet and which this milestone's code never
creates or writes - only you or the review lead does, by hand:

```
decision_id,approved_by,approved_on,reference
C3,<your name>,2026-11-02,<link or doc where the decision itself is written down>
```

One row per decision (`A`, `B`, `C1`, `C2`, `C3`); `approved_by`, `approved_on` (`YYYY-MM-DD`) and
`reference` must all be filled or the row is reported as a problem and the decision stays open. A second
row for a decision already recorded is **not merged** - it is flagged and the decision reverts to open,
so two people cannot silently overwrite each other's approval.

Recording an approval here changes nothing in `evals/review_status.py`'s readiness computation: that module
never imports `review_policy.py` and never reads `policy_approvals.csv`. Approving a decision is a sign-off
for people to read, surfaced only as an informational `policy_open` finding in `review_preflight`'s output -
it is not a switch any code checks.

| Decision | What approving it actually does | Still needs a separate, reviewed code change |
|---|---|---|
| A (lead consent) | records the owner's decision; changes no behaviour by itself | yes - the gate/expected-label change described above, before the 24 rows are labelled |
| B (facts verification) | records the owner's decision; changes no behaviour by itself | yes - the facts-review stage (option 2) or reference answers (option 3) do not exist yet |
| C1 (self-resolution) | records that the owner agrees with the rule already enforced in code (a resolver must hold no sheet); a dataset's `ready` state is identical whether or not this row exists | no, the strict rule already runs unconditionally in `review_status.py` |
| C2 (resolution evidence) | records that the owner agrees `resolution_evidence` being non-blank is the policy; changes no code behaviour | no, same unconditional enforcement as C1 |
| C3 (identity attestation) | records that the owner agrees attestation-based identity (no cryptographic check) is the policy; a dataset's `ready` state is identical whether or not this row exists | no, identity is already checked via `review_identity.py` unconditionally |

`python -m evals.review_preflight` reports every open decision (`policy_open`, severity `info`, never
blocking); nothing here is decided for you by running a command.

## Decisions to record

Superseded by "Owner decision questionnaire and record (Phase 4J)" near the top of this document
(immediately after the intro) — each decision now has its own full card there (ID, status, exact
question, options, owner's answer, rationale, approval identity/date, downstream impact, tests/evidence
required) instead of a single row here. This section is kept only as a one-glance status index; the
cards are the actual record.

| Card | Decision | Status |
|---|---|---|
| A-Q1 | Permission wording required in the read-back? | open |
| A-Q2 | Read back email only, or name and email? | open |
| A-Q3 | Clear-yes policy (word list, "yes + more", languages) | open |
| A-Q4 | Consent before read-back sufficient? | open |
| A-Q5 | Languages in scope | open |
| A-6 | Overall packaged option chosen (1 to 4) | open |
| B | Correctness verification policy (option 1, 2 or 3; who reviews facts) | open |
| C1 | May the two reviewers settle their own disputes when no third person exists? | open |
| C2 | What is sufficient `resolution_evidence`? | open |
| C3 | Who may attest that reviewer ids are distinct humans (`attested_by`)? | open |

## C. Review process decisions the tooling had to take conservatively

These do not block the review. The tooling chose the strict reading so that nothing is validated by accident; the product owner may relax it.

* **C1 Self-adjudication.** An earlier guide allowed the two reviewers to settle disputes together when no third person existed. With a reviewer registry the tooling treats a resolver who filled a sheet as an error, so reviewer independence is enforceable. Options: keep it strict (needs a third person), or allow it explicitly for named cases with the fact recorded in the resolution (a code change).
* **C2 Resolution evidence.** Required to be non-blank; the content is not judged. Options: leave it as a free-text citation, or require a heading that exists in the knowledge files.
* **C3 Identity attestation.** No external identity system exists; `reviewers.csv` is an attestation by the named person. Options: accept it, or require a second person to co-sign it.
* **Adjudication rule.** The documented rule is that a third person decides; it is not a majority vote, and the final labels are recorded explicitly. If you want a different rule (for example "the lead decides if the reviewers cannot agree after discussion") say so; it was not invented here.
