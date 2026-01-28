import os, re, json, requests
from bs4 import BeautifulSoup

PRICE_LIMIT = 400.00
EUR_TO_CHF = 0.97
STATE_FILE = "state.json"

ALLOWED_SHOPS = [
    "digitec", "galaxus", "brack", "microspot",
    "amazon", "decathlon", "sportxx",
    "interdiscount", "mediamarkt"
]

UA = {"User-Agent": "Mozilla/5.0 Chrome/120"}

SOURCES = {
    "Toppreise": "https://www.toppreise.ch/productcollection/Forerunner_965-pc-s67185",
    "Idealo": "https://www.idealo.ch/preisvergleich/OffersOfProduct/203201773_-forerunner-965-garmin.html",
    "PreisRunner": "https://www.pricerunner.ch/pl/143-3200606462/Smartwatches/Garmin-Forerunner-965-preise",
    "Google": "https://www.google.com/search?q=Garmin+Forerunner+965+CHF&tbm=shop",
    "Enjoy365": "https://enjoy365.ch/alle-produkte/"
}

def telegram(msg):
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    requests.post(url, json={
        "chat_id": os.environ["TELEGRAM_CHAT_ID"],
        "text": msg,
        "disable_web_page_preview": False
    })

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen": [], "errors": []}
    return json.load(open(STATE_FILE))

def save_state(s):
    json.dump(s, open(STATE_FILE,"w"), indent=2)

def extract_prices(text):
    prices = []
    for m in re.findall(r"(CHF|EUR)\s?([0-9’'\s]+[.,][0-9]{2})", text):
        val = float(m[1].replace("’","").replace(" ","").replace(",","."))
        if m[0] == "EUR":
            val *= EUR_TO_CHF
        prices.append(val)
    return prices

def shop_allowed(text):
    t = text.lower()
    return any(s in t for s in ALLOWED_SHOPS)

def screenshot(url):
    return f"https://image.thum.io/get/width/1200/{url}"

def check_source(name, url, state):
    try:
        html = requests.get(url, headers=UA, timeout=20).text
    except Exception:
        if name not in state["errors"]:
            telegram(f"⚠️ Quelle temporär nicht erreichbar: {name}")
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
    html = requests.get(SOURCES["Enjoy365"], headers=UA, timeout=20).text
    soup = BeautifulSoup(html, "lxml")

    for a in soup.select("a[href]"):
        t = a.get_text(" ", strip=True).lower()
        if ("garmin" in t or "forerunner" in t) and a["href"] not in state["seen"]:
            state["seen"].append(a["href"])
            telegram(
                "🆕 enjoy365 – neues Angebot:\n"
                f"https://enjoy365.ch{a['href']}\n"
                f"📸 {screenshot('https://enjoy365.ch'+a['href'])}"
            )

def main():
    state = load_state()
    for n,u in SOURCES.items():
        if n == "Enjoy365": continue
        check_source(n,u,state)
    check_enjoy365(state)
    save_state(state)

if __name__ == "__main__":
    main()
