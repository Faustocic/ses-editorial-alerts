#!/usr/bin/env python3
"""
Concorsi Alert — monitor quotidiano per il ciclo editoriale "Concorsi Pubblici".

Gemello di inps_monitor.py (ciclo Evergreen Pagamenti). Sorveglia i nuovi bandi
di concorso rilevanti per Gazzetta del Sud da due canali:

  1. Google News RSS con query mirate sugli enti siciliani/calabresi e sui
     grandi concorsi nazionali (canale robusto, sempre parsabile)
  2. I sommari delle 2 uscite più recenti della GU 4ª Serie Speciale
     "Concorsi ed Esami" — esce martedì e venerdì (best effort: se il markup
     cambia, il canale 1 continua a coprire)

Canali SENZA feed pubblico, da presidiare a mano (annotati nello scadenzario):
  - inPA (inpa.gov.it): ricerca salvata con alert email personale (zero codice)
  - GURS Serie speciale concorsi: di regola l'ultimo venerdì del mese, solo
    digitale dal 2026 (gursonline.regione.sicilia.it)

Per ogni novità che matcha i filtri apre una GitHub Issue etichettata
"concorsi-alert" nel repo: GitHub manda la notifica email in automatico
(verificare di avere Watch attivo sul repo). Al primo run registra solo la
baseline, senza aprire issue.

⚠️ ATTENDIBILITÀ: le scadenze che compaiono nei sommari GU sono REDAZIONALI.
Lo dichiara la GU stessa in testa a ogni sommario: «L'unica data ufficiale è
quella contenuta nel testo del bando». Le issue riportano il caveat.

Requisiti: nessuna dipendenza esterna, solo stdlib (Python 3.10+).
Stato: seen_concorsi.json nel repo (committato dal workflow dopo ogni run).

Uso manuale / collaudo:
    python concorsi_monitor.py --dry-run

Changelog:
  14/7/2026 — v2. Parser GU riscritto sulla struttura REALE dei sommari
    (verificata sull'uscita n. 52 del 10/7/2026): la denominazione dell'ente
    è testo fuori dalle ancore, ogni atto ha DUE ancore (tipo+scadenza
    redazionale, poi titolo) verso lo stesso href; il filtro ora si applica a
    ente+tipo+titolo, altrimenti i bandi siciliani senza toponimo nel titolo
    andavano persi (caso reale: ASP di Messina, atto 26E03735). Filtro GU
    ripulito: via i generici «università» e «scuola» (flood nazionale),
    «enna» con confini di parola (altrimenti matcha «quinquennale»).
    Dedup anche per titolo normalizzato (stessa notizia su più testate).
    Retry sul fetch per gli errori transitori. Collaudato su fixture ricavate
    dalle pagine reali del 14/7/2026.
  13/7/2026 — v1, impianto derivato da inps_monitor.py.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path

# ----------------------------- CONFIG ---------------------------------------

# Query per Google News. Ogni voce genera una ricerca RSS limitata agli
# ultimi 7 giorni (when:7d). Lista da potare/estendere in base al rumore
# delle prime settimane di issue.
TERMS = [
    # --- Angolo locale: Sicilia e area dello Stretto -------------------------
    "concorso Regione Siciliana",
    "concorso comune di Messina",
    "concorso comune di Reggio Calabria",
    "concorso comune di Catania",
    "concorso comune di Palermo",
    "concorso ASP Messina",
    "concorso ASP Catania",
    "concorso ASP Palermo",
    "concorso Università di Messina",
    "concorsi pubblici Sicilia",
    "concorsi pubblici Calabria",
    # --- Nazionali ad alto interesse per il pubblico del ciclo ---------------
    "concorso scuola",
    "concorso Agenzia delle Entrate",
    "concorso INPS",
    "concorso polizia di stato",
    "concorso vigili del fuoco",
]

# Filtro di rilevanza applicato ai testi raccolti (regex, case-insensitive).
KEYWORDS = re.compile(
    r"(concors|bando|selezion[ei]|graduatori|assunzion|reclutament|"
    r"riapertura\s+dei\s+termini|rettific|proroga\s+dei\s+termini|"
    r"scorriment|stabilizzazion)",
    re.IGNORECASE,
)

# Filtro geografico/tematico per il canale GU: i sommari contengono TUTTA
# Italia, si tengono Sicilia/Calabria e i grandi enti nazionali.
# NB: niente «università» né «scuola» generici (passerebbe ogni ateneo
# d'Italia); «enna» con confini di parola, altrimenti matcha «quinquennale».
GU_FILTRO = re.compile(
    r"(sicili|messina|catania|palermo|agrigento|trapani|siracusa|ragusa|"
    r"caltanissetta|\benna\b|reggio\s+calabria|catanzaro|cosenza|crotone|"
    r"vibo\s+valentia|calabri|ministero|presidenza\s+del\s+consiglio|"
    r"agenzia\s+delle\s+entrate|\binps\b|\binail\b|polizia\s+di\s+stato|"
    r"carabinieri|guardia\s+di\s+finanza|vigili\s+del\s+fuoco|ripam|formez)",
    re.IGNORECASE,
)

GU_30GG = "https://www.gazzettaufficiale.it/30giorni/concorsi"
GU_BASE = "https://www.gazzettaufficiale.it"

STATE_FILE = Path("seen_concorsi.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; concorsi-alert/1.0; +editorial monitor)"}
MAX_ISSUES_PER_RUN = 10  # paracadute anti-flood

# ----------------------------- FETCH ----------------------------------------


def fetch(url: str, tentativi: int = 2) -> str:
    """GET con un retry sugli errori transitori (rete, 5xx). I 4xx non si
    ritentano: sono definitivi."""
    ultimo = None
    for i in range(tentativi):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            ultimo = e
        except Exception as e:
            ultimo = e
        if i + 1 < tentativi:
            time.sleep(1.5)
    raise ultimo


def google_news_items() -> list[dict]:
    """Canale 1: RSS di Google News con query mirate (ultimi 7 giorni)."""
    items = []
    for term in TERMS:
        q = urllib.parse.quote(f"{term} when:7d")
        url = f"https://news.google.com/rss/search?q={q}&hl=it&gl=IT&ceid=IT:it"
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
                items.append({"title": title, "link": link, "src": "GoogleNews",
                              "nota": ""})
    return items


# ----------------------------- CANALE GU -------------------------------------

ANCORA_ATTO = re.compile(
    r'<a[^>]+href="(?P<href>[^"]*caricaDettaglioAtto[^"]*)"[^>]*>(?P<testo>.*?)</a>',
    re.S | re.I,
)


def _pulisci(t: str) -> str:
    t = unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t).strip()


def _atti_dal_sommario(html: str) -> list[dict]:
    """Estrae gli atti da un sommario GU. Struttura reale (verificata il
    14/7/2026): la denominazione dell'ente è TESTO tra le ancore; ogni atto
    ha due ancore verso lo stesso href, la prima col tipo e la scadenza
    redazionale, la seconda col titolo."""
    atti, ente, corrente = [], "", None
    ultimo_fine = 0
    for m in ANCORA_ATTO.finditer(html):
        testo_prima = _pulisci(html[ultimo_fine:m.start()])
        ultimo_fine = m.end()
        href = unescape(m.group("href")).strip()
        testo = _pulisci(m.group("testo"))
        if len(testo_prima) > 3:
            # rubrica + denominazione: si tiene la coda, dove sta l'ente
            ente = testo_prima[-160:]
        if corrente is not None and corrente["href"] == href:
            corrente["titolo"] = re.sub(r"\s*Pag\.\s*\d+\s*$", "", testo)
            atti.append(corrente)
            corrente = None
        else:
            if corrente is not None:  # ancora orfana: si tiene comunque
                atti.append(corrente)
            corrente = {"href": href, "ente": ente, "tipo": testo, "titolo": ""}
    if corrente is not None:
        atti.append(corrente)
    return atti


def gu_items() -> list[dict]:
    """Canale 2 (best effort): sommari delle 2 uscite più recenti."""
    try:
        html = fetch(GU_30GG)
    except Exception as e:
        print(f"[warn] pagina GU 30 giorni non raggiungibile: {e}")
        return []

    uscite = {}
    for m in re.finditer(
        r'href="([^"]*gazzetta/concorsi/caricaDettaglio\?[^"]*)"', html
    ):
        href = unescape(m.group(1))
        md = re.search(r"dataPubblicazioneGazzetta=(\d{4}-\d{2}-\d{2})", href)
        mn = re.search(r"numeroGazzetta=(\d+)", href)
        if not (md and mn):
            continue
        if href.startswith("/"):
            href = GU_BASE + href
        uscite[(md.group(1), mn.group(1))] = href
    if not uscite:
        print("[warn] nessuna uscita trovata nella pagina GU 30 giorni: markup "
              "cambiato. Il canale Google News resta attivo.")
        return []

    items = []
    recenti = sorted(uscite.items(), key=lambda kv: kv[0][0], reverse=True)[:2]
    for (data, numero), url in recenti:
        try:
            somm = fetch(url)
        except Exception as e:
            print(f"[warn] sommario GU n. {numero} non raggiungibile: {e}")
            continue
        for atto in _atti_dal_sommario(somm):
            completo = f"{atto['ente']} {atto['tipo']} {atto['titolo']}"
            if not (KEYWORDS.search(completo) and GU_FILTRO.search(completo)):
                continue
            href = atto["href"]
            if href.startswith("/"):
                href = GU_BASE + href
            if not href.startswith("http"):
                continue
            items.append({
                "title": f"GU n. {numero}: {atto['ente'][-70:]} — "
                         f"{atto['tipo']} — {atto['titolo'][:120]}",
                "link": href,
                "src": f"GU 4ª serie n. {numero} del {data}",
                "nota": ("⚠️ La scadenza nel sommario è REDAZIONALE: fa fede "
                         "solo il testo del bando."),
            })
    if not items:
        print("[warn] nessun item rilevante dai sommari GU (o markup cambiato).")
    return items


# ----------------------------- STATE + ISSUES --------------------------------


def _norm_titolo(t: str) -> str:
    """Chiave secondaria di dedup: la stessa notizia ripresa da più testate
    non deve aprire più issue."""
    t = re.sub(r"[^a-z0-9àèéìíòóùú]+", " ", t.lower())
    return "t:" + re.sub(r"\s+", " ", t).strip()[:90]


def seleziona_freschi(candidates: list[dict], seen: set) -> list[tuple]:
    """Dedup per link E per titolo normalizzato. NB: a differenza di
    inps_monitor.py NON si taglia la query string del link, perché gli atti
    GU sono identificati proprio lì (codiceRedazionale)."""
    fresh, batch = [], set()
    for it in candidates:
        klink = it["link"].split("#")[0].rstrip("/")
        ktit = _norm_titolo(it["title"])
        if klink in seen or ktit in seen or klink in batch or ktit in batch:
            continue
        if not KEYWORDS.search(it["title"]):
            continue
        batch.update((klink, ktit))
        fresh.append(((klink, ktit), it))
    return fresh


def load_seen() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=1))


def open_issue(title: str, body: str, dry_run: bool = False) -> bool:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if dry_run or not token or not repo:
        print("[dry-run] Issue:", title)
        return True
    url = f"https://api.github.com/repos/{repo}/issues"
    payload = json.dumps(
        {"title": title, "body": body, "labels": ["concorsi-alert"]}
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
    p = argparse.ArgumentParser(description="Monitor dei nuovi bandi di concorso.")
    p.add_argument("--dry-run", action="store_true", help="non apre issue, stampa soltanto")
    args = p.parse_args()

    seen = load_seen()
    first_run = not seen
    candidates = google_news_items() + gu_items()
    fresh = seleziona_freschi(candidates, seen)

    if first_run:
        # Primo giro: si registra lo stato senza aprire decine di issue storiche.
        for keys, _ in fresh:
            seen.update(keys)
        if not args.dry_run:
            save_seen(seen)
        print(f"[init] baseline registrata: {len(fresh)} item, nessuna issue aperta.")
        return 0

    opened = 0
    for keys, it in fresh:
        if opened >= MAX_ISSUES_PER_RUN:
            print("[warn] raggiunto il tetto issue per run; il resto al prossimo giro.")
            break
        title = f"[{it['src'].split(' n. ')[0]}] {it['title'][:130]}"
        nota = f"\n{it['nota']}\n" if it.get("nota") else "\n"
        body = (
            f"**Fonte:** {it['src']}\n**Link:** {it['link']}\n{nota}\n"
            "Rilevata dal monitor del ciclo *Concorsi Pubblici*.\n"
            "1. Recuperare il **bando integrale** (fonte primaria) e controllare le eventuali **rettifiche**;\n"
            "2. scadenza e ORA della domanda SOLO dal testo del bando;\n"
            "3. inserire o aggiornare la riga nella **coda bandi** (scadenzario + `coda_bandi.json`);\n"
            "4. valutare scheda o reminder nella prossima sessione del Progetto."
        )
        if open_issue(title, body, dry_run=args.dry_run):
            if not args.dry_run:
                seen.update(keys)
            opened += 1

    if not args.dry_run:
        save_seen(seen)
    print(f"[done] nuovi item: {len(fresh)} — issue aperte: {opened}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
