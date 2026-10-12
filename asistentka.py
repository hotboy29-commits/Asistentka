#!/usr/bin/env python3
"""Asistentka – osobní asistentka na zdarma.

Každých 15 minut (GitHub Actions):
  * stáhne e-maily ze Seznamu (IMAP) a události z iCloud kalendáře (CalDAV),
  * roztřídí e-maily na důležité / k přečtení / reklamní (pravidla + volitelně Gemini),
  * pošle připomínky na telefon (ntfy) a v 7:00 ranní souhrn,
  * uloží vše ZAŠIFROVANĚ do docs/data.enc.json (čte to webová appka).

Tajné údaje se berou z proměnných prostředí (GitHub Secrets) – nikdy do kódu.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import email
import imaplib
import json
import os
import re
import sys
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Prague")
ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data.enc.json"
PBKDF2_ITER = 200_000          # musí být stejné jako v docs/index.html
MORNING_FROM_HOUR = 7          # ranní souhrn: od 7:00 do 11:00, jednou denně
MORNING_TO_HOUR = 11
LOOKAHEAD_H = 48               # kolik hodin dopředu sledovat události
MAIL_WINDOW_H = 24             # e-maily z posledních 24 h
REMINDER_OFFSETS = [60, 15]    # připomínky v minutách před začátkem

RECLAMA_WORDS = [
    "sleva", "slevy", "výprodej", "vyprodej", "akce", "newsletter", "promo",
    "black friday", "kupon", "odhlásit", "odhlasit", "odhlášení", "unsubscribe",
    "zľava", "zlava", "% off", "exkluzivní nabídka",
]
DULEZITE_WORDS = [
    "faktura", "invoice", "splatnost", "poptávka", "poptavka", "zakázka", "zakazka",
    "objednávka", "objednavka", "upomínka", "upominka", "smlouva", "termín", "termin",
    "reklamace", "urgentní", "urgentni", "schůzka", "schuzka", "cenová nabídka",
    "cenova nabidka", "nabídka ke", "platba", "zaplaťte", "doručení", "dodání",
]


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def log(*args) -> None:
    print(f"[{dt.datetime.now(TZ):%H:%M:%S}]", *args, flush=True)


# --------------------------------------------------------------------------
# Šifrování: AES-256-GCM, klíč z PBKDF2-SHA256. Stejný formát čte WebCrypto.
# --------------------------------------------------------------------------
def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITER)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_obj(obj: dict, passphrase: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, iv = os.urandom(16), os.urandom(12)
    plain = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    ct = AESGCM(_derive_key(passphrase, salt)).encrypt(iv, plain, None)
    return {"v": 1, "salt": _b64(salt), "iv": _b64(iv), "ct": _b64(ct)}


def decrypt_obj(blob: dict, passphrase: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _derive_key(passphrase, _unb64(blob["salt"]))
    plain = AESGCM(key).decrypt(_unb64(blob["iv"]), _unb64(blob["ct"]), None)
    return json.loads(plain.decode("utf-8"))


def load_previous(passphrase: str) -> dict:
    """Načte předchozí stav (kvůli už odeslaným připomínkám a rannímu souhrnu)."""
    if not DATA_FILE.exists():
        return {}
    try:
        return decrypt_obj(json.loads(DATA_FILE.read_text()), passphrase)
    except Exception as exc:  # špatné heslo nebo poškozený soubor
        log("Nepodařilo se načíst předchozí stav:", exc)
        return {}


# --------------------------------------------------------------------------
# Pomocné funkce pro čas
# --------------------------------------------------------------------------
def to_local(value) -> dt.datetime:
    """date / naive datetime / aware datetime -> aware datetime v Praze."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=TZ)
        return value.astimezone(TZ)
    return dt.datetime.combine(value, dt.time(0), tzinfo=TZ)


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


# --------------------------------------------------------------------------
# E-maily (Seznam, IMAP)
# --------------------------------------------------------------------------
def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _message_text(msg) -> str:
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_type() == "text/plain" and not part.get_filename():
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            return re.sub(r"\s+", " ", text).strip()
    return ""


def fetch_mail(user: str, password: str, since: dt.datetime) -> list[dict]:
    imap = imaplib.IMAP4_SSL("imap.seznam.cz", 993)
    imap.login(user, password)
    out: list[dict] = []
    try:
        imap.select("INBOX", readonly=True)  # readonly: nic se neoznačí jako přečtené
        _, data = imap.search(None, "SINCE", since.strftime("%d-%b-%Y"))
        ids = data[0].split()[-150:]
        for num in ids:
            _, msg_data = imap.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(msg_data[0][1])
            try:
                received = parsedate_to_datetime(msg.get("Date")).astimezone(TZ)
            except Exception:
                received = dt.datetime.now(TZ)
            if received < since:
                continue
            sender_name, sender_addr = parseaddr(msg.get("From", ""))
            sender_name = _decode_header_value(sender_name) or sender_addr
            headers = {k.lower(): (v or "") for k, v in msg.items()}
            out.append({
                "id": msg.get("Message-ID", num.decode()),
                "from": sender_name,
                "from_addr": sender_addr,
                "subject": _decode_header_value(msg.get("Subject", "")) or "(bez předmětu)",
                "date": received.isoformat(),
                "snippet": _message_text(msg)[:600],
                "_headers": {k: headers.get(k, "") for k in ("list-unsubscribe", "precedence")},
            })
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return out


def classify_rules(mail: dict) -> str:
    vip = [v.strip().lower() for v in env("VIP_SENDERS").split(",") if v.strip()]
    sender = (mail["from"] + " " + mail["from_addr"]).lower()
    text = f'{mail["subject"]} {mail["snippet"][:300]}'.lower()
    headers = mail.get("_headers", {})

    if any(v in sender for v in vip):
        return "dulezite"
    if any(w in text for w in DULEZITE_WORDS):
        return "dulezite"
    automated = (
        bool(headers.get("list-unsubscribe"))
        or headers.get("precedence", "").lower() in ("bulk", "list", "junk")
        or bool(re.search(r"no-?reply|newsletter|marketing|info@|news@", sender))
    )
    if automated or any(w in text for w in RECLAMA_WORDS):
        return "reklama"
    return "k_precteni"


def refine_with_gemini(mails: list[dict]) -> None:
    """Volitelně (GEMINI_API_KEY): přesnější třídění + krátké shrnutí. Bez klíče se nic nestane."""
    key = env("GEMINI_API_KEY")
    if not key or not mails:
        return
    model = env("GEMINI_MODEL", "gemini-3.8-flash")
    items = [
        {"id": m["id"], "od": m["from"], "predmet": m["subject"], "text": m["snippet"][:400]}
        for m in mails[:40]
    ]
    prompt = (
        "Jsi osobní asistentka podnikatele. Pro každý e-mail urči kategorii:\n"
        "- 'dulezite': vyžaduje reakci nebo jde o fakturu, zakázku, poptávku, termín, osobní zprávu\n"
        "- 'k_precteni': informace bez nutnosti okamžité reakce\n"
        "- 'reklama': newsletter, marketing, slevy, automatické notifikace bez významu\n"
        "Ke každému napiš 'shrnuti' česky, max 15 slov. Vrať JSON pole objektů "
        "{id, kategorie, shrnuti}.\n\nE-maily:\n" + json.dumps(items, ensure_ascii=False)
    )
    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"},
            },
            timeout=60,
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        result = {r["id"]: r for r in json.loads(text)}
    except Exception as exc:
        log("Gemini nedostupný, používám pravidla:", exc)
        return
    for m in mails:
        r = result.get(m["id"])
        if r and r.get("kategorie") in ("dulezite", "k_precteni", "reklama"):
            m["category"] = r["kategorie"]
            m["summary"] = r.get("shrnuti", "")


# --------------------------------------------------------------------------
# Kalendář (iCloud, CalDAV)
# --------------------------------------------------------------------------
def parse_ics(ics_text: str, start: dt.datetime, end: dt.datetime, cal_name: str = "") -> list[dict]:
    import icalendar

    out = []
    cal = icalendar.Calendar.from_ical(ics_text)
    for comp in cal.walk("VEVENT"):
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue
        dtstart = comp.get("DTSTART")
        if dtstart is None:
            continue
        s = to_local(dtstart.dt)
        dtend = comp.get("DTEND")
        e = to_local(dtend.dt) if dtend is not None else s
        if e < start or s > end:
            continue
        all_day = not isinstance(dtstart.dt, dt.datetime)
        out.append({
            "id": f'{comp.get("UID", "")}|{s.isoformat()}',
            "title": str(comp.get("SUMMARY", "(bez názvu)")),
            "start": s.isoformat(),
            "end": e.isoformat(),
            "location": str(comp.get("LOCATION", "")),
            "calendar": cal_name,
            "allDay": all_day,
        })
    return out


def fetch_events(user: str, password: str, start: dt.datetime, end: dt.datetime) -> list[dict]:
    import caldav

    client = caldav.DAVClient(url="https://caldav.icloud.com", username=user, password=password)
    events: list[dict] = []
    for cal in client.principal().calendars():
        try:
            results = cal.search(start=start, end=end, event=True, expand=True)
        except Exception as exc:
            log(f"Kalendář '{cal.name}' se nepodařilo načíst:", exc)
            continue
        for item in results:
            events.extend(parse_ics(item.data, start, end, cal.name or ""))
    events.sort(key=lambda e: e["start"])
    return events


# --------------------------------------------------------------------------
# Připomínky, ranní souhrn, odesílání (ntfy)
# --------------------------------------------------------------------------
def send_push(title: str, message: str, priority: int = 3) -> None:
    topic = env("NTFY_TOPIC")
    if not topic:
        log("NTFY_TOPIC není nastaven – push se neodeslal:", title)
        return
    try:
        requests.post(
            "https://ntfy.sh",
            json={"topic": topic, "title": title, "message": message, "priority": priority,
                  "tags": ["bell"]},
            timeout=20,
        ).raise_for_status()
    except Exception as exc:
        log("Push se nepodařilo odeslat:", exc)


def due_reminders(events: list[dict], now: dt.datetime, notified: dict) -> list[tuple[str, str]]:
    msgs = []
    for ev in events:
        if ev["allDay"]:
            continue
        start = parse_iso(ev["start"])
        if start <= now:
            continue
        for off in REMINDER_OFFSETS:
            key = f'{ev["id"]}|{off}'
            if key in notified:
                continue
            if now >= start - dt.timedelta(minutes=off):
                where = f' · {ev["location"]}' if ev["location"] else ""
                msgs.append((f"Za {off} min: {ev['title']}",
                             f'Začíná {start:%H:%M}{where}'))
                notified[key] = ev["start"]
    return msgs


def morning_text(now: dt.datetime, events: list[dict], mails: list[dict]) -> str:
    today = [e for e in events if parse_iso(e["start"]).date() == now.date()]
    lines = [f"Dobré ráno! Dnes máš {len(today)} událost(í)."]
    for e in today:
        when = "celý den" if e["allDay"] else parse_iso(e["start"]).strftime("%H:%M")
        where = f' ({e["location"]})' if e["location"] else ""
        lines.append(f"• {when} {e['title']}{where}")

    since = now - dt.timedelta(hours=14)
    night = [m for m in mails if parse_iso(m["date"]) >= since]
    important = [m for m in night if m.get("category") == "dulezite"]
    if important:
        lines.append(f"\nDůležité e-maily přes noc ({len(important)}):")
        for m in important:
            lines.append(f"• {m['from']}: {m['subject']}" + (f" – {m['summary']}" if m.get("summary") else ""))
    else:
        lines.append("\nŽádné důležité e-maily přes noc.")
    spam = sum(1 for m in night if m.get("category") == "reklama")
    lines.append(f"Reklamní / nedůležité e-maily přeskočeny: {spam}.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Hlavní běh
# --------------------------------------------------------------------------
def demo_data(now: dt.datetime) -> tuple[list[dict], list[dict]]:
    """Ukázková data pro otestování PWA bez přístupů."""
    events = [
        {"id": "demo1|x", "title": "Obhlídka zakázky – rekonstrukce koupelny", "start": (now + dt.timedelta(hours=1)).replace(second=0, microsecond=0).isoformat(),
         "end": (now + dt.timedelta(hours=2)).isoformat(), "location": "Vinohrady, Praha", "calendar": "Práce", "allDay": False},
        {"id": "demo2|x", "title": "Dodávka dlažby", "start": (now + dt.timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0).isoformat(),
         "end": (now + dt.timedelta(days=1)).replace(hour=9, minute=0).isoformat(), "location": "", "calendar": "Práce", "allDay": False},
    ]
    mails = [
        {"id": "m1", "from": "Jana Nováková", "from_addr": "jana@example.cz", "subject": "Poptávka – výměna oken",
         "date": (now - dt.timedelta(hours=3)).isoformat(), "snippet": "Dobrý den, potřebovala bych cenovou nabídku na 4 okna.",
         "category": "dulezite", "summary": "Poptávka na výměnu čtyř oken, chce nabídku."},
        {"id": "m2", "from": "Sleva Shop", "from_addr": "news@shop.cz", "subject": "Výprodej – až 50% sleva",
         "date": (now - dt.timedelta(hours=5)).isoformat(), "snippet": "Odhlásit odběr", "category": "reklama", "summary": ""},
        {"id": "m3", "from": "Dodavatel Stavmat", "from_addr": "info@stavmat.cz", "subject": "Potvrzení objednávky č. 2231",
         "date": (now - dt.timedelta(hours=8)).isoformat(), "snippet": "Objednávka byla přijata, dodání v úterý.",
         "category": "k_precteni", "summary": "Objednávka 2231 potvrzena, dodání v úterý."},
    ]
    return events, mails


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="vytvoří ukázková data bez přístupů")
    args = parser.parse_args()

    now = dt.datetime.now(TZ)
    passphrase = env("ASIST_PASS")
    if not passphrase:
        log("Chybí ASIST_PASS (heslo pro šifrování dat).")
        return 1

    prev = load_previous(passphrase)
    state = prev.get("state", {"notified": {}, "last_morning": "", "last_morning_text": ""})
    errors: list[str] = []
    events: list[dict] = []
    mails: list[dict] = []

    if args.demo:
        os.environ["NTFY_TOPIC"] = ""  # demo nikdy neposílá push
        events, mails = demo_data(now)
    else:
        # --- e-maily
        mail_user, mail_pass = env("MAIL_USER"), env("MAIL_PASS")
        if mail_user and mail_pass:
            try:
                raw = fetch_mail(mail_user, mail_pass, now - dt.timedelta(hours=MAIL_WINDOW_H))
                for m in raw:
                    m["category"] = classify_rules(m)
                    m["summary"] = ""
                    m.pop("_headers", None)
                refine_with_gemini(raw)
                mails = sorted(raw, key=lambda m: m["date"], reverse=True)
                log(f"E-maily: {len(mails)}")
            except Exception as exc:
                errors.append(f"E-maily: {exc}")
                mails = prev.get("emails", [])
        else:
            errors.append("E-maily: chybí MAIL_USER / MAIL_PASS")

        # --- kalendář
        cal_user, cal_pass = env("ICLOUD_USER"), env("ICLOUD_APP_PASS")
        if cal_user and cal_pass:
            try:
                events = fetch_events(cal_user, cal_pass, now - dt.timedelta(hours=1),
                                      now + dt.timedelta(hours=LOOKAHEAD_H))
                log(f"Události: {len(events)}")
            except Exception as exc:
                errors.append(f"Kalendář: {exc}")
                events = prev.get("events", [])
        else:
            errors.append("Kalendář: chybí ICLOUD_USER / ICLOUD_APP_PASS")

    # --- připomínky
    for title, body in due_reminders(events, now, state["notified"]):
        log("Připomínka:", title)
        send_push(title, body, priority=4)
    # uklidit stará data z paměti odeslaných připomínek
    cutoff = now - dt.timedelta(days=2)
    state["notified"] = {k: v for k, v in state["notified"].items()
                         if parse_iso(v) > cutoff}

    # --- ranní souhrn (jednou denně, mezi 7:00 a 11:00)
    today = now.date().isoformat()
    if MORNING_FROM_HOUR <= now.hour < MORNING_TO_HOUR and state.get("last_morning") != today:
        text = morning_text(now, events, mails)
        send_push("Ranní souhrn", text, priority=3)
        state["last_morning"] = today
        state["last_morning_text"] = text
        log("Ranní souhrn odeslán")

    data = {
        "updated": now.isoformat(),
        "events": events,
        "emails": mails,
        "reklama_count": sum(1 for m in mails if m.get("category") == "reklama"),
        "morning_text": state.get("last_morning_text", ""),
        "errors": errors,
        "state": state,
    }
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(encrypt_obj(data, passphrase), indent=2))
    log("Data uložena (šifrovaně).", "Chyby:" if errors else "", *errors)
    return 0


if __name__ == "__main__":
    sys.exit(main())
