-- The transaction intelligence engine: uploads, conversations, background jobs, extraction runs,
-- reviewable transactions with a full version history, receipts and integration events.
--
-- Tenant isolation is enforced twice. The application filters every query by customer, and the
-- database itself refuses other customers' rows: request handlers switch to the chatreceipt_tenant
-- role for each transaction, and row-level security limits that role to one customer.

-- Accounts that don't come from WhatsApp (API users, demos).
ALTER TABLE chatreceipt.customers DROP CONSTRAINT customers_have_an_identity;
ALTER TABLE chatreceipt.customers ADD COLUMN external_ref text UNIQUE;
ALTER TABLE chatreceipt.customers ADD CONSTRAINT customers_have_an_identity
    CHECK (whatsapp_user_id IS NOT NULL OR whatsapp_phone IS NOT NULL OR external_ref IS NOT NULL);
ALTER TABLE chatreceipt.customers ADD COLUMN next_receipt_no integer NOT NULL DEFAULT 1 CHECK (next_receipt_no >= 1);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'chatreceipt_tenant') THEN
        CREATE ROLE chatreceipt_tenant NOLOGIN;
    END IF;
END
$$;
GRANT chatreceipt_tenant TO CURRENT_USER;
GRANT USAGE ON SCHEMA chatreceipt TO chatreceipt_tenant;

CREATE FUNCTION chatreceipt.current_customer() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$ SELECT NULLIF(current_setting('chatreceipt.customer_id', true), '')::uuid $$;

CREATE FUNCTION chatreceipt.refuse_change() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN RAISE EXCEPTION 'This history is append-only.'; END $$;

-- Uploaded files. Identical content from one customer is stored once.
CREATE TABLE chatreceipt.uploads (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id       uuid NOT NULL REFERENCES chatreceipt.customers (id) ON DELETE CASCADE,
    kind              text NOT NULL CHECK (kind IN ('image', 'text')),
    original_filename text NOT NULL DEFAULT '' CHECK (char_length(original_filename) <= 255),
    content_type      text NOT NULL CHECK (content_type IN ('image/jpeg', 'image/png', 'image/webp', 'text/plain')),
    byte_size         integer NOT NULL CHECK (byte_size > 0),
    sha256            text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    width             integer CHECK (width > 0),
    height            integer CHECK (height > 0),
    storage_key       text,
    status            text NOT NULL DEFAULT 'stored' CHECK (status IN ('stored', 'deleted')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    expires_at        timestamptz NOT NULL,
    deleted_at        timestamptz,
    UNIQUE (id, customer_id),
    CHECK ((status = 'stored') = (storage_key IS NOT NULL))
);
CREATE UNIQUE INDEX uploads_one_stored_copy ON chatreceipt.uploads (customer_id, sha256) WHERE status = 'stored';
CREATE INDEX uploads_expiring ON chatreceipt.uploads (expires_at) WHERE status = 'stored';

CREATE TABLE chatreceipt.conversations (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id uuid NOT NULL REFERENCES chatreceipt.customers (id) ON DELETE CASCADE,
    source_type text NOT NULL CHECK (source_type IN ('screenshots', 'text')),
    title       text NOT NULL DEFAULT '' CHECK (char_length(title) <= 120),
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, customer_id)
);
CREATE INDEX conversations_by_customer ON chatreceipt.conversations (customer_id, created_at DESC);

CREATE TABLE chatreceipt.conversation_sources (
    conversation_id uuid NOT NULL,
    customer_id     uuid NOT NULL,
    position        smallint NOT NULL CHECK (position BETWEEN 1 AND 20),
    upload_id       uuid NOT NULL,
    PRIMARY KEY (conversation_id, position),
    UNIQUE (conversation_id, upload_id),
    FOREIGN KEY (conversation_id, customer_id) REFERENCES chatreceipt.conversations (id, customer_id) ON DELETE CASCADE,
    FOREIGN KEY (upload_id, customer_id) REFERENCES chatreceipt.uploads (id, customer_id) ON DELETE CASCADE
);
CREATE INDEX conversation_sources_by_upload ON chatreceipt.conversation_sources (upload_id);

-- Background work. The partial unique index stops two active jobs for the same thing.
CREATE TABLE chatreceipt.processing_jobs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id      uuid NOT NULL REFERENCES chatreceipt.customers (id) ON DELETE CASCADE,
    kind             text NOT NULL CHECK (kind IN ('process_conversation', 'render_receipt')),
    subject_id       uuid NOT NULL,
    status           text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'retrying', 'completed', 'failed', 'cancelled')),
    attempts         integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts     integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 20),
    run_after        timestamptz NOT NULL DEFAULT now(),
    lease_token      uuid,
    lease_expires_at timestamptz,
    locked_by        text,
    error_code       text,
    error_message    text,
    result           jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, customer_id)
);
CREATE UNIQUE INDEX processing_jobs_one_active ON chatreceipt.processing_jobs (kind, subject_id)
    WHERE status IN ('queued', 'running', 'retrying');
CREATE INDEX processing_jobs_due ON chatreceipt.processing_jobs (run_after) WHERE status IN ('queued', 'retrying');
CREATE INDEX processing_jobs_leased ON chatreceipt.processing_jobs (lease_expires_at) WHERE status = 'running';

CREATE TABLE chatreceipt.job_attempts (
    job_id      uuid NOT NULL,
    customer_id uuid NOT NULL,
    attempt     integer NOT NULL,
    worker      text NOT NULL,
    outcome     text NOT NULL CHECK (outcome IN ('completed', 'retrying', 'failed', 'lease_lost')),
    error_code  text,
    duration_ms integer NOT NULL DEFAULT 0,
    finished_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, attempt),
    FOREIGN KEY (job_id, customer_id) REFERENCES chatreceipt.processing_jobs (id, customer_id) ON DELETE CASCADE
);

CREATE TABLE chatreceipt.worker_heartbeats (
    worker_id    text PRIMARY KEY,
    started_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now()
);

-- What each screenshot was read as, kept apart from everything interpreted from it.
CREATE TABLE chatreceipt.ocr_results (
    upload_id      uuid NOT NULL,
    customer_id    uuid NOT NULL,
    provider       text NOT NULL,
    prompt_version text NOT NULL,
    model          text NOT NULL DEFAULT '',
    transcript     jsonb NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (upload_id, provider, prompt_version),
    FOREIGN KEY (upload_id, customer_id) REFERENCES chatreceipt.uploads (id, customer_id) ON DELETE CASCADE
);

CREATE TABLE chatreceipt.extraction_runs (
    id                            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id                   uuid NOT NULL REFERENCES chatreceipt.customers (id) ON DELETE CASCADE,
    conversation_id               uuid NOT NULL,
    job_id                        uuid,
    engine_version                text NOT NULL,
    transcription_prompt_version  text NOT NULL,
    interpretation_prompt_version text NOT NULL,
    schema_version                text NOT NULL,
    provider                      text NOT NULL,
    model                         text NOT NULL DEFAULT '',
    stages                        jsonb NOT NULL DEFAULT '[]',
    interpretation                jsonb NOT NULL,
    issues                        jsonb NOT NULL DEFAULT '[]',
    notes                         jsonb NOT NULL DEFAULT '[]',
    input_tokens                  integer NOT NULL DEFAULT 0,
    output_tokens                 integer NOT NULL DEFAULT 0,
    duration_ms                   integer NOT NULL DEFAULT 0,
    created_at                    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, customer_id),
    FOREIGN KEY (conversation_id, customer_id) REFERENCES chatreceipt.conversations (id, customer_id) ON DELETE CASCADE
);
CREATE INDEX extraction_runs_by_conversation ON chatreceipt.extraction_runs (conversation_id, created_at DESC);

CREATE TABLE chatreceipt.conversation_participants (
    run_id          uuid NOT NULL,
    customer_id     uuid NOT NULL,
    participant_key text NOT NULL,
    name            text NOT NULL DEFAULT '',
    side            text NOT NULL CHECK (side IN ('owner', 'contact', 'unknown')),
    role            text NOT NULL CHECK (role IN ('seller', 'buyer', 'other', 'unknown')),
    role_confidence text CHECK (role_confidence IN ('high', 'medium', 'low')),
    role_evidence   text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, participant_key),
    FOREIGN KEY (run_id, customer_id) REFERENCES chatreceipt.extraction_runs (id, customer_id) ON DELETE CASCADE
);

CREATE TABLE chatreceipt.conversation_messages (
    run_id          uuid NOT NULL,
    customer_id     uuid NOT NULL,
    message_key     text NOT NULL,
    seq             integer NOT NULL CHECK (seq >= 1),
    participant_key text,
    kind            text NOT NULL CHECK (kind IN ('text', 'media', 'system', 'deleted')),
    text            text NOT NULL,
    original_text   text NOT NULL,
    time_label      text NOT NULL DEFAULT '',
    day_label       text NOT NULL DEFAULT '',
    sent_at         text NOT NULL DEFAULT '',
    quoted          text NOT NULL DEFAULT '',
    legibility      text NOT NULL DEFAULT 'clear' CHECK (legibility IN ('clear', 'partial', 'unreadable')),
    source_kind     text NOT NULL CHECK (source_kind IN ('screenshot', 'text')),
    source_position integer NOT NULL DEFAULT 0,
    source_index    integer NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, message_key),
    UNIQUE (run_id, seq),
    FOREIGN KEY (run_id, customer_id) REFERENCES chatreceipt.extraction_runs (id, customer_id) ON DELETE CASCADE
);

-- A transaction on its way through review, with the working draft and the state of every field.
CREATE TABLE chatreceipt.extracted_transactions (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id       uuid NOT NULL REFERENCES chatreceipt.customers (id) ON DELETE CASCADE,
    conversation_id   uuid,
    extraction_run_id uuid,
    deal_key          text,
    origin            text NOT NULL CHECK (origin IN ('extraction', 'manual')),
    state             text NOT NULL CHECK (state IN ('draft', 'processing', 'awaiting_review', 'approved', 'completed', 'rejected', 'cancelled', 'failed')),
    version           integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    agreement_status  text CHECK (agreement_status IN ('enquiry', 'quotation', 'negotiation', 'agreement_reached', 'agreement_unclear', 'cancelled', 'completed')),
    summary           text NOT NULL DEFAULT '',
    draft             jsonb NOT NULL,
    field_states      jsonb NOT NULL DEFAULT '{}',
    extracted_values  jsonb NOT NULL DEFAULT '{}',
    analysis          jsonb NOT NULL DEFAULT '{}',
    currency          text NOT NULL DEFAULT '',
    seller            text NOT NULL DEFAULT '',
    buyer             text NOT NULL DEFAULT '',
    transaction_date  date,
    total             numeric(20, 6) CHECK (total >= 0),
    amount_paid       numeric(20, 6) CHECK (amount_paid >= 0),
    outstanding       numeric(20, 6),
    payment_status    text NOT NULL DEFAULT 'not_specified' CHECK (payment_status IN ('not_specified', 'unpaid', 'partially_paid', 'paid', 'refunded')),
    review_required   boolean NOT NULL DEFAULT true,
    approved_version  integer,
    state_reason      text NOT NULL DEFAULT '',
    approved_at       timestamptz,
    completed_at      timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, customer_id),
    FOREIGN KEY (conversation_id, customer_id) REFERENCES chatreceipt.conversations (id, customer_id) ON DELETE SET NULL (conversation_id),
    FOREIGN KEY (extraction_run_id, customer_id) REFERENCES chatreceipt.extraction_runs (id, customer_id) ON DELETE SET NULL (extraction_run_id)
);
CREATE UNIQUE INDEX extracted_transactions_one_per_deal ON chatreceipt.extracted_transactions (extraction_run_id, deal_key)
    WHERE extraction_run_id IS NOT NULL;
CREATE INDEX extracted_transactions_recent ON chatreceipt.extracted_transactions (customer_id, created_at DESC, id DESC);
CREATE INDEX extracted_transactions_by_state ON chatreceipt.extracted_transactions (customer_id, state, created_at DESC);
CREATE INDEX extracted_transactions_by_amount ON chatreceipt.extracted_transactions (customer_id, currency, total);
CREATE INDEX extracted_transactions_by_conversation ON chatreceipt.extracted_transactions (conversation_id);

-- The current items, as typed columns for searching and duplicate checks. The draft is the source of truth.
CREATE TABLE chatreceipt.transaction_items (
    transaction_id uuid NOT NULL,
    customer_id    uuid NOT NULL,
    row_id         text NOT NULL,
    position       smallint NOT NULL,
    description    text NOT NULL DEFAULT '',
    quantity       numeric(18, 3) CHECK (quantity > 0),
    unit_price     numeric(20, 6) CHECK (unit_price >= 0),
    line_total     numeric(20, 6) CHECK (line_total >= 0),
    PRIMARY KEY (transaction_id, row_id),
    FOREIGN KEY (transaction_id, customer_id) REFERENCES chatreceipt.extracted_transactions (id, customer_id) ON DELETE CASCADE
);
CREATE INDEX transaction_items_search ON chatreceipt.transaction_items (customer_id, lower(description));

-- Every version of a transaction, exactly as it stood. Receipts point at one of these.
CREATE TABLE chatreceipt.transaction_versions (
    transaction_id  uuid NOT NULL,
    customer_id     uuid NOT NULL,
    version         integer NOT NULL,
    state           text NOT NULL,
    snapshot        jsonb NOT NULL,
    snapshot_sha256 text NOT NULL,
    reason          text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (transaction_id, version),
    FOREIGN KEY (transaction_id, customer_id) REFERENCES chatreceipt.extracted_transactions (id, customer_id) ON DELETE CASCADE
);

-- Each correction or confirmation a person made, next to what the extraction originally proposed.
CREATE TABLE chatreceipt.review_decisions (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id    uuid NOT NULL,
    customer_id       uuid NOT NULL,
    from_version      integer NOT NULL,
    to_version        integer NOT NULL,
    operation         text NOT NULL CHECK (operation IN ('set', 'add_item', 'remove_item', 'add_adjustment', 'remove_adjustment', 'confirm')),
    field_path        text NOT NULL,
    extracted_value   jsonb,
    previous_value    jsonb,
    new_value         jsonb,
    reason            text NOT NULL DEFAULT '',
    actor_id          uuid NOT NULL,
    extraction_run_id uuid,
    created_at        timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (transaction_id, customer_id) REFERENCES chatreceipt.extracted_transactions (id, customer_id) ON DELETE CASCADE
);
CREATE INDEX review_decisions_by_transaction ON chatreceipt.review_decisions (transaction_id, id);
CREATE INDEX review_decisions_by_field ON chatreceipt.review_decisions (customer_id, field_path);

-- What happened to a transaction, in order.
CREATE TABLE chatreceipt.transaction_events (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id uuid NOT NULL,
    customer_id    uuid NOT NULL,
    event_type     text NOT NULL,
    from_state     text,
    to_state       text,
    version        integer NOT NULL,
    actor          text NOT NULL CHECK (actor IN ('customer', 'system')),
    data           jsonb NOT NULL DEFAULT '{}',
    created_at     timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (transaction_id, customer_id) REFERENCES chatreceipt.extracted_transactions (id, customer_id) ON DELETE CASCADE
);
CREATE INDEX transaction_events_by_transaction ON chatreceipt.transaction_events (transaction_id, id);

CREATE TRIGGER transaction_versions_append_only BEFORE UPDATE ON chatreceipt.transaction_versions
    FOR EACH ROW EXECUTE FUNCTION chatreceipt.refuse_change();
CREATE TRIGGER review_decisions_append_only BEFORE UPDATE ON chatreceipt.review_decisions
    FOR EACH ROW EXECUTE FUNCTION chatreceipt.refuse_change();
CREATE TRIGGER transaction_events_append_only BEFORE UPDATE ON chatreceipt.transaction_events
    FOR EACH ROW EXECUTE FUNCTION chatreceipt.refuse_change();

-- Issued receipts never change. A correction issues a new receipt that replaces the old one.
CREATE TABLE chatreceipt.receipts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id         uuid NOT NULL,
    transaction_id      uuid NOT NULL,
    transaction_version integer NOT NULL,
    number              integer NOT NULL CHECK (number >= 1),
    reference           text NOT NULL,
    status              text NOT NULL DEFAULT 'issued' CHECK (status IN ('issued', 'superseded', 'voided')),
    document_status     text NOT NULL DEFAULT 'pending' CHECK (document_status IN ('pending', 'ready', 'failed')),
    document_key        text,
    document_sha256     text,
    document_bytes      integer,
    replaces_receipt_id uuid REFERENCES chatreceipt.receipts (id) ON DELETE SET NULL,
    void_reason         text NOT NULL DEFAULT '',
    issued_at           timestamptz NOT NULL DEFAULT now(),
    superseded_at       timestamptz,
    voided_at           timestamptz,
    UNIQUE (customer_id, number),
    UNIQUE (id, customer_id),
    FOREIGN KEY (transaction_id, customer_id) REFERENCES chatreceipt.extracted_transactions (id, customer_id) ON DELETE CASCADE,
    FOREIGN KEY (transaction_id, transaction_version) REFERENCES chatreceipt.transaction_versions (transaction_id, version) ON DELETE CASCADE
);
CREATE UNIQUE INDEX receipts_one_per_version ON chatreceipt.receipts (transaction_id, transaction_version) WHERE status <> 'voided';
CREATE UNIQUE INDEX receipts_one_current ON chatreceipt.receipts (transaction_id) WHERE status = 'issued';
CREATE INDEX receipts_recent ON chatreceipt.receipts (customer_id, issued_at DESC, id DESC);

-- Events for other systems (n8n and similar), written in the same transaction as the change they report.
CREATE TABLE chatreceipt.outbox_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id     uuid NOT NULL REFERENCES chatreceipt.customers (id) ON DELETE CASCADE,
    event_type      text NOT NULL,
    aggregate_id    uuid NOT NULL,
    payload         jsonb NOT NULL,
    status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'failed', 'skipped')),
    attempts        integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    delivered_at    timestamptz
);
CREATE INDEX outbox_events_due ON chatreceipt.outbox_events (next_attempt_at) WHERE status = 'pending';

-- Answers to requests that carried an Idempotency-Key, so a retry gets the same answer.
CREATE TABLE chatreceipt.idempotency_keys (
    customer_id     uuid NOT NULL REFERENCES chatreceipt.customers (id) ON DELETE CASCADE,
    key             text NOT NULL CHECK (char_length(key) BETWEEN 8 AND 128),
    operation       text NOT NULL,
    request_sha256  text NOT NULL,
    response_status integer,
    response_body   jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (customer_id, key)
);

-- Files of erased accounts, removed from storage by the worker after the database rows are gone.
CREATE TABLE chatreceipt.storage_erasures (
    customer_ref uuid PRIMARY KEY,
    requested_at timestamptz NOT NULL DEFAULT now()
);

-- Records approved through the engine also appear on the customer's records page.
ALTER TABLE chatreceipt.transactions DROP CONSTRAINT transactions_source_check;
ALTER TABLE chatreceipt.transactions ADD CONSTRAINT transactions_source_check CHECK (source IN ('whatsapp', 'engine'));
ALTER TABLE chatreceipt.transactions ADD COLUMN extracted_transaction_id uuid UNIQUE
    REFERENCES chatreceipt.extracted_transactions (id) ON DELETE CASCADE;
ALTER TABLE chatreceipt.transactions ADD COLUMN voided_at timestamptz;

-- Row-level security for every customer-owned table.
DO $$
DECLARE
    tenant_table text;
BEGIN
    FOREACH tenant_table IN ARRAY ARRAY[
        'uploads', 'conversations', 'conversation_sources', 'processing_jobs', 'job_attempts', 'ocr_results',
        'extraction_runs', 'conversation_participants', 'conversation_messages', 'extracted_transactions',
        'transaction_items', 'transaction_versions', 'review_decisions', 'transaction_events', 'receipts',
        'outbox_events', 'idempotency_keys', 'transactions'
    ]
    LOOP
        EXECUTE format('ALTER TABLE chatreceipt.%I ENABLE ROW LEVEL SECURITY', tenant_table);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON chatreceipt.%I TO chatreceipt_tenant
             USING (customer_id = chatreceipt.current_customer())
             WITH CHECK (customer_id = chatreceipt.current_customer())',
            tenant_table
        );
    END LOOP;
END
$$;
ALTER TABLE chatreceipt.worker_heartbeats ENABLE ROW LEVEL SECURITY;
ALTER TABLE chatreceipt.storage_erasures ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON chatreceipt.customers TO chatreceipt_tenant
    USING (id = chatreceipt.current_customer())
    WITH CHECK (id = chatreceipt.current_customer());

GRANT SELECT ON chatreceipt.customers TO chatreceipt_tenant;
GRANT UPDATE (next_record_no, next_receipt_no) ON chatreceipt.customers TO chatreceipt_tenant;
GRANT SELECT, INSERT, UPDATE ON chatreceipt.transactions TO chatreceipt_tenant;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    chatreceipt.uploads, chatreceipt.conversations, chatreceipt.conversation_sources, chatreceipt.ocr_results,
    chatreceipt.extraction_runs, chatreceipt.conversation_participants, chatreceipt.conversation_messages,
    chatreceipt.transaction_items, chatreceipt.idempotency_keys
    TO chatreceipt_tenant;
GRANT SELECT, INSERT, UPDATE ON chatreceipt.processing_jobs, chatreceipt.extracted_transactions, chatreceipt.receipts TO chatreceipt_tenant;
-- History can be read and added to, never rewritten.
GRANT SELECT, INSERT ON
    chatreceipt.job_attempts, chatreceipt.transaction_versions, chatreceipt.review_decisions,
    chatreceipt.transaction_events, chatreceipt.outbox_events
    TO chatreceipt_tenant;
