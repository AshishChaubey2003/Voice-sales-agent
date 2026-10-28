import json

import pytest
from sqlalchemy.exc import IntegrityError

from api.leads import (
    check_lead_proposal,
    consent_problem,
    is_clear_yes,
    parse_lead_arguments,
    save_lead_from_tool,
)
from api.models import Lead


@pytest.mark.parametrize(
    "text", ["Yes", "yes please", "Sure, go ahead", "Okay", "That's right", "haan"]
)
def test_clear_yes_is_accepted(text):
    assert is_clear_yes(text)


@pytest.mark.parametrize(
    "text", ["No", "Yes but change my email", "wait", "I'm not sure", "", "maybe later"]
)
def test_unclear_or_negative_reply_is_not_consent(text):
    assert not is_clear_yes(text)


def test_consent_requires_the_email_to_be_read_back_before_yes():
    history = [
        {"role": "user", "content": "I'm Rahul, rahul@example.com"},
        {"role": "assistant", "content": "Thanks! Anything else I can help with?"},
        {"role": "user", "content": "Yes"},
    ]

    assert consent_problem(history, "rahul@example.com") is not None


def test_consent_passes_after_read_back_and_clear_yes():
    history = [
        {"role": "user", "content": "I'm Rahul, rahul@example.com"},
        {
            "role": "assistant",
            "content": "To confirm: Rahul, Rahul@Example.com. May our sales team contact you?",
        },
        {"role": "user", "content": "yes"},
    ]

    assert consent_problem(history, "rahul@example.com") is None


def test_consent_rejects_an_email_that_is_only_a_textual_substring_of_the_read_back():
    """LEAD-01: the read-back check must require an exact email token match, not a
    substring. 'sh@oldcorp.com' is textually contained inside the assistant's actual
    email, but it was never itself read back or confirmed, so it must still fail."""
    history = [
        {"role": "user", "content": "I'm Priya, sh@oldcorp.com, please have sales call me"},
        {
            "role": "assistant",
            "content": "Got it, Priya - freshsh@oldcorp.com. May our sales team contact you?",
        },
        {"role": "user", "content": "Yes"},
    ]

    assert consent_problem(history, "sh@oldcorp.com") is not None


def test_invalid_email_is_rejected():
    lead, problem = parse_lead_arguments('{"name": "Rahul", "email": "not-an-email"}')

    assert lead is None
    assert problem


def _corrected_email_history():
    """Visitor corrects the email; the assistant re-reads back only the corrected one."""
    return [
        {"role": "user", "content": "I'm Rahul, rahul@old.com, please have sales call me"},
        {"role": "assistant", "content": "Just to confirm: Rahul, rahul@old.com. May sales contact you?"},
        {"role": "user", "content": "No wait, it's rahul@new.com"},
        {"role": "assistant", "content": "Got it, Rahul, rahul@new.com. May sales contact you?"},
        {"role": "user", "content": "Yes"},
    ]


def test_correction_rejects_a_save_proposing_the_superseded_email():
    """Phase 4M coverage gap (not a defect): the mechanical gate must still refuse to save
    the pre-correction email even though the visitor said yes, because that exact email was
    never the one read back and confirmed. Mirrors the dataset's gate-visitor-change and
    lead-correction-no-save cases, exercised here through the real check_lead_proposal() gate
    rather than only at the is_clear_yes() word-list level."""
    history = _corrected_email_history()
    arguments = json.dumps({"name": "Rahul", "email": "rahul@old.com"})

    lead, problem = check_lead_proposal(history, arguments)

    assert lead is None
    assert problem


def test_correction_accepts_a_save_proposing_the_confirmed_email():
    """The corrected email, once actually read back and confirmed, is accepted - the gate
    is not simply rejecting everything after a correction, only the un-confirmed value."""
    history = _corrected_email_history()
    arguments = json.dumps({"name": "Rahul", "email": "rahul@new.com"})

    lead, problem = check_lead_proposal(history, arguments)

    assert problem is None
    assert lead is not None
    assert str(lead.email) == "rahul@new.com"


def _consented_history():
    return [
        {"role": "user", "content": "I'm Asha, asha@example.com, please have sales call me"},
        {
            "role": "assistant",
            "content": "To confirm: Asha, asha@example.com. May our sales team contact you?",
        },
        {"role": "user", "content": "Yes"},
    ]


class _FakeSessionWithRaceOnFirstFlush:
    """Simulates two concurrent save_lead calls for the same conversation: the real unique
    constraint on (organization_id, conversation_id) lets only one INSERT through, and the
    other must see an IntegrityError on flush - here, raised the first time flush() runs."""

    def __init__(self):
        self.added = []
        self.flush_calls = 0
        self.rollback_calls = 0
        self._row_exists = False

    async def scalar(self, stmt):
        if not self._row_exists:
            return None
        existing = self.added[0]
        return existing

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flush_calls += 1
        if self.flush_calls == 1:
            # The "other" concurrent call's row is now in place.
            self._row_exists = True
            raise IntegrityError("INSERT INTO leads ...", {}, Exception("duplicate key value"))

    async def rollback(self):
        self.rollback_calls += 1


@pytest.mark.anyio
async def test_concurrent_lead_save_is_idempotent_not_an_unhandled_error():
    """LEAD-02: a race between two save_lead calls for the same conversation must not
    surface as an uncaught IntegrityError/500 - the loser of the race should roll back and
    update the row the winner just inserted, still reporting 'saved'."""
    session = _FakeSessionWithRaceOnFirstFlush()
    history = _consented_history()
    arguments = json.dumps({"name": "Asha", "email": "asha@example.com"})

    import uuid

    outcome = await save_lead_from_tool(session, uuid.uuid4(), uuid.uuid4(), history, arguments)

    assert outcome == {"status": "saved"}
    assert session.flush_calls == 2
    assert session.rollback_calls == 1
    assert isinstance(session.added[0], Lead)
    assert session.added[0].email == "asha@example.com"


@pytest.fixture
def anyio_backend():
    return "asyncio"