# Collab Studio

A website where customers submit a collab idea ("Real Madrid × Las Vegas Raiders"), get two AI-designed
front-and-back concepts on a private page, ask for changes, approve a final proof and pay. Paid, approved orders
then move into a production queue. "Collab Studio" is a working name; you can change it in Settings.

Stack: FastAPI + SQLite + server-rendered HTML with a little vanilla JS. It has no build step and no new
dependencies (it uses `fastapi`, `uvicorn`, `python-multipart`, `pillow` and `requests` from the repo's
`requirements.txt`).

## Run it

```bash
pip install -r requirements.txt            # if you haven't already
cp collab_studio/.env.example collab_studio/.env   # optional
python3 -m collab_studio                   # http://127.0.0.1:8470
```

- Admin: http://127.0.0.1:8470/admin. The password comes from `COLLAB_ADMIN_PASSWORD`. If that isn't set, a
  random password is saved to `data/collab_studio/admin_password.txt`.
- Data (database, uploads, generated images) lives in `data/collab_studio/`, which git ignores.
- Tests: `python3 -m unittest tests.test_collab_studio`

## The customer journey

1. **`/create`**: the customer picks a garment and style and enters the idea, colours, back name and number,
   up to 5 reference images, notes, name and email. The form checks required fields and uploads, and shows a
   summary before the customer submits. No account is needed.
2. The request is saved, a private link is emailed, and a design job is queued.
3. A background worker runs the job: interpret the request → build a structured brief → write the prompt →
   generate concepts A and B (front and back) → run automatic checks. The page shows progress; failures are
   retried with backoff.
4. By default the designer reviews drafts in admin and clicks **Send to customer**. This emails a link to
   the design page (`/p/<token>`).
5. The customer zooms in, flips front/back, and clicks **Choose This Design** or **Request Changes**. A
   revision becomes a new version (A2…); earlier versions stay under "Previous versions".
6. **`/approve`** shows the exact version and proof being approved, plus name, number, size, quantity, the
   size chart and the quote. The approval is stored against that version and proof.
7. **Pay** goes to the simulated test checkout or to Stripe Checkout. Payment is confirmed on the server
   before the order is marked paid.
8. **`/order`** shows the status timeline, updates, and tracking once the order ships.

## Rules the code enforces (all covered by tests)

- **Private links:** each link is an HMAC token. Only its hash is stored. A customer only sees their own
  references, the concepts that were sent to them, and their proofs. Drafts and production files stay
  admin-only. Private pages are `no-store` and `noindex`, and logs show `/p/<private>` instead of the token.
- **Versions are never overwritten.** Revisions, regenerations and manual uploads always add a new version,
  and each version keeps its own brief, prompt and images.
- **Approvals can go stale.** An approval records the version, proof, garment, name, number, size and
  quantity. Choosing another design, a new proof, a change to the name or number, or a revision
  invalidates it, and the customer has to approve again.
- **Production gate:** an order can only be submitted when payment is confirmed, the current approval is
  the one that was paid for and matches the current proof, production artwork has passed review, and
  branding permission isn't pending or declined. The submit is claimed atomically, so it can't happen twice.
- **No duplicates:** payment events are unique per (provider, event id). A checkout session can only mark an
  order paid once, even when the Stripe redirect and the webhook both arrive. There is one order per
  request, and emails are de-duplicated per event.
- **Admin** uses a signed session cookie, CSRF tokens on every form and login rate limiting. Cross-site
  POSTs are refused.

## Admin dashboard (`/admin`)

From the dashboard you can:
- List requests, filtered by status or "needs attention" (concepts to review, failed generation, branding
  review, ready for production).
- Open a request to see:
  - the references and the brief (JSON)
  - each job's attempts and errors
  - each version with its prompt and checks
- Send or hold concepts, regenerate with extra design instructions (up to a generation limit), or upload
  replacement concepts.
- Upload a final proof (this requires a fresh approval) and edit the name or number (this invalidates the
  approval).
- Set branding permission (not needed / pending / cleared / declined, with notes) and grant extra revisions.
- Upload production panels per print area and manufacturer templates, validate them, and mark the artwork
  passed or failed.
- Download a production pack (.zip), submit to production, record the actual manufacturing cost (the page
  shows the margin), add tracking (this emails the customer) and post customer updates.
- **Settings:** business name, revision and generation limits, retries, tax rate, turnaround text, the
  demo-price flag, review and proof options, garments (price, shipping, sizes, size chart, print panels and
  their pixel requirements, Printify mapping), shipping countries, shop links and FAQ.
- **Emails:** every message is shown exactly as the customer would receive it.

## Integrations

| Area | Status | How to make it live |
| --- | --- | --- |
| Image generation | **Demo**: stamped placeholders, labelled "not generated from your request" everywhere | `COLLAB_IMAGE_PROVIDER=codex` (Codex CLI with your ChatGPT login, already on this Mac) or `openai` + `OPENAI_API_KEY`. Manual concept upload always works. |
| Payments | **Simulated**: an in-app TEST MODE checkout that confirms on the server | `STRIPE_SECRET_KEY=sk_test_…` + `STRIPE_WEBHOOK_SECRET`, then point a Stripe webhook at `/webhooks/stripe` (`checkout.session.completed`). Live keys are refused unless you opt in. |
| Email | **Preview**: stored and viewable in `/admin/emails`, nothing is sent | `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` (any SMTP service) |
| Fulfillment | **Manual**: download the pack, send it to your manufacturer, record their reference | `COLLAB_FULFILLMENT_PROVIDER=printify` + token + shop id, **and** a verified per-garment `printify` mapping in Settings (`blueprint_id`, `print_provider_id`, `variants` by size, `positions`). Written against Printify's orders API, not yet run against it. |
| Shop links | Empty | Fill the URLs in Settings → Shop links (they appear on the landing page). |

## What still needs your input

- **Prices and shipping are demo values.** The demo badge stays until you untick it.
- **Tax isn't configured.** Set a rate, or use Stripe Tax.
- **Size charts and print-panel pixel sizes are empty on purpose.** Copy them from your manufacturer's verified
  product pages. Until then, artwork checks show "review" and need a human check.
- Turnaround text is a placeholder.
- **Real team and player names.** The app keeps official logos and wordmarks out of every prompt and flags
  these requests for branding review. Selling fan-inspired designs that name real teams or players is still a
  legal risk; get advice before you go live.
