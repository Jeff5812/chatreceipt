"""WhatsApp as a second way into ChatReceipt.

A customer sends a screenshot to the business number; the webhook stores the event and answers at
once; a background worker reads the image with the same upload checks and extraction service the
website uses, and asks the customer to confirm, correct or cancel. Nothing is saved until they confirm.

    events        webhook payloads to validated values
    client        the WhatsApp Cloud API (send, media download)
    messages      outgoing message builders and their limits
    summary       a draft as a readable message
    corrections   guided edits of a draft, checked by the backend's calculation rules
    store         every SQL statement for the channel
    conversation  what each incoming message does
    processing    reading a submission's screenshots
    worker        the background loops
"""
