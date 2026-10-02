# Stripe plan for the Soccer Coach Website service

## Business and current state

Sell a custom one-page website to a private soccer coach for **$249 USD once**.
Fieldwork is a fictional portfolio example, not a business selling training:
https://fieldwork-soccer-nathan.danipaul-fan-page.chatgpt.site/

The example is publicly shared. Jarvis can research contacts, draft service
pitches, and request approval for one Gmail send. It can save and show a live
Stripe payment URL. It cannot yet query Stripe payments, create invoices, or
automatically deliver paid orders.

Stripe installation was confirmed on October 2, 2026, but this session exposes
neither `stripe_implementation_planner` nor Stripe account tools. The requested
`npx skills add https://docs.stripe.com` fallback succeeded. This plan follows
the official `stripe-best-practices` payment, security, and Connect references;
it is not a result from the unavailable planner. No live Stripe object, API key,
webhook endpoint, or payment has been configured by this change.

## Product setup to perform through the Stripe app

| Setting | Value |
| --- | --- |
| Product | Soccer Coach Website |
| Amount | 24900 minor units / $249 USD |
| Billing | One-time, quantity 1 |
| Description | Custom one-page website with client branding and approved copy, mobile layout, services information, contact or existing booking links, and one revision. Domain and hosting costs are separate. Scope and timing agreed before payment. |
| Fulfillment | Custom work after payment verification; no instant download |
| Checkout | Stripe-hosted Payment Link |

Inspect existing products/prices first to avoid creating duplicates. Create an
isolated sandbox example and test the customer path. Then create or reuse the
equivalent live product, one-time price, and link in the owner's verified
business account. Check currency, amount, account, description, and receipt
settings before sharing. Keep the public coaching demo's form as a demo; do not
attach a website-service payment button that could be mistaken for session
booking.

Copy the live `https://buy.stripe.com/...` URL into Jarvis with:
**"Connect Soccer Coach Website to this payment link: [URL]."**
Jarvis validates URL structure only; it does not verify the amount or account.
Its `payment` action displays the saved link. Initial pitches show the example
and invite a reply. Agree the specific deliverable and timing with each client
before requesting payment.

## Payments and Invoicing

Payment Links fit the initial fixed-price offer. Use Stripe-hosted checkout so
the portfolio and Jarvis do not collect card details. Use client-specific
Invoicing for individually agreed work: prepare a draft with the exact scope,
price, and client identity, review it, then send only on explicit instruction.
An invoice draft or payment URL is not a paid order. Do not automatically charge
a saved payment method or send recurring invoices for this one-time service.

Before automated payment tracking or fulfillment launches, implement signed
webhook handling and durable, idempotent order storage. For Checkout, handle
`checkout.session.completed`, `checkout.session.async_payment_succeeded`, and
`checkout.session.async_payment_failed`; verify paid status and the server-side
product/price, currency, amount, and order reference before recording payment.
For invoices, use verified `invoice.paid` and `invoice.payment_failed` events.
Deduplicate event/order processing, tolerate retries and delayed events, and
update refunds/disputes without silently presenting gross payments as profit.
A browser success page must never authorize fulfillment. Until event handling
exists, the owner checks Stripe directly before recording an outreach outcome;
automated revenue or paid-order reporting remains unavailable.

## Connect

The current offer collects the owner's website-design fee. There are no
connected coaches, split payments, or transfers to implement in this phase.
If a future product lets coaches accept player payments through our platform,
first decide who owns the customer relationship and checkout. A SaaS model in
which independent coaches own their customers points toward direct charges;
a marketplace with platform-run checkout points toward destination charges.

That future implementation should use Accounts v2, Stripe-hosted or embedded
onboarding, capability readiness checks, account management and notification
banner components, and signed account/payment webhooks. Agree fees, disputes,
refunds, payout responsibilities, and liability before moving live funds.
Do not create connected accounts or transfer money merely to sell this website
service.

## API-key integration boundary

Jarvis's current hosted-link handoff requires no Stripe secret key. For a future
backend, use a least-privilege restricted API key stored in the hosting secrets
store; use separate sandbox and live keys and a separate webhook signing secret.
Never put these values in the public HTML, Git, analytics, logs, or chat. Use the
current SDK's client instance, server-owned price/order data, and idempotency
keys for retried writes. Retain an approval step for invoice sends, refunds,
charges, and transfers; an integration does not authorize those actions.

## Official references

- [Payment Link creation](https://docs.stripe.com/payment-links/create)
- [Invoicing and Payment Links comparison](https://docs.stripe.com/invoicing)
- [Connect](https://docs.stripe.com/connect)
- [Checkout fulfillment](https://docs.stripe.com/checkout/fulfillment)
- [Webhook signature verification](https://docs.stripe.com/webhooks)
- [Restricted API keys](https://docs.stripe.com/keys)
