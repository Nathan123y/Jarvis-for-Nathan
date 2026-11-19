"""Fulfillment partners. services.submit_to_production() checks every gate BEFORE calling a provider.

manual    You send the production pack (downloadable from the admin order page) to your manufacturer yourself and
          record their reference. Default.
printify  Creates a Printify order with print areas made on the fly. Needs PRINTIFY_API_TOKEN, PRINTIFY_SHOP_ID and,
          per garment in admin settings, `printify` = {"blueprint_id", "print_provider_id", "variants": {size: id},
          "positions": {panel: printify position}} copied from Printify's catalog. Nothing is assumed.
"""
import base64


class FulfillmentError(RuntimeError):
    pass


class ManualFulfillment:
    name = "manual"

    def submit(self, order, req, approval, garment_cfg, artwork, read_bytes, external_ref=""):
        return {"ref": external_ref or f"manual-{req['ref']}",
                "note": "Marked as submitted manually. Send the production pack to your manufacturer."}


class PrintifyFulfillment:
    name = "printify"
    API = "https://api.printify.com/v1"

    def __init__(self, token, shop_id, http=None):
        if http is None:
            import requests
            http = requests
        self.token, self.shop_id, self.http = token, shop_id, http

    def _call(self, method, path, body):
        r = self.http.request(method, f"{self.API}/{path}", json=body, timeout=60,
                              headers={"Authorization": f"Bearer {self.token}"})
        if r.status_code >= 300:
            raise FulfillmentError(f"Printify returned {r.status_code}: {r.text[:300]}")
        return r.json()

    def submit(self, order, req, approval, garment_cfg, artwork, read_bytes, external_ref=""):
        if not (self.token and self.shop_id):
            raise FulfillmentError("Printify credentials aren't configured.")
        p = garment_cfg.get("printify") or {}
        variant = (p.get("variants") or {}).get(approval["size"])
        if not (p.get("blueprint_id") and p.get("print_provider_id") and variant and p.get("positions")):
            raise FulfillmentError("This garment has no verified Printify mapping (blueprint, provider, variant for "
                                   f"size {approval['size']}, positions). Add it in Settings.")
        addr = order.get("shipping") or {}
        if not addr.get("address1"):
            raise FulfillmentError("The order has no shipping address.")
        areas = {}
        for panel, asset in artwork.items():
            pos = p["positions"].get(panel)
            if not pos:
                continue
            up = self._call("POST", "uploads/images.json", {"file_name": f"{req['ref']}-{panel}.png",
                                                             "contents": base64.b64encode(read_bytes(asset)).decode()})
            areas[pos] = up["preview_url"]
        body = {
            "external_id": f"collab-{order['id']}",
            "label": req["ref"],
            "line_items": [{"blueprint_id": p["blueprint_id"], "print_provider_id": p["print_provider_id"],
                            "variant_id": variant, "print_areas": areas, "quantity": approval["quantity"]}],
            "shipping_method": 1,
            "send_shipping_notification": False,
            "address_to": {k: addr.get(k, "") for k in ("first_name", "last_name", "email", "phone", "country",
                                                          "region", "address1", "address2", "city", "zip")},
        }
        res = self._call("POST", f"shops/{self.shop_id}/orders.json", body)
        return {"ref": str(res.get("id", "")), "note": "Created in Printify. Review and send it to production there."}


def make_fulfillment(env):
    if env.fulfillment_provider == "printify":
        return PrintifyFulfillment(env.printify_api_token, env.printify_shop_id)
    return ManualFulfillment()
