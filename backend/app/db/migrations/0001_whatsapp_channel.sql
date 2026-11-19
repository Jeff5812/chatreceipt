-- Customers who use ChatReceipt through WhatsApp, their saved records, and the durable queues that
-- make webhook handling safe to retry.
--
-- Everything lives in the "chatreceipt" schema, which Supabase's auto-generated REST API doesn't
-- expose, and row-level security is on with no policies, so only the backend's own database role
-- (the table owner) can read these tables.

REVOKE ALL ON SCHEMA chatreceipt FROM PUBLIC;

CREATE TABLE chatreceipt.customers (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Meta's business-scoped user ID: always present on real webhooks, and the customer's identity.
    whatsapp_user_id text UNIQUE,
    -- Digits only. Meta omits it for people who use a WhatsApp username.
    whatsapp_phone   text UNIQUE,
    next_record_no   integer NOT NULL DEFAULT 1 CHECK (next_record_no >= 1),
    welcomed_at      timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_message_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT customers_have_an_identity CHECK (whatsapp_user_id IS NOT NULL OR whatsapp_phone IS NOT NULL)
);

-- Every message Meta delivers, once. The primary key makes duplicate deliveries harmless.
CREATE TABLE chatreceipt.whatsapp_inbound (
    message_id      text PRIMARY KEY,
    seq             bigint GENERATED ALWAYS AS IDENTITY,
    customer_id     uuid NOT NULL REFERENCES chatreceipt.customers (id) ON DELETE CASCADE,
    kind            text NOT NULL CHECK (kind IN ('text', 'image', 'reply', 'unsupported')),
    -- Cleared as soon as the message has been handled.
    payload         jsonb NOT NULL DEFAULT '{}',
    sent_at         timestamptz NOT NULL,
    status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'handled', 'failed')),
    attempts        integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    handled_at      timestamptz
);
CREATE INDEX whatsapp_inbound_pending ON chatreceipt.whatsapp_inbound (customer_id, sent_at, seq) WHERE status = 'pending';
CREATE INDEX whatsapp_inbound_due ON chatreceipt.whatsapp_inbound (next_attempt_at, seq) WHERE status = 'pending';
CREATE INDEX whatsapp_inbound_recent ON chatreceipt.whatsapp_inbound (customer_id, kind, created_at);

-- One set of screenshots on its way to becoming a record.
CREATE TABLE chatreceipt.whatsapp_submissions (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id      uuid NOT NULL REFERENCES chatreceipt.customers (id) ON DELETE CASCADE,
    status           text NOT NULL CHECK (status IN ('collecting', 'processing', 'awaiting_confirmation', 'confirmed', 'cancelled', 'failed')),
    -- WhatsApp media references only. Image bytes are never stored.
    images           jsonb NOT NULL DEFAULT '[]',
    images_version   integer NOT NULL DEFAULT 1,
    held_images      jsonb NOT NULL DEFAULT '[]',
    draft            jsonb,
    review           jsonb,
    -- Goes up with every version shown to the customer, so an old button can't confirm a newer draft.
    revision         integer NOT NULL DEFAULT 0,
    edited_fields    jsonb NOT NULL DEFAULT '[]',
    pending_edit     jsonb,
    first_image_at   timestamptz NOT NULL DEFAULT now(),
    run_after        timestamptz NOT NULL DEFAULT now(),
    attempts         integer NOT NULL DEFAULT 0,
    lease_token      uuid,
    lease_expires_at timestamptz,
    delay_notified   boolean NOT NULL DEFAULT false,
    failure_code     text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX whatsapp_one_open_submission ON chatreceipt.whatsapp_submissions (customer_id)
    WHERE status IN ('collecting', 'processing', 'awaiting_confirmation');
CREATE INDEX whatsapp_submissions_due ON chatreceipt.whatsapp_submissions (run_after) WHERE status IN ('collecting', 'processing');

-- Records the customer confirmed. Totals are the backend's own calculation.
CREATE TABLE chatreceipt.transactions (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id      uuid NOT NULL REFERENCES chatreceipt.customers (id) ON DELETE CASCADE,
    record_no        integer NOT NULL,
    reference        text NOT NULL,
    source           text NOT NULL DEFAULT 'whatsapp' CHECK (source IN ('whatsapp')),
    -- One record per submission, so a repeated confirmation can't save twice.
    submission_id    uuid UNIQUE REFERENCES chatreceipt.whatsapp_submissions (id) ON DELETE SET NULL,
    draft            jsonb NOT NULL,
    calculation      jsonb NOT NULL,
    currency         text NOT NULL,
    seller           text NOT NULL DEFAULT '',
    buyer            text NOT NULL DEFAULT '',
    transaction_date date,
    total            numeric(20, 6) NOT NULL CHECK (total >= 0),
    amount_paid      numeric(20, 6),
    outstanding      numeric(20, 6),
    payment_status   text NOT NULL CHECK (payment_status IN ('not_specified', 'unpaid', 'partially_paid', 'paid', 'refunded')),
    edited_fields    integer NOT NULL DEFAULT 0,
    image_hashes     text[] NOT NULL DEFAULT '{}',
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (customer_id, record_no)
);
CREATE INDEX transactions_by_customer ON chatreceipt.transactions (customer_id, created_at DESC, id DESC);

-- Replies waiting to be sent. Written in the same transaction as the state change they describe.
-- customer_ref is deliberately not a foreign key: the goodbye after a data deletion must still go out.
CREATE TABLE chatreceipt.whatsapp_outbound (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_ref        uuid NOT NULL,
    to_user_id          text,
    to_phone            text,
    dedupe_key          text NOT NULL UNIQUE,
    -- Cleared once sent: it can contain record details or a sign-in link.
    payload             jsonb NOT NULL,
    status              text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    attempts            integer NOT NULL DEFAULT 0,
    next_attempt_at     timestamptz NOT NULL DEFAULT now(),
    provider_message_id text,
    last_error_code     text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    sent_at             timestamptz
);
CREATE INDEX whatsapp_outbound_pending ON chatreceipt.whatsapp_outbound (customer_ref, id) WHERE status = 'pending';
CREATE INDEX whatsapp_outbound_due ON chatreceipt.whatsapp_outbound (next_attempt_at, id) WHERE status = 'pending';

-- One-time codes sent over WhatsApp to open the records page, and the sessions they start.
-- Only hashes are stored.
CREATE TABLE chatreceipt.web_sign_in_codes (
    code_hash   text PRIMARY KEY,
    customer_id uuid NOT NULL REFERENCES chatreceipt.customers (id) ON DELETE CASCADE,
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE chatreceipt.web_sessions (
    token_hash  text PRIMARY KEY,
    customer_id uuid NOT NULL REFERENCES chatreceipt.customers (id) ON DELETE CASCADE,
    expires_at  timestamptz NOT NULL,
    revoked_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE chatreceipt.customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE chatreceipt.whatsapp_inbound ENABLE ROW LEVEL SECURITY;
ALTER TABLE chatreceipt.whatsapp_submissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE chatreceipt.transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE chatreceipt.whatsapp_outbound ENABLE ROW LEVEL SECURITY;
ALTER TABLE chatreceipt.web_sign_in_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE chatreceipt.web_sessions ENABLE ROW LEVEL SECURITY;
