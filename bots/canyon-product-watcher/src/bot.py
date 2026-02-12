import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

BOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BOT_DIR / "data"
TARGETS_FILE = BOT_DIR / "targets.json"

REQUEST_TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BLOCK_PATTERNS = [
    "captcha",
    "cloudflare",
    "enable javascript",
    "unusual traffic",
    "access denied",
]

DEFAULT_STATE = {
    "sale_active": False,
    "last_price": None,
    "last_original_price": None,
    "last_signature": "",
}


# ----------------------------
# Helpers
# ----------------------------
def parse_bool_env(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def sanitize_target_id(target_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", target_id.strip())


def state_file_for_target(target_id: str) -> Path:
    safe_id = sanitize_target_id(target_id)
    return DATA_DIR / f"state_{safe_id}.json"


def load_targets() -> List[Dict[str, Any]]:
    with open(TARGETS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("targets.json must be a list")
    return data


def load_state(state_file: Path) -> Dict[str, Any]:
    if not state_file.exists():
        return dict(DEFAULT_STATE)

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return dict(DEFAULT_STATE)

    normalized = dict(DEFAULT_STATE)
    if isinstance(state, dict):
        normalized.update({k: state.get(k) for k in DEFAULT_STATE.keys() if k in state})
    return normalized


def save_state(state_file: Path, state: Dict[str, Any]) -> None:
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def telegram_send(message: str) -> bool:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    r = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": False,
        },
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()

    try:
        payload = r.json()
        if payload.get("ok") is False:
            return False
    except Exception:
        pass

    return True


def parse_price(raw: str) -> Optional[float]:
    if not raw:
        return None

    s = str(raw)
    s = s.replace("\u00a0", " ")
    s = s.replace("'", "").replace("’", "")
    s = re.sub(r"[^0-9,\.\-]", "", s)

    if not s:
        return None

    has_dot = "." in s
    has_comma = "," in s

    if has_dot and has_comma:
        last_dot = s.rfind(".")
        last_comma = s.rfind(",")
        decimal_sep = "." if last_dot > last_comma else ","
        thousands_sep = "," if decimal_sep == "." else "."
        normalized = s.replace(thousands_sep, "")
        normalized = normalized.replace(decimal_sep, ".")
    elif has_comma:
        normalized = s.replace(",", ".")
    elif has_dot:
        if re.search(r"\d+\.\d{2}$", s):
            normalized = s
        else:
            normalized = s.replace(".", "")
    else:
        normalized = s

    try:
        return float(normalized)
    except Exception:
        return None


def find_ldjson_script_contents(html: str) -> List[str]:
    pattern = re.compile(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        re.IGNORECASE | re.DOTALL,
    )
    return [m.strip() for m in pattern.findall(html or "") if m and m.strip()]


def iter_dicts(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_dicts(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_dicts(item)


def get_type_values(node: Dict[str, Any]) -> List[str]:
    t = node.get("@type")
    if isinstance(t, str):
        return [t]
    if isinstance(t, list):
        return [x for x in t if isinstance(x, str)]
    return []


def extract_current_price(html: str, currency_expected: str) -> Optional[float]:
    candidates: List[float] = []

    for content in find_ldjson_script_contents(html):
        try:
            parsed = json.loads(content)
        except Exception:
            continue

        for node in iter_dicts(parsed):
            types = {t.lower() for t in get_type_values(node)}
            if "product" not in types:
                continue

            offers = node.get("offers")
            if isinstance(offers, dict):
                offer_nodes = [offers]
            elif isinstance(offers, list):
                offer_nodes = [x for x in offers if isinstance(x, dict)]
            else:
                offer_nodes = []

            for offer in offer_nodes:
                currency = str(offer.get("priceCurrency") or "").strip()
                if currency != currency_expected:
                    continue

                price_val = parse_price(str(offer.get("price") or ""))
                if price_val is None:
                    continue

                candidates.append(price_val)

    if not candidates:
        return None

    return min(candidates)


def extract_original_price(html: str) -> Optional[float]:
    m = re.search(
        r"<s[^>]*class=[\"'][^\"']*productDescription__priceOriginal[^\"']*[\"'][^>]*>(.*?)</s>",
        html or "",
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None

    inner = m.group(1)
    text = re.sub(r"<[^>]+>", " ", inner)
    return parse_price(text)


def is_blocked_html(html: str) -> bool:
    low = (html or "").lower()
    return any(p in low for p in BLOCK_PATTERNS)


def fetch_html(url: str) -> Optional[str]:
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException:
        return None

    if r.status_code != 200:
        return None

    html = r.text
    if is_blocked_html(html):
        return None

    return html


def build_signature(current_price: Optional[float], original_price: Optional[float], sale_active: bool) -> str:
    c = "" if current_price is None else f"{current_price:.2f}"
    o = "" if original_price is None else f"{original_price:.2f}"
    return f"sale={int(sale_active)}|current={c}|original={o}"


def run_target(target: Dict[str, Any], dry_run: bool) -> None:
    target_id = str(target.get("id") or "").strip()
    label = str(target.get("label") or target_id).strip()
    url = str(target.get("url") or "").strip()
    currency_expected = str(target.get("currency_expected") or "").strip()

    if not target_id or not url or not currency_expected:
        print(f"{target_id or 'unknown'}: NO_CHANGE")
        return

    state_path = state_file_for_target(target_id)
    prev_state = load_state(state_path)

    html = fetch_html(url)
    if not html:
        print(f"{target_id}: NO_CHANGE")
        return

    current_price = extract_current_price(html, currency_expected)
    if current_price is None:
        print(f"{target_id}: NO_CHANGE")
        return

    original_price = extract_original_price(html)

    sale_active_now = (
        original_price is not None
        and original_price > current_price + 0.01
    )

    next_state = {
        "sale_active": bool(sale_active_now),
        "last_price": current_price,
        "last_original_price": original_price,
        "last_signature": build_signature(current_price, original_price, sale_active_now),
    }

    should_notify = (not bool(prev_state.get("sale_active", False))) and sale_active_now

    if should_notify:
        text = (
            f"{label}: sale started\n"
            f"Current: {current_price:.2f} {currency_expected}\n"
            f"Original: {original_price:.2f} {currency_expected}\n"
            f"{url}"
        )

        if dry_run:
            print(f"{target_id}: WOULD_NOTIFY")
            return

        try:
            sent = telegram_send(text)
        except Exception:
            sent = False

        if sent:
            save_state(state_path, next_state)
            print(f"{target_id}: NOTIFY")
        else:
            save_state(state_path, next_state)
            print(f"{target_id}: NO_CHANGE")
        return

    if not dry_run:
        save_state(state_path, next_state)

    print(f"{target_id}: NO_CHANGE")


def main() -> None:
    dry_run = parse_bool_env(os.getenv("DRY_RUN"))
    targets = load_targets()

    for t in targets:
        try:
            run_target(t, dry_run)
        except Exception:
            target_id = str(t.get("id") or "unknown").strip() or "unknown"
            print(f"{target_id}: NO_CHANGE")


if __name__ == "__main__":
    main()
