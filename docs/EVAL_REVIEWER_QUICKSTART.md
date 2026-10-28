# Reviewer quickstart (print this, keep `EVAL_REVIEW_GUIDE.md` for details)

You are one of **two independent reviewers**. You label cases; you do not decide whether the assistant is good. Your labels become the ground truth that later measurements are checked against, so a careful `ambiguous` is worth more than a confident guess.

## Before you start

1. You have your **own copy** of each blank sheet, named `sheet_<your-name>-golden.csv` and `sheet_<your-name>-candidates.csv` in `evals/review/` (never edit `golden_review_sheet_blank.csv` or `candidate_review_sheet_blank.csv`).
2. Open the three files in `knowledge/nimbuscrm/` (`faq.md`, `features.md`, `pricing.md`). **They are the only source of truth.** No general knowledge, no web, no real product.
3. **Do not** open the `.jsonl` datasets, another reviewer's sheet, or `docs/EVAL_POLICY_DECISIONS.md`, and do not discuss cases, until you have submitted both sheets. They contain or imply the author's expected answers.
4. Lead and consent rows: wait until the product owner has confirmed the consent rule (ask the review lead).

## Per row

| Column | Fill with | Short rule |
|---|---|---|
| `answerable_from_kb` | `yes` / `partly` / `no` | `yes`: the files state what a good answer needs. `partly`: only some of it. `no`: absent, or unrelated |
| `evidence_sections` | `Title > Heading; Title > Heading` | Exactly as in the files (`# Title`, `## Heading`). Required for `yes` and `partly` |
| `expected_behavior` | `answer` / `correct_premise` / `decline_or_abstain` / `redirect_to_sales` / `other` | What a *good reply* does |
| `should_save` | `yes` / `no` | Lead rows only |
| `ambiguous_or_exclude` | `keep` / `ambiguous` / `exclude` | `keep` unless you cannot label it with confidence |
| `comment` | text | **Required** for `partly`, `ambiguous`, `exclude`, and when you used arithmetic or inference |

## Decision guide

**answerable_from_kb**
* `yes`: you can point to the heading(s) that contain every fact the answer needs. Simple arithmetic on stated numbers counts, but say so in the comment.
* `partly`: a correct answer would need something the files do not state, or only one of several parts is stated. Comment: which part is missing.
* `no`: nothing relevant, or the question is about something else (other products, general knowledge, the weather). A product word in the question does not make it answerable.

**expected_behavior**
* `answer`: give the information from the files.
* `correct_premise`: the question assumes something the files contradict; a good reply corrects it.
* `decline_or_abstain`: a good reply says it does not have that information (use when `answerable` is `no`, or the missing part is the point).
* `redirect_to_sales`: the right move is to hand the visitor to the sales team (they ask for a person, a custom quote, or something only sales can settle).
* `other`: greetings, small talk, prompt-injection attempts and anything else; explain in the comment.

**should_save** (lead rows only)
* `yes`: under the confirmed consent rule, a lead *should* be stored for this conversation.
* `no`: anything else, including a hesitant, conditional or negative reply and any case where the rule is not met. When in doubt, flag `ambiguous` rather than guess.

**ambiguous_or_exclude**
* `keep`: you are confident in every other label.
* `ambiguous`: two reasonable reviewers could label it differently, or the files are silent/unclear on a point the answer depends on. Say what the two readings are.
* `exclude`: the case is broken: duplicate of another case, nonsense, contradicts the files, or depends on something outside the files. Say why.

## When you are not sure

* Never fill a label you would be guessing. Choose `ambiguous` and write the two readings you considered.
* **Still fill the other columns with your best reading** (the checker needs a complete row). If both reviewers flag a case, those labels are ignored and the case is excluded from validated metrics. If only you flag it, it goes to the adjudicator with your comment.
* A **blank row means "not reviewed"**, not "don't know". Never use blank for uncertainty.
* Changing your mind: fix your own sheet **before** you have seen anyone else's. After that, do not edit; explain in a comment.

## Before you submit (completion checks)

1. Every row is filled (no blank rows) and every row has `ambiguous_or_exclude`.
2. Saved as **CSV UTF-8 (Comma delimited)**, same columns, same rows, same order. Do not rename, delete, sort or add rows. (Excel in some locales writes `;` instead of `,`: that is rejected.)
3. File name: `evals/review/sheet_<your-name>-golden.csv` and `evals/review/sheet_<your-name>-candidates.csv`, with **the same `<your-name>` in both**, matching your `reviewer_id` in `evals/review/reviewers.csv` exactly. Tell the review lead the exact file names: they declare you in that registry (one id per person), and a sheet that is not declared there counts for nothing. A file name is not proof of identity, so never fill in a sheet for someone else or submit two sheets for the same dataset. If the lead assigned cases with `python -m evals.review_assign`, that assignment names which cases you're on; it is a separate thing from your submitted sheet, and submitting a sheet is never skipped just because the assignment exists.
4. Run `python -m evals.review_preflight`. You want **0 errors** and **no pending "blank rows" for your sheet**. Read every warning about evidence sections: a heading that matches nothing in the files is probably a typo.
5. Do not run anything that edits files; the preflight and `python -m evals.review_status --text` are read-only.

Then tell the review lead your sheets are in. You are done until a dispute needs a third person.
