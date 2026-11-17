import csv
import random
import re
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent   # папка, где лежит скрипт
NUMBERS_FILE = BASE_DIR / "numbers.csv"
MESSAGE_FILE = BASE_DIR / "message.txt"
LOG_FILE = BASE_DIR / "sent_log.csv"   # колонки: company, phone, status, time
PROFILE_DIR = str(BASE_DIR / "wa_profile")   # тут хранится сессия WhatsApp

MIN_DELAY, MAX_DELAY = 60, 280    # пауза после отправленного сообщения, сек
CHECK_DELAY = (5, 60)             # пауза после номера без WhatsApp, сек
DAILY_LIMIT = 30                  # максимум отправленных сообщений за запуск

COMPOSER = (
    '#main footer div[contenteditable="true"], '
    '#main div[contenteditable="true"][role="textbox"], '
    'footer div[contenteditable="true"][data-tab="10"]'
)
DIALOG = 'div[role="dialog"], [data-animate-modal-popup="true"]'
# фразы из окна «номер недействителен» (рус/англ/кырг) - только по ним ставим no_wa;
# другие окна (например «Начало чата...») игнорируются
INVALID_WORDS = (
    "недействител", "не зарегистр", "нет в whatsapp", "не использует",
    "invalid", "isn't on whatsapp", "not on whatsapp", "жараксыз",
)
GENERIC_NAMES = {"Гостиница", "Хостел", "Гостевой дом", "Отель", "Гостевые дома", "Сауна"}
CHAT_TIMEOUT = 60   # сколько ждать загрузки чата, сек

# признаки того, что вход выполнен (достаточно любого)
LOGGED_IN = (
    '#pane-side, div[role="grid"], div[contenteditable="true"][data-tab="3"], '
    '[data-icon="new-chat-outline"], [data-icon="chats-filled"], '
    '[data-icon="menu"], [data-icon="menu-outline"]'
)
QR = "div[data-ref]"   # блок с QR-кодом


def wait_for_login(page):
    """Ждёт вход. Если сессия сохранена - продолжает сразу, QR не нужен."""
    start = time.time()
    qr_seen = False
    gone_since = None
    while time.time() - start < 300:
        if page.query_selector(LOGGED_IN):
            print("Вход выполнен, продолжаю.")
            return
        if page.query_selector(QR):
            if not qr_seen:
                print("Показан QR-код: отсканируйте его телефоном (первый раз).")
            qr_seen = True
            gone_since = None
        else:
            if gone_since is None:
                gone_since = time.time()
            # QR нет и список чатов не распознан: либо загрузка, либо новая вёрстка
            limit = 8 if qr_seen else 25
            if time.time() - gone_since > limit:
                print("QR-кода нет, считаю что вход выполнен, продолжаю.")
                return
        time.sleep(1)
    raise SystemExit("Не удалось войти за 5 минут.")


def read_contacts():
    contacts = []
    with NUMBERS_FILE.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 2 or not row[0].strip():
                continue
            name = row[0].strip()
            raw = " ".join(row[1:])
            phones = []
            for part in re.split(r"[;,/]", raw):
                digits = re.sub(r"\D", "", part)  # убираем +, пробелы, скобки, дефисы
                if len(digits) >= 9 and digits not in phones:
                    phones.append(digits)
            if phones:
                contacts.append((name, phones))
    return contacts


def read_variants():
    text = MESSAGE_FILE.read_text(encoding="utf-8").strip()
    variants = [v.strip() for v in re.split(r"\n\s*---\s*\n", text) if v.strip()]
    if not variants:
        raise SystemExit("message.txt пустой")
    return variants


def read_log():
    done_companies, sent_phones, no_wa_phones, sent_count = set(), set(), set(), 0
    if LOG_FILE.exists():
        with LOG_FILE.open(encoding="utf-8", newline="") as f:
            for row in csv.reader(f):
                if len(row) < 3:
                    continue
                company, phone, status = row[0], row[1], row[2]
                if status == "sent":
                    done_companies.add(company)
                    sent_phones.add(phone)
                    sent_count += 1
                elif status == "no_wa":
                    no_wa_phones.add(phone)
    return done_companies, sent_phones, no_wa_phones, sent_count


def log(company, phone, status):
    with LOG_FILE.open("a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow([company, phone, status, time.strftime("%Y-%m-%d %H:%M:%S")])


def invalid_popup_shown(page):
    """True, если открыто именно окно «номер недействителен / нет в WhatsApp»."""
    for dlg in page.query_selector_all(DIALOG):
        try:
            txt = dlg.inner_text().lower()
        except Exception:
            continue  # окно успело закрыться
        if any(w in txt for w in INVALID_WORDS):
            return True
    return False


def try_send(page, phone, text):
    """Возвращает 'sent', 'no_wa' или 'error'."""
    url = f"https://web.whatsapp.com/send?phone={phone}&text={urllib.parse.quote(text)}"
    page.goto(url)
    start = time.time()
    while time.time() - start < CHAT_TIMEOUT:
        box = page.query_selector(COMPOSER)
        if box:
            time.sleep(random.uniform(1.5, 3))
            box.click()
            page.keyboard.press("Enter")
            time.sleep(random.uniform(2, 7))
            return "sent"
        if invalid_popup_shown(page):
            return "no_wa"
        time.sleep(1)
    return "error"  # чат не загрузился - это НЕ значит, что WhatsApp нет; проверим в следующий раз


def main():
    contacts = read_contacts()
    variants = read_variants()
    done_companies, sent_phones, no_wa_phones, sent_total = read_log()

    # общие названия («Гостиница», «Хостел»...) - это разные заведения, сверяем их по номеру
    todo = [
        (n, p) for n, p in contacts
        if not any(ph in sent_phones for ph in p)
        and (n in GENERIC_NAMES or n not in done_companies)
    ]
    print(f"Компаний всего: {len(contacts)}, осталось: {len(todo)}, вариантов текста: {len(variants)}")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://web.whatsapp.com")
        print("Проверяю вход в WhatsApp Web...")
        wait_for_login(page)

        sent_now = 0
        for name, phones in todo:
            if sent_now >= DAILY_LIMIT:
                print("Достигнут дневной лимит, останавливаюсь.")
                break

            delivered = False
            for phone in phones:
                if phone in no_wa_phones:
                    print(f"[SKIP] {name}: +{phone} уже проверен, WhatsApp нет")
                    continue

                text = variants[sent_total % len(variants)].format(name=name)
                try:
                    status = try_send(page, phone, text)
                except Exception as e:
                    print(f"[ERR]  {name} +{phone}: {e}")
                    log(name, phone, "error")
                    continue

                if status == "sent":
                    log(name, phone, "sent")
                    print(f"[OK]   {name} +{phone} (вариант {sent_total % len(variants) + 1})")
                    sent_total += 1
                    sent_now += 1
                    delivered = True
                    break
                elif status == "error":
                    log(name, phone, "error")
                    print(f"[ERR]  {name} +{phone}: чат не загрузился за {CHAT_TIMEOUT} с, пробую следующий номер")
                    time.sleep(random.uniform(*CHECK_DELAY))
                else:
                    log(name, phone, "no_wa")
                    no_wa_phones.add(phone)
                    print(f"[NO WA] {name} +{phone}, пробую следующий номер")
                    time.sleep(random.uniform(*CHECK_DELAY))

            if delivered:
                time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
            else:
                print(f"[----] {name}: ни на одном номере нет WhatsApp")

        ctx.close()


if __name__ == "__main__":
    main()