"""The transaction intelligence engine.

Turns a conversation (screenshots or text) into reviewed transaction proposals:

1. ``transcription`` reads screenshots into per-screenshot message lists (OCR, via the AI provider).
2. ``screenshots`` and ``text_import`` build one normalised ``Conversation`` with stable message keys,
   participants and notes about ordering, overlaps and gaps.
3. ``interpretation`` asks the AI provider what happened, message by message (offers, counter-offers,
   acceptances, payment claims...), as structured events that cite message keys.
4. ``negotiation`` and ``payments`` apply ChatReceipt's own rules to those events to work out what was
   agreed and paid. The AI never decides the agreement or the money; these modules do.
5. ``proposal`` builds an editable draft with field-level states; ``validation`` and ``review_policy``
   decide what a person must check before the record can be approved.

Everything here is pure: no database, no files, no network except through the provider interface.
"""
