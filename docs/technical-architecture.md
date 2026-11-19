# ChatReceipt Technical Architecture

## Preferred stack

### Frontend

- Next.js.
- React.
- TypeScript.
- Tailwind CSS.
- A consistent component system such as shadcn/ui, if compatible with the existing project.

### Backend

- Python.
- FastAPI.

### Database

- Supabase PostgreSQL.

### Authentication

- Supabase Auth, if account functionality is included.

### File storage

- Supabase Storage, or another appropriate private object storage service.

### AI

- A multimodal model capable of understanding screenshots and extracting structured information.

### PDF generation

- A reliable HTML-to-PDF or PDF generation solution.

### Image processing

- Pillow or an appropriate image processing library.

### Deployment

- Use a deployment configuration appropriate for the selected frontend and backend hosting.

## Architectural flow

```text
User uploads screenshot
↓
Frontend validates file type and size
↓
Backend receives upload
↓
Image validation and preprocessing
↓
AI extracts conversation text and transaction information
↓
Structured output validation
↓
Deterministic transaction calculations
↓
Editable transaction draft
↓
User reviews and corrects
↓
Transaction record is generated
↓
PDF or PNG export
```

## Important architecture rules

The AI must not calculate authoritative totals.

The backend must calculate:

- Line item subtotals.
- Discounts.
- Additional fees.
- Delivery fees.
- Final total.
- Amount paid.
- Outstanding amount.

The AI may extract suggested values.

The backend validates those values.

The user reviews the result.

## AI provider abstraction

Do not tightly couple the entire application to one AI provider.

Create a clean interface for the extraction service.

The provider should be replaceable without rewriting the transaction workflow.

Do not build a complicated multi-provider routing system in the MVP.

## Data validation

Use structured schemas.

Reject or flag:

- Invalid quantities.
- Invalid currency codes.
- Negative values where prohibited.
- Malformed dates.
- Unsupported payment statuses.
- Invalid line items.
- Inconsistent totals.

Do not silently correct uncertain information.

## File handling

Validate:

- File type.
- File size.
- Image dimensions.
- Number of uploaded images.
- Image integrity.

Treat all uploads as untrusted.

Do not trust file extensions alone.

Avoid storing raw screenshots indefinitely without a clear user-facing reason.

## Security

- Protect API keys.
- Keep secrets server-side.
- Use private storage.
- Use signed URLs where appropriate.
- Enforce ownership of saved transactions.
- Apply rate limits.
- Validate all external input.
- Do not log raw screenshots or sensitive extracted information unnecessarily.
- Delete temporary files after processing where appropriate.
- Protect against malicious files and prompt injection within screenshots.

## Database

Do not introduce a large schema prematurely.

The MVP may need:

- `users`
- `transactions`
- `transaction_items`
- `uploaded_images`

The exact schema must be chosen after assessing whether authentication and saved history are included in the first version.

Use migrations.

Do not modify production database structures without an explicit migration plan.

## Testing

Include tests for:

- Extraction schema validation.
- Transaction arithmetic.
- Discounts.
- Delivery fees.
- Multiple currencies.
- Missing information.
- Payment status ambiguity.
- Duplicate line items.
- Negative values.
- Invalid dates.
- Screenshot ordering.
- Unauthorized access.
- PDF generation.

## Observability

Track:

- Extraction success rate.
- Validation failure rate.
- Processing duration.
- AI cost per extraction.
- User corrections.
- Export completion.

Do not track or retain sensitive conversation content unnecessarily.

## Development rules

Preserve existing working code.

Avoid unnecessary dependencies.

Avoid giant files.

Avoid duplicate business logic.

Use clear module boundaries.

Add tests for meaningful behavior.

Do not claim that a feature works unless it has been verified.
