---
name: jev-crm-triage
description: Sort Wellness by Madeline business records with Jev — social comments into buyers, inbox and CRM threads into reply-needed or not, customers and builders into health groups (churn risk, who to contact this week), meeting sentences into decisions and action items — and turn the labels into CRM proposals a person approves. Use when triaging comments, leads, the inbox, follow-ups or meeting notes for the Madeline CRM. For writing Jev code in general, use jev-evaluation.
---

# Jev CRM triage

Jev labels; the CRM records; a person approves anything that reaches a customer. The
generic recipe (fixed labels, per-record binding, certainty threshold, validation set)
is in `jev-evaluation` → `references/use-cases.md`. This skill is where that recipe meets
the Wellness by Madeline CRM, `WBM_Messenger`, Gmail and Granola.

## Rules that hold in every pipeline

- **Nothing sends from here.** Labels become drafts, tasks, tags or proposals. A CRM
  `send_message`, Gmail send or a public reply in Madeline's or Adam's name needs the
  user's yes, every time (their rule, not Jev's). `WBM_Messenger.draft_outreach` only drafts, so it is the default.
- **Respect opt-outs before drafting.** Check `get_suppressions` (and the person's
  opt-out state) before proposing any outreach. A suppressed person gets no draft.
- **Below the threshold, a person decides.** Start at certainty 0.6; anything under it
  goes to the review list with its top two labels, never to an automatic action.
- **Validate before the first real run.** Label 30–50 past records by hand (or take
  ones whose outcome is known: who bought, who churned), run Jev blind, and report
  agreement and the misses. Tighten label descriptions where it is wrong.
- **Personal details stay out of Jev's state** unless the decision needs them. Send the
  comment or message text and an id, not a full profile. Use `jev_classify_data` first
  when in doubt.
- **No answer from Jev means review**, never "skip" and never "send".

## 1. Buyers in comments

Pile: Facebook or TikTok comments (scraped, or pasted by the user).

| label | meaning |
|---|---|
| `ready_to_buy` | asks price, where to buy, how to order, or says they want it |
| `interested` | positive and curious, no buying signal yet |
| `question` | asks about ingredients, results, shipping, the business |
| `complaint` | unhappy, problem report |
| `other` | spam, tags of friends, small talk |

Downstream: `search_people` to match the commenter; `upsert_person` only when the user
agrees to add them; `draft_outreach` (message_kind first contact) for `ready_to_buy`
and `interested`; `propose_task` for complaints. Output a priority list, buyers first.

## 2. Inbox and CRM threads

Pile: `list_recent_inbound` / `list_communication_threads` (CRM) or Gmail
`search_threads`. Read bodies with `get_communication_thread_content` only for threads
Jev cannot label from the preview.

| label | meaning |
|---|---|
| `needs_reply` | a person asked something or is waiting on Madeline |
| `order_or_account` | orders, shipping, account changes |
| `lead` | new prospect or referral |
| `fyi` | receipts, notifications, newsletters |
| `spam_or_scam` | unsolicited offers, phishing, fake brand deals |

Downstream: only `needs_reply` and `lead` reach Claude or `draft_outreach`; save
replies with `save_message_draft`. Labelling in Gmail (`label_thread`) or marking
spam changes a shared system, so it is proposed, not done.

## 3. Customer and builder health

Pile: people from `list_follow_ups`, `list_stale_leads` and `get_person_context`
(tenure, last order, care-cycle stage, recent contact). Pass a compact row per person.

`Score` over `["healthy", "needs_attention", "at_risk"]`, one question per person.
Downstream: a weekly "who to contact" list ordered by `at_risk` probability;
`propose_task` or `propose_outreach_plan` per person, approved by the user. Never
change a stage (`set_person_stage`, `transition_customer_care_stage`) on Jev's label
alone.

## 4. Meeting sentences

Pile: a Granola transcript (`get_meeting_transcript`), split into sentences or turns.

| label | meaning |
|---|---|
| `decision` | something was agreed |
| `action_item` | someone will do something |
| `risk` | a concern, objection or blocker |
| `question` | open question to follow up |
| `other` | everything else |

Downstream: a recap grouped by label; action items become `propose_task`; the recap
feeds `record_post_meeting_review` after the user confirms it.

## Reporting a run

Give counts per label, the review list with reasons, validation agreement if a set was
run, and the proposals awaiting approval. Do not paste every record back.
