"""Collab Studio settings.

Two layers:
- Environment (secrets and provider choices). Read once at start-up; never sent to the browser.
- Business settings (garments, prices, limits, shop links). Stored in the database, edited in the admin dashboard.
  DEFAULT_SETTINGS below is only the first-run seed.
"""
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Tiny .env reader (KEY=VALUE lines). Real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Env:
    data_dir: Path
    base_url: str
    secret_key: str
    admin_password: str
    admin_password_file: str = ""
    image_provider: str = "demo"          # demo | codex | openai
    openai_api_key: str = ""
    openai_image_model: str = "gpt-image-1"
    payment_provider: str = "simulated"   # simulated | stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    email_provider: str = "preview"       # preview | smtp
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = "Collab Studio <studio@example.com>"
    fulfillment_provider: str = "manual"  # manual | printify
    printify_api_token: str = ""
    printify_shop_id: str = ""
    run_worker: bool = True
    cookie_secure: bool = False
    extra: dict = field(default_factory=dict)


def _secret_from_file(path: Path) -> str:
    """A random secret kept in the data folder (mode 600) so dev restarts keep sessions and the admin password."""
    if path.is_file():
        return path.read_text().strip()
    val = secrets.token_urlsafe(24)
    path.write_text(val)
    os.chmod(path, 0o600)
    return val


def load_env(data_dir: str = "") -> Env:
    _load_dotenv(PACKAGE_DIR / ".env")
    g = os.environ.get
    d = Path(data_dir or g("COLLAB_DATA_DIR") or (PACKAGE_DIR.parent / "data" / "collab_studio")).resolve()
    d.mkdir(parents=True, exist_ok=True)
    (d / "files").mkdir(exist_ok=True)
    pw = g("COLLAB_ADMIN_PASSWORD", "")
    pw_file = ""
    if not pw:
        pw_file = str(d / "admin_password.txt")
        pw = _secret_from_file(Path(pw_file))
    stripe_key = g("STRIPE_SECRET_KEY", "")
    payment = g("COLLAB_PAYMENT_PROVIDER", "stripe" if stripe_key else "simulated")
    if payment == "stripe" and stripe_key.startswith("sk_live") and g("COLLAB_ALLOW_LIVE_PAYMENTS") != "1":
        raise RuntimeError("A live Stripe key is set. Use a test key (sk_test_...) or set COLLAB_ALLOW_LIVE_PAYMENTS=1.")
    return Env(
        data_dir=d,
        base_url=g("COLLAB_BASE_URL", "http://127.0.0.1:8470").rstrip("/"),
        secret_key=g("COLLAB_SECRET_KEY") or _secret_from_file(d / "secret_key.txt"),
        admin_password=pw,
        admin_password_file=pw_file,
        image_provider=g("COLLAB_IMAGE_PROVIDER", "demo"),
        openai_api_key=g("OPENAI_API_KEY", ""),
        openai_image_model=g("COLLAB_OPENAI_IMAGE_MODEL", "gpt-image-1"),
        payment_provider=payment,
        stripe_secret_key=stripe_key,
        stripe_webhook_secret=g("STRIPE_WEBHOOK_SECRET", ""),
        email_provider=g("COLLAB_EMAIL_PROVIDER", "smtp" if g("SMTP_HOST") else "preview"),
        smtp_host=g("SMTP_HOST", ""),
        smtp_port=int(g("SMTP_PORT", "587")),
        smtp_user=g("SMTP_USER", ""),
        smtp_password=g("SMTP_PASSWORD", ""),
        email_from=g("COLLAB_EMAIL_FROM", "Collab Studio <studio@example.com>"),
        fulfillment_provider=g("COLLAB_FULFILLMENT_PROVIDER", "manual"),
        printify_api_token=g("PRINTIFY_API_TOKEN", ""),
        printify_shop_id=g("PRINTIFY_SHOP_ID", ""),
        run_worker=g("COLLAB_RUN_WORKER", "1") == "1",
        cookie_secure=g("COLLAB_BASE_URL", "").startswith("https://"),
    )


# ── Business settings seed (editable in /admin/settings) ─────────────────────────────────────────────────────────────
# Prices and shipping are DEMONSTRATION values until you replace them ("prices_are_demo" shows a badge to customers).
# Size charts and print-panel pixel requirements are intentionally empty: fill them in from your manufacturer's
# verified product page. Nothing here is a manufacturer specification.
DEFAULT_SETTINGS = {
    "business_name": "Collab Studio",
    "support_email": "studio@example.com",
    "currency": "usd",
    "ship_countries": ["US"],
    "prices_are_demo": True,
    "revision_limit": 1,
    "max_generations_per_request": 4,
    "generation_max_attempts": 3,
    "require_admin_review": True,
    "require_admin_proof": False,
    "tax_rate_percent": None,
    "tax_note": "Sales tax isn't configured yet. If tax applies, it's shown before you pay.",
    "turnaround_design": "Concepts usually arrive within 2 business days.",
    "turnaround_production": "Production and shipping times are confirmed with your final proof.",
    "shop_links": [
        {"label": "TikTok Shop", "url": ""},
        {"label": "Etsy", "url": ""},
        {"label": "eBay", "url": ""},
        {"label": "Pop-Up Store", "url": ""},
    ],
    "garments": {
        "soccer": {
            "label": "Collared soccer jersey",
            "blurb": "Short sleeves, collar, full all-over print front, back and sleeves.",
            "price": 64.00, "shipping": 8.00, "enabled": True,
            "sizes": ["S", "M", "L", "XL", "2XL", "3XL"],
            "size_chart": [],
            "panels": [{"name": "front"}, {"name": "back"}, {"name": "right_sleeve"}, {"name": "left_sleeve"},
                       {"name": "collar"}],
            "image": "gallery/garnet-royale-front.jpg",
        },
        "basketball": {
            "label": "Basketball jersey",
            "blurb": "Sleeveless, deep armholes, oversized numbers front and back.",
            "price": 59.00, "shipping": 8.00, "enabled": True,
            "sizes": ["S", "M", "L", "XL", "2XL", "3XL"],
            "size_chart": [],
            "panels": [{"name": "front"}, {"name": "back"}],
            "image": "gallery/starfire-front.jpg",
        },
        "tee": {
            "label": "Short-sleeve shirt",
            "blurb": "An everyday tee carrying the same front-and-back artwork.",
            "price": 42.00, "shipping": 6.00, "enabled": True,
            "sizes": ["S", "M", "L", "XL", "2XL", "3XL"],
            "size_chart": [],
            "panels": [{"name": "front"}, {"name": "back"}],
            "image": "gallery/sunset-tide-front.jpg",
        },
    },
    "styles": {
        "vintage_waves": {"label": "Vintage waves",
                          "blurb": "Flowing asymmetric waves, warped stripes, washed colours, cracked lettering."},
        "bold_graphic": {"label": "Bold graphic",
                         "blurb": "One oversized hero graphic, high contrast, loud from across the room."},
        "clean_athletic": {"label": "Clean athletic",
                           "blurb": "Crisp panels and pinstripes, restrained texture, classic numbers."},
    },
    "faq": [
        ["Are these official team or player products?",
         "No. Every design is an original fan-inspired concept made by us. We don't use official logos, crests or "
         "wordmarks, and nothing we make is licensed by or affiliated with any team, league or player."],
        ["Can I use my own team, business or mascot?",
         "Yes. If you own the name or logo, tell us in your request and attach references. We may ask you to confirm "
         "you have the right to use it before production."],
        ["How many changes can I ask for?",
         "{revisions} included with every request. If you need more, reply to your design email and we'll sort it out."],
        ["When do I pay?",
         "Only after you've chosen a design and approved the final proof. Submitting an idea is free."],
        ["How long does it take?",
         "Design times are shown on the request form. Production and shipping estimates are confirmed with your "
         "final proof, before you pay."],
    ],
}
