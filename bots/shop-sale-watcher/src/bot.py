import os
import json
import time
import re
from typing import Any, Dict, Optional, Tuple, List
from pathlib import Path

import requests

# ----------------------------
# Config
# ----------------------------
MNSTRY_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = MNSTRY_DIR / "data"
TARGETS_FILE = MNSTRY_DIR / "targets.json"

REQUEST_TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)

TOP_N = 5  # Top 5 in message 1, next 5 in message 2 if available

# Set to True if you want a "Sale ended" message when discounts disappear
NOTIFY_SALE_END = False

# How many total attempts for transient errors
MAX_ATTEMPTS = 3
RETRY_SLEEP_SECONDS = 3


# ----------------------------
# DRY_RUN
# ----------------------------
def parse_bool_env(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


DRY_RUN = parse_bool_env(os.getenv("DRY_RUN"))


# ----------------------------
# Targets
# ----------------------------
def load_targets() -> List[Dict[str, Any]]:
    with open(TARGETS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("targets.json must be a list of target objects")
    return data


def sanitize_target_id(target_id: str) -> str:
    # Keep filename safe and stable
    return re.sub(r"[^A-Za-z0-9_-]+", "_", target_id.strip())


def state_file_for_target(target_id: str) -> str:
    safe_id = sanitize_target_id(target_id)
    return str(DATA_DIR / f"state_{safe_id}.json")


def normalize_base_url(url: str) -> str:
    u = (url or "").strip()
    if u.endswith("/"):
        u = u[:-1]
    return u


# ----------------------------
# State
# ----------------------------
def load_state(state_file: str) -> Dict[str, Any]:
    """
    sale_active: was beim letzten Lauf irgendein rabattiertes Produkt aktiv?
    last_signature: Signatur des Top-Deals (nur Diagnose)
    """
    if not os.path.exists(state_file):
        return {"sale_active": False, "last_signature": ""}

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        # Normalize expected keys
        if "sale_active" not in state:
            state["sale_active"] = False
        if "last_signature" not in state:
            state["last_signature"] = ""
        return state
    except Exception:
        # Corrupt or partial file: reset
        return {"sale_active": False, "last_signature": ""}


def save_state(state_file: str, state: Dict[str, Any]) -> None:
    with open(state_file, "w", encoding="utf-8") as f:
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

    try:
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return None

    # If blocked / temporary issue: skip (avoid false alerts)
    if r.status_code != 200:
        return None

    # Shopify JSON should be JSON; if HTML arrives (WAF/Block), skip
    ct = (r.headers.get("content-type") or "").lower()
    if "application/json" not in ct and "json" not in ct:
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


def collect_deals(
    products: List[Dict[str, Any]],
    base_url: str,
    home_url: str
) -> Tuple[List[Dict[str, Any]], int, int]:
    """
    Returns:
      deals: list of discounted variants with computed discount
      discounted_products_count: Produkte mit mind. 1 rabattierter Variante
      discounted_variants_count: Anzahl rabattierter Varianten
    """
    deals: List[Dict[str, Any]] = []
    discounted_products_count = 0
    discounted_variants_count = 0

    seen_variant_ids = set()

    for p in products:
        title = p.get("title") or "Product"
        handle = p.get("handle") or ""
        url = f"{base_url}/products/{handle}" if handle else home_url

        product_has_discount = False

        for v in p.get("variants", []) or []:
            price = to_float(v.get("price"))
            cap = to_float(v.get("compare_at_price"))
            variant_id = v.get("id")

            if price is None or cap is None:
                continue

            # Dedupe
            vid_int = int(variant_id) if variant_id is not None else 0
            if vid_int in seen_variant_ids:
                continue
            seen_variant_ids.add(vid_int)

            if cap > price:
                product_has_discount = True
                discounted_variants_count += 1

                disc_abs, disc_pct = calc_discount(cap, price)

                deals.append({
                    "title": title,
                    "url": url,
                    "variant_id": vid_int,
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
      4) variant_id (stable)
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
# Target runner
# ----------------------------
def run_once_for_target(target: Dict[str, Any], state_file: str) -> Tuple[Dict[str, Any], List[str], str]:
    target_id = str(target.get("id") or "").strip()
    label = str(target.get("label") or target_id).strip()
    base_url = normalize_base_url(str(target.get("url") or "").strip())
    if not target_id or not base_url:
        raise ValueError("Invalid target config: id and url are required")

    home_url = f"{base_url}/"
    products_json = f"{base_url}/products.json?limit=250"

    state = load_state(state_file)

    data = http_get_json(products_json)
    if not data:
        # Keep state unchanged and still return a summary so we always log one line per target
        return state, [], f"{target_id}: NO_CHANGE"

    products = data.get("products", []) or []
    deals, discounted_products, discounted_variants = collect_deals(products, base_url, home_url)

    sale_now = len(deals) > 0
    ranked = rank_deals(deals)

    top = ranked[:TOP_N]
    next_top = ranked[TOP_N:TOP_N * 2]

    signature = ""
    if top:
        d0 = top[0]
        signature = f"{d0.get('variant_id', 0)}|{d0['compare_at']:.2f}>{d0['price']:.2f}"

    was_active = bool(state.get("sale_active", False))

    notifications: List[str] = []

    # Notify only on transition: False -> True
    if (not was_active) and sale_now:
        header_1 = (
            f"🚨 {label}: Rabattaktion erkannt!\n\n"
            f"📦 Reduzierte Produkte: {discounted_products}\n"
            f"🏷️ Reduzierte Varianten: {discounted_variants}\n"
            f"🔗 {home_url}\n\n"
            f"🔥 Top {min(TOP_N, len(top)) if top else TOP_N} Deals:\n"
        )
        body_1 = "\n\n".join(format_deal_line(d) for d in top) if top else "• (keine Details verfügbar)"
        notifications.append(header_1 + body_1)

        remaining_variants = max(0, discounted_variants - len(top))
        header_2 = (
            "📩 Weitere Infos:\n"
            f"• Weitere reduzierte Varianten (nach Top {len(top)}): {remaining_variants}\n"
        )

        if next_top:
            header_2 += "\n➡️ Nächste Top Deals:\n"
            body_2 = "\n\n".join(format_deal_line(d) for d in next_top)
            notifications.append(header_2 + body_2)
        else:
            header_2 += "\n(Keine weiteren Deals in den nächsten Slots.)"
            notifications.append(header_2)

    if was_active and (not sale_now) and NOTIFY_SALE_END:
        notifications.append(f"✅ {label}: Rabattaktion scheint beendet (keine reduzierten Varianten mehr gefunden).")

    # Update state (DRY_RUN will not persist it)
    state["sale_active"] = sale_now
    state["last_signature"] = signature

    summary = f"{target_id}: NO_CHANGE"
    if notifications:
        summary = f"{target_id}: WOULD_NOTIFY ({len(notifications)} items)" if DRY_RUN else f"{target_id}: NOTIFY ({len(notifications)} items)"

    return state, notifications, summary


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    targets = load_targets()

    for target in targets:
        target_id = str(target.get("id") or "").strip() or "unknown"
        state_file = state_file_for_target(target_id)

        try:
            for attempt in range(MAX_ATTEMPTS):
                try:
                    new_state, notifications, summary = run_once_for_target(target, state_file)

                    # Always print one result line per target
                    print(summary)

                    if DRY_RUN:
                        # In DRY_RUN: do not send Telegram, do not write state
                        for msg in notifications:
                            print(f"[DRY_RUN] Would send Telegram for {target_id}:")
                            print(msg)
                        break

                    # Normal mode: send only if there is something to notify
                    for msg in notifications:
                        telegram_send(msg)

                    # Persist per-target state only in normal mode
                    save_state(state_file, new_state)
                    break

                except requests.RequestException:
                    if attempt == MAX_ATTEMPTS - 1:
                        raise
                    time.sleep(RETRY_SLEEP_SECONDS * (attempt + 1))
                except Exception:
                    if attempt == MAX_ATTEMPTS - 1:
                        raise
                    time.sleep(RETRY_SLEEP_SECONDS)

        except Exception as e:
            print(f"{target_id}: ERROR {e}")
            if not DRY_RUN:
                # Minimal warning; do not abort whole run
                try:
                    telegram_send(f"MNSTRY bot error on {target_id}: {e}")
                except Exception:
                    pass
            continue


if __name__ == "__main__":
    main()
