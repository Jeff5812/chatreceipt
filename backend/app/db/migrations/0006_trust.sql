-- Payment checks, the other person's confirmation of a record, and WhatsApp usernames (2026-11-21).
-- Additive only: no existing row changes, and every new column allows NULL or has a default, so the
-- code from before this migration keeps working with it.

-- WhatsApp usernames. Someone with a username can hide their number: Meta then sends only their
-- business-scoped user ID (the identity, unchanged) and the username, which is kept for display.
ALTER TABLE chatreceipt.customers
    ADD COLUMN whatsapp_username text CHECK (whatsapp_username ~ '^[A-Za-z0-9._]{3,35}$'),
    -- A question the customer is answering outside a draft: what's wrong with a record they were
    -- asked to confirm. {"kind": "dispute", "transaction_id": ..., "until": ...}. NULL when none.
    ADD COLUMN awaiting jsonb;

-- A username in a chat ("@ada_closet") is unique on WhatsApp while its owner keeps it, so it
-- recognises a contact as reliably as a phone number. Stored lower-cased, without the "@".
ALTER TABLE chatreceipt.counterparties
    ADD COLUMN username text CHECK (username ~ '^[a-z0-9._]{3,35}$');
CREATE UNIQUE INDEX counterparties_username ON chatreceipt.counterparties (customer_id, username) WHERE username IS NOT NULL;

-- payment_check: whether money recorded as paid reached the customer. NULL when there is nothing to
-- check (nothing paid, or the customer was the one paying).
--   claimed       the chat or a transfer receipt says it was paid; nobody has checked yet
--   received      the customer said it reached their account
--   not_received  the customer said it hasn't arrived
-- ChatReceipt never verifies a payment itself: "received" is the customer's own answer.
-- payment_reference: the reference or session ID printed on a transfer receipt, normalised to
-- upper-case letters and digits, so the same receipt on two records can be pointed out.
-- confirm_code: the code in the link the other person taps to confirm the record.
ALTER TABLE chatreceipt.transactions
    ADD COLUMN payment_check text CHECK (payment_check IN ('claimed', 'received', 'not_received')),
    ADD COLUMN payment_checked_at timestamptz,
    ADD COLUMN payment_reference text CHECK (payment_reference ~ '^[0-9A-Z]{6,40}$'),
    ADD COLUMN confirm_code text UNIQUE CHECK (confirm_code ~ '^[0-9A-Z]{10}$');
CREATE INDEX transactions_payment_reference ON chatreceipt.transactions (customer_id, payment_reference)
    WHERE payment_reference IS NOT NULL;

-- The other person's answer to a record they were sent: "agreed", or "disputed" with what they
-- say is wrong. The responder is whoever messaged the code, identified the way WhatsApp identified
-- them. If they later delete their own data, their identifiers here are removed and the answer stays.
CREATE TABLE chatreceipt.record_responses (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id     uuid NOT NULL REFERENCES chatreceipt.transactions (id) ON DELETE CASCADE,
    responder_id       uuid REFERENCES chatreceipt.customers (id) ON DELETE SET NULL,
    responder_phone    text CHECK (responder_phone ~ '^[0-9]{5,20}$'),
    responder_username text CHECK (responder_username ~ '^[A-Za-z0-9._]{3,35}$'),
    responder_name     text CHECK (char_length(responder_name) BETWEEN 1 AND 120),
    response           text NOT NULL CHECK (response IN ('agreed', 'disputed')),
    note               text CHECK (char_length(note) BETWEEN 1 AND 500),
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX record_responses_by_record ON chatreceipt.record_responses (transaction_id, created_at DESC);
CREATE INDEX record_responses_by_responder ON chatreceipt.record_responses (responder_id);
ALTER TABLE chatreceipt.record_responses ENABLE ROW LEVEL SECURITY;
