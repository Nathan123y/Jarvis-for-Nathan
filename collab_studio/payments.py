"""Checkout providers. The amount always comes from the server-side order, never from the browser.

simulated  An in-app TEST checkout page. The "payment" is confirmed server-side against the stored checkout session.
stripe     Stripe Checkout (hosted page) through the REST API with a secret key. Paid status is confirmed by retrieving
           the session server-side and/or a signed webhook. Card details never touch this app.
"""
import hashlib
import hmac
import secrets
import time

STRIPE_API = "https://api.stripe.com/v1"


class PaymentError(RuntimeError):
    pass


class SimulatedPayments:
    name = "simulated"
    test_mode = True

    def create_checkout(self, order, approval, title, success_url, cancel_url, test_page_url, countries=("US",)):
        sid = "sim_" + secrets.token_urlsafe(18)
        return {"id": sid, "url": test_page_url.format(session=sid)}

    def retrieve(self, session_id):
        return None                     # the simulated session lives only in our checkouts table


class StripePayments:
    name = "stripe"

    def __init__(self, secret_key, webhook_secret="", http=None):
        if http is None:
            import requests
            http = requests
        self.key, self.webhook_secret, self.http = secret_key, webhook_secret, http
        self.test_mode = secret_key.startswith("sk_test")

    def _call(self, method, path, data=None, idem=None):
        headers = {"Idempotency-Key": idem} if idem else {}
        r = self.http.request(method, STRIPE_API + path, data=data, auth=(self.key, ""), headers=headers, timeout=30)
        if r.status_code >= 300:
            try:
                msg = r.json().get("error", {}).get("message", "")
            except ValueError:
                msg = ""
            raise PaymentError(f"Stripe returned {r.status_code}: {msg[:200]}")
        return r.json()

    def create_checkout(self, order, approval, title, success_url, cancel_url, test_page_url, countries=("US",)):
        data = {
            "mode": "payment",
            "success_url": success_url + "?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": cancel_url,
            "client_reference_id": str(order["id"]),
            "metadata[order_id]": str(order["id"]),
            "metadata[approval_id]": str(approval["id"]),
            "payment_intent_data[metadata][order_id]": str(order["id"]),
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": order["currency"],
            "line_items[0][price_data][unit_amount]": str(order["total_cents"]),
            "line_items[0][price_data][product_data][name]": title[:250],
        }
        for i, cc in enumerate(countries):
            data[f"shipping_address_collection[allowed_countries][{i}]"] = cc
        s = self._call("POST", "/checkout/sessions", data,
                       idem=f"collab-checkout-{order['id']}-{approval['id']}-{order['total_cents']}")
        return {"id": s["id"], "url": s["url"]}

    def retrieve(self, session_id):
        return self.view(self._call("GET", f"/checkout/sessions/{session_id}"))

    @staticmethod
    def view(s):
        """Normalise a Checkout Session object (from retrieve or a webhook event)."""
        ship = (s.get("collected_information") or {}).get("shipping_details") or s.get("shipping_details") or {}
        addr = ship.get("address") or {}
        cust = s.get("customer_details") or {}
        name = (ship.get("name") or cust.get("name") or "").split(" ", 1) + [""]
        return {"id": s["id"], "paid": s.get("payment_status") == "paid", "amount_cents": s.get("amount_total"),
                "currency": s.get("currency"), "metadata": s.get("metadata") or {},
                "shipping": {"first_name": name[0], "last_name": name[1], "email": cust.get("email") or "",
                             "phone": cust.get("phone") or "", "country": addr.get("country") or "",
                             "region": addr.get("state") or "", "address1": addr.get("line1") or "",
                             "address2": addr.get("line2") or "", "city": addr.get("city") or "",
                             "zip": addr.get("postal_code") or ""}}

    def verify_webhook(self, payload: bytes, sig_header: str, tolerance=300):
        if not self.webhook_secret:
            raise PaymentError("STRIPE_WEBHOOK_SECRET is not set")
        parts = dict(p.split("=", 1) for p in (sig_header or "").split(",") if "=" in p)
        sigs = [p.split("=", 1)[1] for p in (sig_header or "").split(",") if p.startswith("v1=")]
        t = parts.get("t", "")
        if not t.isdigit() or abs(time.time() - int(t)) > tolerance:
            raise PaymentError("webhook timestamp outside tolerance")
        expected = hmac.new(self.webhook_secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, s) for s in sigs):
            raise PaymentError("webhook signature mismatch")
        return True


def make_payments(env):
    if env.payment_provider == "stripe":
        if not env.stripe_secret_key:
            raise RuntimeError("COLLAB_PAYMENT_PROVIDER=stripe needs STRIPE_SECRET_KEY")
        return StripePayments(env.stripe_secret_key, env.stripe_webhook_secret)
    return SimulatedPayments()
