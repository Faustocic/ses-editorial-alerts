#!/usr/bin/env python3
"""
INPS Alert — monitor quotidiano per il ciclo editoriale "Evergreen Pagamenti".

Controlla le notizie INPS rilevanti da due canali:
  1. Google News RSS con query mirate su site:inps.it (canale robusto, sempre parsabile)
  2. La pagina notizie di inps.it (best effort: se il markup cambia o è renderizzato
     in JS, il canale 1 continua a coprire)

Per ogni novità che matcha le keyword apre una GitHub Issue nel repo:
GitHub manda la notifica email in automatico (verificare di avere Watch attivo sul repo).

Requisiti: nessuna dipendenza esterna, solo stdlib (Python 3.10+).
Stato: seen_inps.json nel repo (committato dal workflow dopo ogni run).
Changelog: 7/7/2026 — aggiunte le keyword dell'incentivo stabilizzazione (circ. INPS 72/2026).
"""

import json
import os
import re
import sys
import urllib.request
import urllib.parse
from html import unescape
from pathlib import Path

# ----------------------------- CONFIG ---------------------------------------

# Termini monitorati: uno per pezzo del ciclo editoriale.
TERMS = [
    "cedolino pensione",
    "quattordicesima",
    "tredicesima",
    "assegno unico",
    "NASpI",
    "assegno di inclusione",
    "supporto per la formazione e il lavoro",
    "carta acquisti",
    "carta dedicata a te",
    "ISEE",
    "rivalutazione pensioni",
    "disoccupazione agricola",
    "730",
    "incentivo alla stabilizzazione",  # circ. INPS 72/2026 — DL 62/2026 (decreto Lavoro)
]

# Filtro di rilevanza applicato ai titoli raccolti (regex, case-insensitive).
KEYWORDS = re.compile(
    r"(cedolino|pension[ei]|quattordicesima|tredicesima|assegno\s+unico|naspi|dis[- ]?coll|"
    r"assegno\s+di\s+inclusione|supporto\s+per\s+la\s+formazione|carta\s+acquisti|"
    r"carta\s+dedicata|isee|rivalutazion|perequazion|disoccupazione\s+agricola|730|"
    r"stabilizzazion|salario\s+giusto)",
    re.IGNORECASE,
)

INPS_NEWS_PAGE = "https://www.inps.it/it/it/inps-comunica/notizie.html"
STATE_FILE = Path("seen_inps.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; inps-alert/1.0; +editorial monitor)"}
MAX_ISSUES_PER_RUN = 8  # paracadute anti-flood al primo avvio

# ----------------------------- FETCH ----------------------------------------


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def google_news_items() -> list[dict]:
    """Canale 1: RSS di Google News con query mirate su site:inps.it."""
    items = []
    for term in TERMS:
        q = urllib.parse.quote(f'"{term}" site:inps.it')
        url = (
            f"https://news.google.com/rss/search?q={q}"
            "&hl=it&gl=IT&ceid=IT:it"
        )
        try:
            xml = fetch(url)
        except Exception as e:
            print(f"[warn] Google News KO per '{term}': {e}")
            continue
        for m in re.finditer(
            r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>", xml, re.S
        ):
            title = unescape(re.sub(r"<.*?>", "", m.group(1))).strip()
            link = unescape(m.group(2)).strip()
            if title and link:
                items.append({"title": title, "link": link, "src": "GoogleNews"})
    return items


def inps_page_items() -> list[dict]:
    """Canale 2 (best effort): anchor della pagina notizie INPS."""
    try:
        html = fetch(INPS_NEWS_PAGE)
    except Exception as e:
        print(f"[warn] pagina INPS non raggiungibile: {e}")
        return []
    items = []
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        href = unescape(m.group(1)).strip()
        text = unescape(re.sub(r"<.*?>", " ", m.group(2)))
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 15:
            continue
        if not KEYWORDS.search(text):
            continue
        if href.startswith("/"):
            href = "https://www.inps.it" + href
        if not href.startswith("http"):
            continue
        items.append({"title": text, "link": href, "src": "inps.it"})
    if not items:
        print("[warn] nessun item dalla pagina INPS: markup cambiato o rendering JS. "
              "Il canale Google News resta attivo.")
    return items


# ----------------------------- STATE + ISSUES --------------------------------


def load_seen() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=1))


def open_issue(title: str, body: str) -> bool:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("[dry-run] Issue:", title)
        return True
    url = f"https://api.github.com/repos/{repo}/issues"
    payload = json.dumps(
        {"title": title, "body": body, "labels": ["inps-alert"]}
    ).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            **UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("[ok] issue creata:", json.loads(r.read()).get("html_url"))
        return True
    except Exception as e:
        print(f"[err] creazione issue fallita: {e}")
        return False


# ----------------------------- MAIN ------------------------------------------


def main() -> int:
    seen = load_seen()
    first_run = not seen
    candidates = google_news_items() + inps_page_items()

    # dedup per link normalizzato
    fresh, batch_seen = [], set()
    for it in candidates:
        key = it["link"].split("?")[0].rstrip("/")
        if key in seen or key in batch_seen:
            continue
        if not KEYWORDS.search(it["title"]):
            continue
        batch_seen.add(key)
        fresh.append((key, it))

    if first_run:
        # Primo giro: si registra lo stato senza aprire decine di issue storiche.
        for key, _ in fresh:
            seen.add(key)
        save_seen(seen)
        print(f"[init] baseline registrata: {len(fresh)} item, nessuna issue aperta.")
        return 0

    opened = 0
    for key, it in fresh:
        if opened >= MAX_ISSUES_PER_RUN:
            print("[warn] raggiunto il tetto issue per run; il resto al prossimo giro.")
            break
        title = f"INPS: {it['title'][:120]}"
        body = (
            f"**Fonte:** {it['src']}\n**Link:** {it['link']}\n\n"
            "Rilevata dal monitor del ciclo *Evergreen Pagamenti*. "
            "Verificare se impatta un pezzo in finestra (scadenzario) e, in caso, "
            "portarla come fonte primaria nella prossima sessione del Progetto Claude."
        )
        if open_issue(title, body):
            seen.add(key)
            opened += 1

    save_seen(seen)
    print(f"[done] nuovi item: {len(fresh)} — issue aperte: {opened}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
