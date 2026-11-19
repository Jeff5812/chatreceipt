"""The transaction intelligence engine's application layer.

Applies the pure rules in ``app.intelligence`` and ``app.records`` to stored data:

    ingestion     uploads and conversations
    jobs          the durable job queue: leases, retries, fencing
    failures      which errors are worth retrying
    processing    reading a conversation into reviewable transactions
    transactions  review, approval and the rest of the lifecycle, with full history
    receipts      issuing, rendering, replacing and voiding receipts
    outbox        integration events, delivered reliably after commit
    worker        the background loops
"""
