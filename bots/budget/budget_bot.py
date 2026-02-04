#!/usr/bin/env python3
import os
import re
import json
import urllib.request
import urllib.parse
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, date
from typing import Optional, List, Dict, Tuple

# =========================
# Config
# =========================
TELEGRAM_BOT_TOKEN = os.environ["BUDGET_TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["BUDGET_TELEGRAM_CHAT_ID"]  # numeric string
DATA_DIR = os.environ.get("BUDGET_DATA_DIR", "bots/budget/data")

# Main currency CHF. THB secondary.
THB_TO_CHF = float(os.environ.get("BUDGET_THB_TO_CHF", "0.026"))  # fixed rate

EXPENSES_PATH = os.path.join(DATA_DIR, "expenses.jsonl")
STATE_PATH = os.path.join(DATA_DIR, "state.json")


# =========================
# Helpers
# =========================
def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(EXPENSES_PATH):
        with open(EXPENSES_PATH, "w", encoding="utf-8") as f:
            pass
    if not os.path.exists(STATE_PATH):
        save_state({
            "last_update_id": 0,
            "hints_shown": {
                "currency_default": False,
            }
        })


def load_state() -> Dict:
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def swiss_money(x: float) -> str:
    # Swiss formatting: 1’234.56
    s = f"{x:,.2f}"
    return s.replace(",", "’")


def iso_from_eu(ddmmyy: str) -> str:
    # DD.MM.YY
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{2})", ddmmyy)
    if not m:
        raise ValueError("Invalid EU date. Use DD.MM.YY (e.g. 12.02.26)")
    dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    year = 2000 + yy
    return date(year, mm, dd).isoformat()


def parse_relative_date(tok: str) -> Optional[Tuple[str, str]]:
    t = tok.lower()
    if t == "today":
        return (date.today().isoformat(), "today")
    if t == "yesterday":
        return ((date.today() - timedelta(days=1)).isoformat(), "yesterday")
    return None


def normalize_amount_token(tok: str) -> Optional[float]:
    # Accept: 1200, 1200.5, 1200.50, -1200, -1200.5
    if not re.fullmatch(r"-?\d+(\.\d{1,2})?", tok):
        return None
    return float(tok)


@dataclass
class ParsedExpense:
    id: str
    date_iso: str
    date_input_raw: Optional[str]
    amount_original: float
    currency_original: str  # CHF | THB
    amount_chf: float
    category: str  # hotel | transport | activity | misc
    subcategory: Optional[str]  # flight | ferry | bus | None
    nights: Optional[int]  # only for hotel
    note: str
    source: str  # telegram | discord
    raw_input: str
    flags: List[str]


# =========================
# Parsing (Final Scope)
# =========================
HOTEL_TOKENS = {"hotel", "bungalow", "resort"}
CURRENCY_TOKENS = {"chf", "thb"}
TRANSPORT_SUB = {"flight", "ferry", "bus"}
CATEGORIES = {"hotel", "transport", "activity", "misc"}


def parse_input(text: str, source: str = "telegram") -> ParsedExpense:
    raw = text.strip()
    tokens = [t for t in raw.split() if t]
    flags: List[str] = []

    # 1) Date: EU DD.MM.YY OR today/yesterday. Default today.
    date_iso = date.today().isoformat()
    date_input_raw = None
    remaining: List[str] = []
    date_set = False

    for tok in tokens:
        if not date_set:
            rel = parse_relative_date(tok)
            if rel:
                date_iso, date_input_raw = rel
                date_set = True
                continue
            if re.fullmatch(r"\d{2}\.\d{2}\.\d{2}", tok):
                date_iso = iso_from_eu(tok)
                date_input_raw = tok
                date_set = True
                continue
        remaining.append(tok)

    if not date_set:
        flags.append("date_default_today")

    tokens = remaining

    # 2) Amount (first numeric token)
    amount_val: Optional[float] = None
    remaining = []
    for tok in tokens:
        if amount_val is None:
            v = normalize_amount_token(tok)
            if v is not None:
                amount_val = v
                continue
        remaining.append(tok)

    if amount_val is None:
        raise ValueError("No amount found. Provide e.g. 1200 or 1200.5")

    amount_val = float(f"{amount_val:.2f}")
    tokens = remaining

    # 3) Currency (chf/thb). Default CHF.
    currency = "CHF"
    remaining = []
    currency_set = False
    for tok in tokens:
        tl = tok.lower()
        if (not currency_set) and tl in CURRENCY_TOKENS:
            currency = "THB" if tl == "thb" else "CHF"
            currency_set = True
            continue
        remaining.append(tok)
    if not currency_set:
        flags.append("used_default_currency")
    tokens = remaining

    # 4) Category + Subcategory
    category: Optional[str] = None
    subcategory: Optional[str] = None
    remaining = []

    for tok in tokens:
        tl = tok.lower()
        if category is None and tl in HOTEL_TOKENS:
            category = "hotel"
            continue
        if subcategory is None and tl in TRANSPORT_SUB:
            category = "transport"
            subcategory = tl
            continue
        if category is None and tl in CATEGORIES:
            category = tl
            continue
        remaining.append(tok)

    if category is None:
        category = "misc"
        flags.append("used_default_category")

    tokens = remaining

    # 5) Hotel nights: only pattern "<int> night"
    nights: Optional[int] = None
    remaining = []
    i = 0
    while i < len(tokens):
        if category == "hotel" and i + 1 < len(tokens):
            if re.fullmatch(r"\d+", tokens[i]) and tokens[i + 1].lower() == "night":
                nights = int(tokens[i])
                i += 2
                continue
        remaining.append(tokens[i])
        i += 1

    if category == "hotel":
        if nights is None:
            nights = 1
            flags.append("hotel_default_nights")
    else:
        nights = None

    note = " ".join(remaining).strip()

    # 6) Convert to CHF (fixed FX)
    if currency == "CHF":
        amount_chf = amount_val
    else:
        amount_chf = float(f"{(amount_val * THB_TO_CHF):.2f}")

    # ID
    now = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    eid = f"exp_{now}"

    return ParsedExpense(
        id=eid,
        date_iso=date_iso,
        date_input_raw=date_input_raw,
        amount_original=amount_val,
        currency_original=currency,
        amount_chf=amount_chf,
        category=category,
        subcategory=subcategory,
        nights=nights,
        note=note,
        source=source,
        raw_input=raw,
        flags=flags
    )


# =========================
# Storage
# =========================
def append_expense(exp: ParsedExpense) -> None:
    with open(EXPENSES_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(exp), ensure_ascii=False) + "\n")


def load_expenses() -> List[Dict]:
    out = []
    if not os.path.exists(EXPENSES_PATH):
        return out
    with open(EXPENSES_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


# =========================
# Telegram API
# =========================
def tg_api(method: str, params: Dict) -> Dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def tg_get_updates(offset: int) -> List[Dict]:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = urllib.parse.urlencode({"timeout": 0, "offset": offset}).encode("utf-8")
    req = urllib.request.Request(url, data=params, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload.get("ok"):
        return []
    return payload.get("result", [])


def tg_send(chat_id: str, text: str) -> None:
    tg_api("sendMessage", {"chat_id": chat_id, "text": text})


# =========================
# Reporting
# =========================
def today_total_chf(expenses: List[Dict], day_iso: str) -> float:
    return sum(e["amount_chf"] for e in expenses if e["date_iso"] == day_iso)


def summarize_today(expenses: List[Dict], day_iso: str) -> str:
    day_items = [e for e in expenses if e["date_iso"] == day_iso]
    if not day_items:
        return f"📅 Today ({day_iso})\nNo entries yet."

    total = sum(e["amount_chf"] for e in day_items)

    cats = {"hotel": 0.0, "transport": 0.0, "activity": 0.0, "misc": 0.0}
    hotel_nights = 0
    hotel_total = 0.0
    for e in day_items:
        cats[e["category"]] += e["amount_chf"]
        if e["category"] == "hotel":
            hotel_total += e["amount_chf"]
            hotel_nights += int(e["nights"] or 1)

    lines = [
        f"📊 Today ({day_iso})",
        f"Total: {swiss_money(total)} CHF",
        "",
        "Breakdown:"
    ]
    for k in ["hotel", "transport", "activity", "misc"]:
        if abs(cats[k]) > 0.0001:
            lines.append(f"- {k}: {swiss_money(cats[k])} CHF")

    if hotel_nights > 0:
        lines.append(f"- hotel avg/night: {swiss_money(hotel_total / hotel_nights)} CHF ({hotel_nights} night)")

    return "\n".join(lines)


def summarize_stats(expenses: List[Dict]) -> str:
    if not expenses:
        return "📊 Stats\nNo entries yet."

    total = sum(e["amount_chf"] for e in expenses)

    dates = sorted({e["date_iso"] for e in expenses})
    first = date.fromisoformat(dates[0])
    last = date.fromisoformat(dates[-1])
    days = (last - first).days + 1
    avg_day = total / days if days > 0 else total

    cats = {"hotel": 0.0, "transport": 0.0, "activity": 0.0, "misc": 0.0}
    hotel_nights = 0
    hotel_total = 0.0
    for e in expenses:
        cats[e["category"]] += e["amount_chf"]
        if e["category"] == "hotel":
            hotel_total += e["amount_chf"]
            hotel_nights += int(e["nights"] or 1)

    lines = [
        "📊 Stats (all)",
        f"Period: {dates[0]} → {dates[-1]} ({days} day)",
        f"Total: {swiss_money(total)} CHF",
        f"Avg/day: {swiss_money(avg_day)} CHF",
        "",
        "Breakdown (total):"
    ]
    for k in ["hotel", "transport", "activity", "misc"]:
        if abs(cats[k]) > 0.0001:
            lines.append(f"- {k}: {swiss_money(cats[k])} CHF")

    if hotel_nights > 0:
        lines += [
            "",
            f"Hotel nights: {hotel_nights} night",
            f"Hotel avg/night: {swiss_money(hotel_total / hotel_nights)} CHF"
        ]
    return "\n".join(lines)


def help_text() -> str:
    return "\n".join([
        "🤖 Travel Budget Bot (CHF main, THB secondary)",
        "",
        "Input (one line, any order):",
        "- amount: 1200 or 1200.5 (bot normalizes to 2 decimals)",
        "- optional date: DD.MM.YY or today / yesterday",
        "- optional currency: chf (default) or thb",
        "- categories:",
        "  hotel (also: bungalow, resort) + optional 'X night'",
        "  transport via: flight | ferry | bus",
        "  activity (only if you type 'activity')",
        "  misc (fallback)",
        "",
        "Examples:",
        "  1200.5 hotel 3 night koh tao",
        "  450 ferry",
        "  yesterday 60 misc coffee",
        "  11.02.26 180 bus",
        "",
        "Commands:",
        "/stats  /stats all  /today  /help",
        "",
        "Corrections (no edit/delete):",
        "Use a negative counter-entry + correct new entry (with explicit date)."
    ])


# =========================
# Smart minimal UX (Option 4)
# =========================
def format_confirmation(exp: ParsedExpense, state: Dict) -> str:
    cat_disp = exp.category + (f"/{exp.subcategory}" if exp.subcategory else "")

    saved_line = f"✅ Saved: {swiss_money(exp.amount_chf)} CHF ({cat_disp}"
    if exp.category == "hotel":
        n = exp.nights or 1
        saved_line += f", {n} night"
    saved_line += ")"

    # compute today's total after saving
    expenses = load_expenses()
    t_total = today_total_chf(expenses, date.today().isoformat())

    lines = [saved_line, f"📅 Today total: {swiss_money(t_total)} CHF"]

    # one-time minimal hint
    hints = state.get("hints_shown", {})
    if "used_default_currency" in exp.flags and not hints.get("currency_default", False):
        lines.append("ℹ️ Currency defaulted to CHF. Add 'thb' if needed.")
        hints["currency_default"] = True
    state["hints_shown"] = hints

    return "\n".join(lines)


# =========================
# Main message handling
# =========================
def handle_message(text: str, state: Dict) -> Optional[str]:
    t = text.strip()
    if not t:
        return None

    # Commands
    if t.startswith("/help"):
        return help_text()

    if t.startswith("/stats"):
        parts = t.split()
        expenses = load_expenses()
        if len(parts) >= 2 and parts[1].lower() == "all":
            return summarize_stats(expenses)
        return summarize_today(expenses, date.today().isoformat())

    if t.startswith("/today"):
        expenses = load_expenses()
        return summarize_today(expenses, date.today().isoformat())

    # /exp optional; if not present, treat as expense
    if t.startswith("/exp"):
        t = t[4:].strip()
        if not t:
            return help_text()

    exp = parse_input(t, source="telegram")
    append_expense(exp)
    return format_confirmation(exp, state)


def main():
    ensure_data_dir()
    state = load_state()
    last_update_id = int(state.get("last_update_id", 0))
    offset = last_update_id + 1

    updates = tg_get_updates(offset=offset)
    if not updates:
        return

    max_update_id = last_update_id
    for upd in updates:
        uid = upd.get("update_id", 0)
        max_update_id = max(max_update_id, uid)

        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue

        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        if chat_id != str(TELEGRAM_CHAT_ID):
            continue

        text = msg.get("text", "")
        try:
            reply = handle_message(text, state)
            if reply:
                tg_send(TELEGRAM_CHAT_ID, reply)
        except Exception as e:
            tg_send(TELEGRAM_CHAT_ID, f"⚠️ Error: {e}")

    state["last_update_id"] = max_update_id
    save_state(state)


if __name__ == "__main__":
    main()
