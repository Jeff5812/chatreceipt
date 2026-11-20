-- The WhatsApp assistant's working state per customer (2026-11-19). Additive only: no existing data changes.
--
-- request / request_lease_until: a free-text message waiting for the AI to read it ("who still owes
-- me?", "send Ada's receipt again"), one per customer. A worker asks the AI outside any transaction;
-- later messages from that customer wait until it has been answered, so replies stay in order. The
-- text is removed as soon as it has been answered.
--
-- memory: the last few lines of the conversation (theirs and ChatReceipt's), so a message like "yes"
-- or "the second one" is read in context. Kept short, and cleared after a day without messages.

ALTER TABLE chatreceipt.customers
    ADD COLUMN request jsonb,
    ADD COLUMN request_lease_until timestamptz,
    ADD COLUMN memory jsonb NOT NULL DEFAULT '[]';

CREATE INDEX customers_waiting_requests ON chatreceipt.customers (last_message_at) WHERE request IS NOT NULL;
