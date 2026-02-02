import os, re, json, requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

PRICE_LIMIT = 400.00
EUR_TO_CHF = 0.97
STATE_FILE = "state.json"

MIN_REASONABLE_PRICE = 100.0  # verhindert "CHF 12.00" Quatsch

ALLOWED_SHOPS = [
    "digitec", "galaxus", "brack", "microspot",
    "amazon", "decathlon", "sportxx",
    "interdiscount", "mediamarkt"
]

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

SOURCES = {
    "Toppreise": "https://www.toppreise.ch/productcollection/Forerunner_965-pc-s67185",
    "Idealo": "https://www.idealo.ch/preisvergleich/OffersOfProduct/203201773_-forerunner-965-garmin.html",
    "PreisRunner": "https://www.pricerunner.ch/pl/143-3200606462/Smartwatches/Garmin-Forerunner-965-preise",
    "Google": "https://www.google.com/search?q=Garmin+Forerunner+965+CHF&tbm=shop",
    "Enjoy365": "https://enjoy365.ch/alle-produkte/"
}

BLOCK_PATTERNS = [
    "captcha", "cloudflare", "enable javascript", "unusual traffic", "access denied"
]

def telegram(msg):
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    requests.post(url, json={
        "chat_id": os.environ["TELEGRAM_CHAT_ID"],
        "text": msg,
        "disable_web_page_preview": False
    }, timeout=20)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen": [], "errors": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)

def extract_prices(text):
    prices = []
    for cur, num in re.findall(r"(CHF|EUR)\s?([0-9’'\s]+[.,][0-9]{2})", text):
        val = float(num.replace("’","").replace(" ","").replace(",","."))
        if cur == "EUR":
            val *= EUR_TO_CHF
        if val >= MIN_REASONABLE_PRICE:
            prices.append(val)
    return prices

def shop_allowed(text):
    t = text.lower()
    return any(s in t for s in ALLOWED_SHOPS)

def screenshot(url):
    return f"https://image.thum.io/get/width/1200/{url}"

def fetch_html(name, url):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    html = r.text
    low = html.lower()
    if any(p in low for p in BLOCK_PATTERNS):
        raise RuntimeError(f"Blocked or bot page for {name}")
    return html

def check_source(name, url, state):
    try:
        html = fetch_html(name, url)
    except Exception as e:
        if name not in state["errors"]:
            telegram(f"⚠️ Quelle temporär nicht erreichbar/blocked: {name}\n{type(e).__name__}: {e}")
            state["errors"].append(name)
        return

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    if not shop_allowed(text):
        return

    prices = extract_prices(text)
    if not prices:
        return

    best = min(prices)
    if best < PRICE_LIMIT:
        msg = (
            f"🚨 DEAL ALARM 🚨\n"
            f"Garmin Forerunner 965\n"
            f"Preis: CHF {best:.2f}\n"
            f"Quelle: {name}\n"
            f"{url}\n\n"
            f"📸 {screenshot(url)}"
        )
        telegram(msg)

def check_enjoy365(state):
    base = SOURCES["Enjoy365"]
    try:
        html = fetch_html("Enjoy365", base)
    except Exception as e:
        if "Enjoy365" not in state["errors"]:
            telegram(f"⚠️ Quelle temporär nicht erreichbar/blocked: Enjoy365\n{type(e).__name__}: {e}")
            state["errors"].append("Enjoy365")
        return

    soup = BeautifulSoup(html, "lxml")

    for a in soup.select("a[href]"):
        t = a.get_text(" ", strip=True).lower()
        if "garmin" in t or "forerunner" in t:
            full = urljoin(base, a["href"])
            if full not in state["seen"]:
                state["seen"].append(full)
                telegram(
                    "🆕 enjoy365 – neues Angebot:\n"
                    f"{full}\n"
                    f"📸 {screenshot(full)}"
                )

def main():
    state = load_state()
    for n,u in SOURCES.items():
        if n == "Enjoy365":
            continue
        check_source(n, u, state)
    check_enjoy365(state)
    save_state(state)

if __name__ == "__main__":
    main()
