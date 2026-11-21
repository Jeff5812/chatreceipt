-- Who is who on a receipt, and chat exports sent over WhatsApp (2026-11-20).
-- Additive only: no existing row changes, and every new column has a default or allows NULL.

-- The account owner: the customer messaging ChatReceipt. Their stable identity stays Meta's
-- business-scoped user ID (whatsapp_user_id); these columns describe how they appear on receipts.
--
-- party_name: the name they confirmed for their side of a receipt (their business or own name).
-- usual_role: the role they last said they had, offered first when a chat doesn't show it.
-- whatsapp_name: the profile name WhatsApp sends with each message. Unconfirmed: it is only offered
--   as a one-tap answer, never put on a receipt by itself.
-- name_prompts_skipped: how often they chose not to give a name. After two, ChatReceipt stops asking.
-- business_number_id: the ChatReceipt WhatsApp number (Meta's phone number ID) they message.
ALTER TABLE chatreceipt.customers
    ADD COLUMN party_name text CHECK (char_length(party_name) BETWEEN 1 AND 120),
    ADD COLUMN usual_role text CHECK (usual_role IN ('seller', 'buyer')),
    ADD COLUMN whatsapp_name text CHECK (char_length(whatsapp_name) BETWEEN 1 AND 120),
    ADD COLUMN name_prompts_skipped smallint NOT NULL DEFAULT 0 CHECK (name_prompts_skipped >= 0),
    ADD COLUMN business_number_id text CHECK (char_length(business_number_id) <= 64);

-- The people a customer deals with, remembered from the receipts they confirm, so the next receipt
-- with the same person needs no correction. A phone number is a reliable match and fills the name;
-- a label (the name at the top of a chat, an export's sender label) is only the customer's own name
-- for a contact, so a match on it is offered for checking, never treated as proof.
CREATE TABLE chatreceipt.counterparties (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id   uuid NOT NULL REFERENCES chatreceipt.customers (id) ON DELETE CASCADE,
    -- The name the customer confirmed on a receipt.
    name          text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
    -- Digits only, when a chat or export showed this person's number.
    phone         text CHECK (phone ~ '^[0-9]{7,15}$'),
    -- How this person appeared in the customer's chats, lower-cased letters and digits only.
    labels        text[] NOT NULL DEFAULT '{}' CHECK (cardinality(labels) <= 10),
    last_role     text CHECK (last_role IN ('seller', 'buyer')),
    receipts      integer NOT NULL DEFAULT 0 CHECK (receipts >= 0),
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX counterparties_phone ON chatreceipt.counterparties (customer_id, phone) WHERE phone IS NOT NULL;
CREATE INDEX counterparties_customer ON chatreceipt.counterparties (customer_id, last_seen_at DESC);
CREATE INDEX counterparties_labels ON chatreceipt.counterparties USING gin (labels);
ALTER TABLE chatreceipt.counterparties ENABLE ROW LEVEL SECURITY;

-- Which side of a saved record the customer was on, when it was known: "who still owes me?" counts
-- only records where they are the seller, and money they owe as a buyer is listed separately.
-- NULL for records saved before this column existed, and whenever their side wasn't known.
ALTER TABLE chatreceipt.transactions
    ADD COLUMN owner_role text CHECK (owner_role IN ('seller', 'buyer'));

-- A WhatsApp chat export (.txt or .zip) sent as a document.
ALTER TABLE chatreceipt.whatsapp_inbound DROP CONSTRAINT whatsapp_inbound_kind_check;
ALTER TABLE chatreceipt.whatsapp_inbound
    ADD CONSTRAINT whatsapp_inbound_kind_check CHECK (kind IN ('text', 'image', 'reply', 'unsupported', 'document'));
