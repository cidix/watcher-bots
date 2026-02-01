import os
import json
import time
from typing import Any, Dict, Optional, Tuple, List

import requests

# ----------------------------
# Config
# ----------------------------
STATE_FILE = "mnstry_state.json"

MNSTRY_BASE = "https://mnstry.com"
MNSTRY_HOME = f"{MNSTRY_BASE}/"
MNSTRY_PRODUCTS_JSON = f"{MNSTRY_BASE}/products.json?limit=250"

REQUEST_TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)

TOP_N = 5  # Top 5 in message 1, next 5 in message 2 if available


# ----------------------------
# State
# ----------------------------
def load_state() -> Dict[str, Any]:
    """
    sale_active: was beim letzten Lauf irgendein rabattiertes Produkt aktiv?
    last_signature: Signatur des Top-Deals (nur Diagnose)
    """
    if not os.path.exists(STATE_FILE):
        return {"sale_active": False, "last_signature": ""}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ----------------------------
# Telegram
# ----------------------------
def telegram_send(message: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "disable_web_page_preview": False}

    r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()


# ----------------------------
# HTTP helpers
# ----------------------------
def http_get_json(url: str) -> Optional[Dict[str, Any]]:
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

    # Wenn blockiert / temporär down: still skippen (keine Fehlalarme)
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except Exception:
        return None


# ----------------------------
# Discount detection
# ----------------------------
def calc_discount(compare_at: float, price: float) -> Tuple[float, float]:
    """
    Returns: (discount_abs, discount_pct)
    """
    discount_abs = compare_at - price
    if compare_at <= 0:
        return discount_abs, 0.0
    discount_pct = (discount_abs / compare_at) * 100.0
    return discount_abs, discount_pct


def collect_deals(products: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int, int]:
    """
    Returns:
      deals: list of discounted variants with computed discount
      discounted_products_count: Produkte mit mind. 1 rabattierter Variante
      discounted_variants_count: Anzahl rabattierter Varianten
    """
    deals: List[Dict[str, Any]] = []
    discounted_products_count = 0
    discounted_variants_count = 0

    for p in products:
        title = p.get("title") or "MNSTRY Product"
        handle = p.get("handle") or ""
        url = f"{MNSTRY_BASE}/products/{handle}" if handle else MNSTRY_HOME

        product_has_discount = False

        for v in p.get("variants", []):
            price = to_float(v.get("price"))
            cap = to_float(v.get("compare_at_price"))
            variant_id = v.get("id")

            if price is None or cap is None:
                continue

            if cap > price:
                product_has_discount = True
                discounted_variants_count += 1

                disc_abs, disc_pct = calc_discount(cap, price)

                deals.append({
                    "title": title,
                    "url": url,
                    "variant_id": int(variant_id) if variant_id is not None else 0,
                    "price": price,
                    "compare_at": cap,
                    "discount_abs": disc_abs,
                    "discount_pct": disc_pct,
                })

        if product_has_discount:
            discounted_products_count += 1

    return deals, discounted_products_count, discounted_variants_count


def rank_deals(deals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Ranking:
      1) Rabatt % desc
      2) Rabatt CHF desc
      3) Preis asc (günstiger bevorzugt)
    """
    return sorted(
        deals,
        key=lambda d: (-d["discount_pct"], -d["discount_abs"], d["price"], d["variant_id"])
    )


def format_deal_line(d: Dict[str, Any]) -> str:
    return (
        f"• {d['title']}\n"
        f"  {d['compare_at']:.2f} → {d['price']:.2f}  "
        f"(-{d['discount_abs']:.2f} / {d['discount_pct']:.1f}%)\n"
        f"  {d['url']}"
    )


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    state = load_state()

    data = http_get_json(MNSTRY_PRODUCTS_JSON)
    if not data:
        # still fail: keine Meldung, Status nicht kaputt machen
        return

    products = data.get("products", [])
    deals, discounted_products, discounted_variants = collect_deals(products)

    sale_now = len(deals) > 0
    ranked = rank_deals(deals)

    top5 = ranked[:TOP_N]
    next5 = ranked[TOP_N:TOP_N * 2]

    signature = ""
    if top5:
        d0 = top5[0]
        signature = f"{d0.get('variant_id',0)}|{d0['compare_at']:.2f}>{d0['price']:.2f}"

    # Meldung nur beim Wechsel: False -> True
    if (not state.get("sale_active", False)) and sale_now:
        # Message 1: Summary + Top 5
        header_1 = (
            "🚨 MNSTRY Rabattaktion erkannt!\n\n"
            f"📦 Reduzierte Produkte: {discounted_products}\n"
            f"🏷️ Reduzierte Varianten: {discounted_variants}\n"
            f"🔗 {MNSTRY_HOME}\n\n"
            "🔥 Top 5 Deals:\n"
        )
        body_1 = "\n\n".join(format_deal_line(d) for d in top5) if top5 else "• (keine Details verfügbar)"
        telegram_send(header_1 + body_1)

        # Message 2: Always send (per your request)
        remaining_variants = max(0, discounted_variants - len(top5))
        header_2 = (
            "📩 Weitere Infos:\n"
            f"• Weitere reduzierte Varianten (nach Top 5): {remaining_variants}\n"
        )

        if next5:
            header_2 += "\n➡️ Nächste Top 5:\n"
            body_2 = "\n\n".join(format_deal_line(d) for d in next5)
            telegram_send(header_2 + body_2)
        else:
            header_2 += "\n(Keine weiteren Deals in den nächsten 5.)"
            telegram_send(header_2)

    # Reset, wenn kein Sale mehr aktiv ist (ohne Meldung)
    state["sale_active"] = sale_now
    state["last_signature"] = signature
    save_state(state)


if __name__ == "__main__":
    # Retry bei kurzen Netzwerk-Hickups
    for attempt in range(3):
        try:
            main()
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3)


