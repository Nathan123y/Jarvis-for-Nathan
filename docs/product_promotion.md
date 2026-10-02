# Soccer Coach Organizer promotion

This optional `product_sales` plugin adds an on-demand promotion workflow for
https://attontios.gumroad.com/l/kvaya. It uses existing Jarvis web search and
Gmail sign-in. No audio, voice, launcher, or UI changes are needed. There is no
background worker, paid OpenAI API, automatic posting, or automatic email send.

## Activate

After this change is merged, quit Jarvis, pull `main` in the project terminal,
and reopen `~/Applications/Jarvis.app`. No app reinstall is needed. Confirm
`product_sales` is enabled in the Plugin Manager.

1. Ask **“Jarvis, prepare my product promotion.”** It saves six Facebook post
   drafts, each with a purchase link. Say Instagram if that is your chosen
   account. Preview or copy one draft by its returned ID. Posting remains a
   handoff to your selected social account. Use communities that allow promotion.
2. Ask **“Find youth soccer clubs in San Jose to research for my organizer.”**
   Search returns candidates, not verified recipients or permission to contact.
   Verify the actual contact and address on an official page, or identify a
   contact you already know. Avoid guessing addresses or contacting private
   parents/children through a club roster.
3. Ask **“Save this contact for product promotion: [club], [exact email],
   [official contact page or known relationship], [why it is relevant].”**
4. Ask **“Draft a roster-focused product pitch for [saved email].”** Jarvis can
   tailor the text using verified product facts and your stated relationship.
   The tool adds a tagged product link. See the current price on Gumroad;
   the proposed $12 launch price is not hard-coded into pitches.
5. To send, fill in **Settings → Plugin Settings → Product promotion** once:
   Gmail account (`personal`, `school`, or `spam`), sender/business name, and a valid
   business postal address. Then say **“Send product pitch [draft ID].”**
   Review the complete email and press **CONFIRM** on the Jarvis window.
   The send is blocked without a full preview, connection, or confirmation.

For a separate promotion address, say **“Connect my spam Gmail.”** Choose your
actual dedicated Gmail address in Google's account chooser and approve access.
`spam` is Jarvis's local account label; it does not create a Google account or
change the email address recipients see. Confirm Jarvis reports the intended
address, then set the promotion Gmail account field to `spam` and save. Say
**“Send that product pitch from my spam Gmail.”** Its OAuth token is stored
separately in ignored `config/gmail_spam_token.json`. The sender name and postal
address fields are still required. Check that inbox for replies and opt-outs.

Use Gumroad's seller test purchase to check the uploaded ZIP and download before
launching promotion. Keep the existing standard product page; the custom
landing page is not required. Gumroad handles checkout and file delivery.

## Contact history and replies

Say **“Show my product promotion status.”** Sent contacts cannot receive another
first pitch through this plugin. After checking the sending Gmail inbox, record
an outcome: **“Mark [email] replied / bought / do not contact.”** An opt-out stays
blocked even if the same address is rediscovered with different capitalization.
Customer replies and support use the existing Gmail tools and their approval.

Promotional emails include an advertisement label, the configured postal
address, and instructions to reply STOP. Keep receiving replies and record
opt-outs promptly. U.S. commercial email rules also cover individual and B2B
pitches: [FTC guidance](https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business).
This plugin does not automatically read or classify opt-out replies. Until that
is added, check the sending inbox and update the contact history before outreach.

If sending fails after approval, the contact becomes **uncertain** so a network
timeout cannot cause a duplicate. Check Gmail Sent first. Then explicitly mark
the uncertain contact **sent** or **not sent**, according to what you verified.
Cancellation or an expired confirmation sends nothing and leaves the draft.

## Sales checks

Say **“Check my product sales for the last 30 days.”** The plugin uses the Gumroad
CLI already installed on the Mac, in read-only, non-interactive mode. If login
has not succeeded, run `~/.local/bin/gumroad auth login --web` on the Mac. No
Gumroad credentials are copied into Jarvis or shared in chat.

The report resolves `kvaya` to the product's canonical ID, checks up to three
sales pages, deduplicates order IDs, labels incomplete pagination, and excludes
test purchases when Gumroad explicitly flags them. It shows reported orders,
refund flags and individual formatted purchase amounts. These are not profit
or payouts. Review fees, taxes, partial refunds and disputes in Gumroad's own
dashboard/export. Buyer email addresses stay out of the report and are not saved.
Tagged links do not measure visits by themselves; this version cannot report
conversion rates or attribute orders to a particular pitch.

## Local data and validation

Contact addresses, opt-outs and drafts live in ignored
`config/product_sales/state.json`, written atomically with private permissions.
They are not added to Git. Keep a backup of this file; losing the opt-out list
requires stopping outreach until it is recovered. Corrupt history blocks writes
and sends instead of silently resetting the records.

Run `python3 -m unittest discover -s tests -p 'test_product_sales.py'` and the
existing `test_gmail.py` suite. Tests replace Gmail and Gumroad calls; a real
approved Mac email and real seller sales login still need an owner-side check.

## Choosing another product

`product_sales` now includes the original Soccer Coach Organizer and 15 more
Excel/PDF organizers. Ask "Show my products" (`action=products`) and select an
exact name or ID with the `product` parameter. `brief` and `research` work before
publication. `draft`, `campaign` and `sales` require a verified published link.

The seller launch pack contains 15 separate customer ZIPs and the uploader.
Update the Gumroad CLI before creating drafts:
`curl -fsSL https://gumroad.com/install-cli.sh | bash`. The uploader checks that
`products create --help` offers `--draft` and uses that flag on every create.
Older versions are stopped before any write: the create API can publish by
default despite older CLI help calling it a draft. This needs the 2026-10-02
CLI release or later with explicit draft support.
From the extracted pack on the Mac, run `python3 upload_products.py --create` to
create drafts with downloads, descriptions, covers, previews and thumbnails.
Open each draft and use Gumroad's test-purchase feature to verify checkout and
the actual downloaded files. Then use `python3 upload_products.py --publish
--tested`. The CLI login stays local. The uploader checkpoints uncertain writes
and stops rather than risking duplicate uploads. It never emails prospects.
Login is checked using the CLI's `authenticated` field, separately from API
`success` responses. Terminal progress is printed for each upload, and failures
show the command and CLI error message with credentials redacted. Keep the
pack's `upload_state.json` if anything fails; it records confirmed product IDs
and unresolved writes.

The uploader connects published URLs to ignored local
`config/product_sales/catalog_links.json`. For products published manually, ask
"Sync my Gumroad product catalog" (`action=sync`). Sync matches unique catalog
names while ignoring capitalization and repeated spaces, and only accepts
explicit published status and Gumroad URLs. Changed wording is never guessed.
Unpublished listings or ambiguous names are not connected.
When a list entry omits its URL or publication status, sync reads that listing
by its actual product ID and verifies the identity before binding its URL.
A failed or mismatched detail lookup preserves the current links.
The on-screen sync report includes how many listings Gumroad returned, the
names it observed, and separate reasons for unpublished, unmatched, duplicate,
or unverified products. Zero matches is not an accessibility-permission or API
key diagnosis. If names differ, compare those returned names with the catalog;
use the same wording or explicitly resolve which buyer bundle a renamed listing
contains before connecting it. Sync never publishes products or sends email.

Examples:

- "Find independent tutors near San Jose for Tutor Session Organizer."
- "Draft a Tutor Session Organizer pitch for this verified contact."
- "Check Tutor Session Organizer sales for the last 30 days."

Drafts retain their product name and link even if another product is selected
later. Contact history and opt-outs are shared across the catalog. Switching
products does not make a previously pitched contact eligible for another first
pitch. Gmail sends still require the complete on-screen review and confirmation.
The new products have 100 prepared rows per working sheet and require desktop
Excel; their bundled facts describe their actual files and limits.
