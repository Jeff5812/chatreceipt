-- Typed corrections that need the AI to understand them, and the customer's recent messages about
-- a draft, kept with it as context. Both are cleared when the draft is saved or cancelled, and
-- removed with it when drafts expire. Additive only: existing rows keep working unchanged.

ALTER TABLE chatreceipt.whatsapp_submissions
    ADD COLUMN instruction jsonb,
    ADD COLUMN dialogue jsonb NOT NULL DEFAULT '[]';

CREATE INDEX whatsapp_submissions_instructions ON chatreceipt.whatsapp_submissions (updated_at)
    WHERE instruction IS NOT NULL;
