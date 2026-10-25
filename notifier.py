"""
UoM DAI notifier
Ελέγχει τα "Νέα του Τμήματος" Εφαρμοσμένης Πληροφορικής (ΠΑΜΑΚ)
και στέλνει μήνυμα στο Telegram όταν βγαίνει νέα ανακοίνωση.
"""

import json
import os
import re
import sys
from html import escape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://www.uom.gr"
NEWS_URL = f"{BASE}/dai/ta-nea-toy-tmhmatos"
SEEN_FILE = Path(__file__).with_name("seen.json")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (uom-notifier; student project)",
    "Accept-Language": "el-GR,el;q=0.9",
}


# ---------- 1. Κατέβασμα σελίδας ----------

def fetch_page(session: requests.Session, url: str) -> str:
    """
    Το uom.gr έχει ένα μικρό 'firewall': την πρώτη φορά σου δίνει μια σελίδα
    'System loading...' που βάζει ένα cookie (UomSiteJSID), καλεί το
    /myfirewall-ajax.php και μετά ξαναφορτώνει. Κάνουμε το ίδιο με Python.
    """
    html = session.get(url, headers=HEADERS, timeout=30).text

    match = re.search(r"UomSiteJSID=([0-9a-f]+)", html)
    if match and "post-news-body" not in html:
        session.cookies.set("UomSiteJSID", match.group(1), domain="www.uom.gr", path="/")
        session.get(f"{BASE}/myfirewall-ajax.php", headers=HEADERS, timeout=30)
        html = session.get(url, headers=HEADERS, timeout=30).text

    return html


# ---------- 2. Διάβασμα ανακοινώσεων ----------

def parse_news(html: str) -> list[dict]:
    """Βγάζει από το HTML: id, τίτλο, link και ημερομηνία κάθε ανακοίνωσης."""
    soup = BeautifulSoup(html, "html.parser")
    items = []

    for box in soup.select("div.post-news-body"):
        link = box.select_one("h6 a[href]")
        if not link:
            continue
        url = link["href"]
        id_match = re.search(r"/(\d+)-", url)
        if not id_match:
            continue

        dates = [s.get_text(strip=True) for s in box.select("ul.post-news-meta span.small")]

        items.append({
            "id": id_match.group(1),
            "title": " ".join(link.get_text().split()),
            "url": url,
            "date": dates[0] if dates else "",
        })

    return items


# ---------- 3. Τι έχουμε ήδη δει ----------

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(
        json.dumps(sorted(seen, key=int, reverse=True), indent=2),
        encoding="utf-8",
    )


# ---------- 4. Telegram ----------

def send_telegram(text: str) -> bool:
    """Στέλνει μήνυμα. Επιστρέφει True αν πέτυχε, False αν όχι."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[χωρίς Telegram] " + text.replace("\n", " | "))
        return True
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            timeout=30,
        )
    except requests.RequestException as e:
        print("Σφάλμα σύνδεσης με το Telegram:", e)
        return False

    if r.status_code == 401:
        print("ΛΑΘΟΣ TOKEN: το Telegram δεν αναγνωρίζει το TELEGRAM_TOKEN. "
              "Έλεγξε ότι το αντέγραψες ολόκληρο από το @BotFather.")
    elif r.status_code == 400 and "chat not found" in r.text:
        print("ΛΑΘΟΣ CHAT ID: έλεγξε το TELEGRAM_CHAT_ID και ότι έχεις στείλει "
              "τουλάχιστον ένα μήνυμα στο bot σου.")
    elif not r.ok:
        print(f"Σφάλμα Telegram {r.status_code}: {r.text}")
    return r.ok


def format_message(item: dict) -> str:
    return (
        f"📢 <b>Νέα ανακοίνωση ΤΕΠ</b>\n\n"
        f"{escape(item['title'])}\n\n"
        f"🗓 {escape(item['date'])}\n"
        f"🔗 {escape(item['url'])}"
    )


# ---------- main ----------

def main() -> int:
    session = requests.Session()
    html = fetch_page(session, NEWS_URL)
    news = parse_news(html)

    if not news:
        print("Δεν βρέθηκαν ανακοινώσεις. Μάλλον άλλαξε η σελίδα ή μπλόκαρε το αίτημα.")
        return 1

    seen = load_seen()
    first_run = not seen

    new_items = [n for n in news if n["id"] not in seen]

    if first_run:
        # Την πρώτη φορά δεν στέλνουμε 10 μηνύματα, απλώς τα σημειώνουμε.
        seen.update(n["id"] for n in news)
        save_seen(seen)
        print(f"Πρώτη εκτέλεση: σημείωσα {len(news)} ανακοινώσεις ως 'ήδη γνωστές'.")
        ok = send_telegram("✅ Το UoM notifier ξεκίνησε. Θα σου στέλνω τις νέες ανακοινώσεις.")
        return 0 if ok else 1

    if not new_items:
        print("Τίποτα καινούργιο.")
        return 0

    # Από την παλιότερη στη νεότερη, για να έρχονται με σωστή σειρά.
    # Μια ανακοίνωση σημειώνεται ως 'γνωστή' μόνο αν στάλθηκε, ώστε να μη χαθεί.
    all_ok = True
    for item in reversed(new_items):
        if send_telegram(format_message(item)):
            seen.add(item["id"])
            print("Στάλθηκε:", item["title"][:80])
        else:
            all_ok = False
            break

    save_seen(seen)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
