"""Reading a WhatsApp chat export with the same extraction as screenshots.

The export is parsed and its owner worked out without AI (``app.chat_import``). The AI then gets the
chat as numbered lines, with the photos sent in it, and answers with the same schema as for
screenshots. The same mapping turns that into a draft, so evidence, review flags and every figure
follow the same rules, and ChatReceipt's own code still does all the arithmetic.

What the export settles is not left to the AI: when the file shows who the other person is, that
name is the other party, whatever the model read.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Sequence

from app.chat_import import Attachment, ChatExport, transcript
from app.config import Settings
from app.extraction.prompt import SYSTEM_PROMPT
from app.extraction.provider import ExtractionProviderError, ImagePart, StructuredRequest, TextPart
from app.extraction.schema import EXTRACTION_SCHEMA, AIEvidence, AIExtraction, AIField
from app.extraction.service import build_extraction_response, clean_text, parse_ai_answer
from app.intelligence.conversation import NoteSeverity, Side
from app.models import ChatParticipant, ChatSummary, ExtractionResponse, ReviewReason
from app.names import phone_in_label
from app.observability import log_event
from app.uploads import PreparedImage, UploadedFile, UploadRejected, prepare_screenshots

EXPORT_GUIDE = """\
This is a WhatsApp chat export, not screenshots, so there are no bubbles: each line names its \
sender. Source 1 is the chat: the participants first, then numbered messages ("[m12] p1 · 10:38 · \
text"). Photos sent in the chat follow as sources 2 and up, each labelled with its message.
- The phone's owner, who exported the chat, is marked in the participant list when known. Use \
that for owner_role and is_owner. When nobody is marked, leave owner_role empty unless the \
messages make plain who exported the chat.
- other_party: in a one-to-one chat, the other participant's name exactly as listed. In a group, \
leave it empty.
- Evidence: cite 1 for a message and quote its text exactly; cite a photo's source number for a photo.
- A line that only names a file ("IMG-....jpg (file attached)", "<attached: ...>", "<Media \
omitted>") is a photo or file. If it isn't among the sources, you can't see it.
"""


def chat_request(export: ChatExport, photos: Sequence[tuple[Attachment, list[PreparedImage]]], *, focus: str = "") -> StructuredRequest:
    parts: list[TextPart | ImagePart] = [TextPart(EXPORT_GUIDE), TextPart("Source 1, the chat:\n" + transcript(export))]
    for number, (attachment, images) in enumerate(photos, start=2):
        message = export.conversation.message(attachment.message_key)
        person = export.conversation.participant(message.participant if message else None)
        sender = f' by "{person.name}"' if person else ""
        parts.append(TextPart(f"Source {number}: a photo ({attachment.name}) sent with message [{attachment.message_key}]{sender}"))
        parts += [ImagePart(media_type=image.media_type, data=image.data) for image in images]
    if focus:
        parts.append(TextPart(f"The person wants the record for this deal: {clean_text(focus, 300)}. Report it in the fields, and the chat's other deals in other_deals."))
    parts.append(TextPart("Extract the transaction from this chat."))
    return StructuredRequest(task="extraction", system=SYSTEM_PROMPT, parts=parts, schema=EXTRACTION_SCHEMA)


def prepare_photos(attachments: Sequence[Attachment], settings: Settings) -> list[tuple[Attachment, list[PreparedImage]]]:
    """Each photo checked like a screenshot; one that fails is left out rather than failing the chat."""
    prepared = []
    for attachment in attachments:
        try:
            images = prepare_screenshots(
                [UploadedFile(name=attachment.name, data=attachment.data)],
                max_files=1, max_file_bytes=settings.max_file_bytes, max_total_bytes=settings.max_file_bytes,
            )
        except UploadRejected:
            continue
        prepared.append((attachment, images))
    return prepared


def with_known_sides(ai: AIExtraction, export: ChatExport) -> AIExtraction:
    """The other party the export itself shows replaces the model's reading of it."""
    contact = export.contact
    if contact is None:
        if export.owner.key is None:
            # Without knowing who exported the chat, a name can't be placed on either side by role.
            return ai.model_copy(update={"other_party": AIField(found=False, value="", confidence="low", evidence=[], note="")})
        return ai
    other = AIField(
        found=True, value=contact.name, confidence="high",
        evidence=[AIEvidence(screenshot=1, quote=contact.name)], note="The other person in the chat export.",
    )
    return ai.model_copy(update={"other_party": other})


def chat_summary(export: ChatExport, photos_read: int) -> ChatSummary:
    conversation = export.conversation
    counts = Counter(message.participant for message in conversation.messages if message.participant)
    days = [message.day for message in conversation.messages if message.day]
    return ChatSummary(
        participants=[
            ChatParticipant(
                key=person.key, name=person.name, phone=phone_in_label(person.name), messages=counts.get(person.key, 0),
                side="owner" if person.side == Side.OWNER else "contact" if person.side == Side.CONTACT else "unknown",
            )
            for person in conversation.participants
        ],
        group=conversation.is_group,
        messages=len(conversation.messages),
        first_day=days[0] if days else "",
        last_day=days[-1] if days else "",
        owner_source=export.owner.source,
        photos_read=photos_read,
        media_in_archive=export.media_in_archive,
        truncated=any(note.code == "truncated" for note in conversation.notes),
    )


async def run_chat_extraction(provider: object, export: ChatExport, settings: Settings, *, focus: str = "") -> ExtractionResponse:
    """Read a parsed export with the AI and map the answer like a screenshot's."""
    photos = await asyncio.to_thread(prepare_photos, export.attachments, settings)
    images_sent = sum(len(images) for _, images in photos)
    started = time.perf_counter()
    try:
        result = await provider.generate(chat_request(export, photos, focus=focus))  # type: ignore[attr-defined]
        ai = parse_ai_answer(result.data)
    except ExtractionProviderError as exc:
        log_event(
            "extraction.failed", source="chat_export", kind=exc.kind.value, detail=exc.detail,
            provider=getattr(provider, "name", ""), messages=len(export.conversation.messages), images_sent=images_sent,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        raise
    response = build_extraction_response(with_known_sides(ai, export), screenshots=1 + len(photos), images_sent=images_sent)
    notes = [ReviewReason(field=None, message=note.message) for note in export.conversation.notes if note.severity == NoteSeverity.REVIEW]
    summary = chat_summary(export, len(photos))
    response = response.model_copy(
        update={
            "source": "chat_export",
            "chat": summary,
            "review_reasons": [*response.review_reasons, *notes],
            "review_required": response.review_required or bool(notes),
        }
    )
    log_event(
        "extraction.completed",
        source="chat_export",
        provider=getattr(provider, "name", ""),
        model=result.usage.model,
        duration_ms=round((time.perf_counter() - started) * 1000),
        messages=summary.messages,
        participants=len(summary.participants),
        group=summary.group,
        owner_known=bool(summary.owner_source),
        photos=summary.photos_read,
        images_sent=images_sent,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        fallback_used=result.usage.fallback_used,
        contains_transaction=response.contains_transaction,
        other_deals=len(response.other_deals),
        review_required=response.review_required,
    )
    return response
