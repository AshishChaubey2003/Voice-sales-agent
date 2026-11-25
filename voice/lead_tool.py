import asyncio
import json
import uuid
from collections.abc import Callable

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams

from api.leads import SAVE_LEAD_TOOL, save_lead_from_tool
from voice.rag import conversation_turns

VOICE_LEAD_RULES = """Voice lead capture rules:
- Email addresses are easy to mishear. Ask the visitor to spell the part before the @ sign letter by letter, then read the full address back slowly and ask if it is correct.
- If the visitor corrects any detail, read the corrected version back again before saving.
- Keep each of these steps to one short sentence."""


class PendingLeadSaves:
    """VOICE-01: lets a disconnect handler wait briefly for an in-flight save_lead commit
    instead of cancelling it mid-write.

    This is explicit task ownership, not a blind ``asyncio.shield``: the save task itself
    is untouched (it either finishes or is genuinely cancelled by the caller, unchanged),
    this object only tracks whether one is currently in flight so the disconnect path can
    choose to wait, with a bounded timeout so a stuck save can never block shutdown
    forever. ``save()`` reports success exactly as before - only after the database
    confirms it - this only changes *when* a cancellation can land relative to that.
    """

    def __init__(self) -> None:
        self._count = 0
        self._idle = asyncio.Event()
        self._idle.set()

    def enter(self) -> None:
        self._count += 1
        self._idle.clear()

    def exit(self) -> None:
        self._count = max(0, self._count - 1)
        if self._count == 0:
            self._idle.set()

    async def wait_idle(self, timeout: float) -> bool:
        """True once nothing is in flight; False if ``timeout`` elapses first."""
        if timeout <= 0:
            return self._idle.is_set()
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False


def build_save_lead_schema() -> FunctionSchema:
    function = SAVE_LEAD_TOOL["function"]
    return FunctionSchema(
        name=function["name"],
        description=function["description"],
        properties=function["parameters"]["properties"],
        required=function["parameters"]["required"],
    )


def build_save_lead_handler(
    *,
    session_factory,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    on_result: Callable[[str], None] | None = None,
    pending: PendingLeadSaves | None = None,
):
    """Pipecat calls this when the LLM asks for save_lead; the consent gate still decides.

    ``pending``, when supplied, is marked in-flight for exactly the DB write (VOICE-01) so
    a caller such as the disconnect handler can wait for it instead of cancelling it mid-
    write. It is released as soon as the write finishes (committed, rejected, or errored),
    before any of the result-reporting below runs.
    """

    async def handler(params: FunctionCallParams) -> None:
        history = conversation_turns(params.context.get_messages())
        if pending is not None:
            pending.enter()
        try:
            async with session_factory() as session:
                outcome = await save_lead_from_tool(
                    session,
                    organization_id,
                    conversation_id,
                    history,
                    json.dumps(dict(params.arguments)),
                )
                if outcome["status"] == "saved":
                    await session.commit()
        except Exception:
            logger.exception("save_lead failed")
            outcome = {
                "status": "rejected",
                "reason": "The details could not be saved right now. Apologise and offer to try again.",
            }
        finally:
            if pending is not None:
                pending.exit()

        logger.info(f"save_lead: status={outcome['status']}")
        if on_result:
            on_result(outcome["status"])
        await params.result_callback(outcome)

    return handler