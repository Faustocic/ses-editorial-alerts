#!/usr/bin/env python3
"""
Concorsi Scadenze — alert di calendario per il ciclo editoriale "Concorsi Pubblici".

Gemello di scadenzario_alert.py (ciclo Evergreen Pagamenti). Mentre
concorsi_monitor.py sorveglia le FONTI (nuovi bandi), questo sorveglia il
CALENDARIO: ogni giorno legge la coda bandi in coda_bandi.json e apre una
GitHub Issue etichettata "scadenzario-concorsi" quando:

  A. si avvicina la SCADENZA DOMANDA di un bando in coda (preavviso 5 giorni)
     → è il picco reale di query: il reminder va scritto ADESSO;
  B. si avvicina la data delle PROVE, se nota (preavviso 3 giorni);
  C. la scadenza domanda è PASSATA ma lo stato della scheda è ancora
     DA SCHEDARE / SCHEDATO / REMINDER FATTO → la scheda va convertita
     d'intent (prove/graduatoria) o archiviata: mai un «come fare domanda»
     al futuro su una scadenza passata;
  D. (opzionale, spento di default) finestra dell'hub mensile K1.

Perché esiste: nel ciclo Pagamenti l'assenza dell'alert di calendario ha
fatto arrivare il pezzo #21 «a ridosso della finestra in silenzio». Qui
l'alert nasce insieme al ciclo, non dopo.

Fonte dei dati: coda_bandi.json nella root del repo. Il file si aggiorna
SEMPRE insieme alla tabella dello scadenzario (regola: file interi, mai
patch). Le scadenze nel JSON devono venire dal TESTO del bando, non dalle
date redazionali di GU o inPA.

Requisiti: nessuna dipendenza esterna, solo stdlib (Python 3.10+).
Stato: seen_concorsi_scadenze.json nel repo (committato dal workflow).

Uso manuale / collaudo:
    python concorsi_scadenze.py --dry-run
    python concorsi_scadenze.py --data 2026-08-01 --dry-run

Changelog: 14/7/2026 — v1, collaudata su matrice di casi (preavviso domanda,
giorno di scadenza, conversione post-scadenza, preavviso prove, dedup,
voce malformata, file assente).
"""

import argparse
import json
import os
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

# ----------------------------- CONFIG ---------------------------------------

PREAVVISO_DOMANDA = 5   # giorni di anticipo sul reminder di scadenza domanda
PREAVVISO_PROVE = 3     # giorni di anticipo sul pezzo prove
CODA_FILE = Path("coda_bandi.json")
STATE_FILE = Path("seen_concorsi_scadenze.json")
MAX_ISSUES_PER_RUN = 12
UA = {"User-Agent": "Mozilla/5.0 (compatible; concorsi-scadenze/1.0; +editorial calendar)"}

# Hub mensile K1 «Concorsi attivi in Sicilia»: portare a True al debutto del
# pezzo. Finché è False non genera alert.
HUB_ATTIVO = False
HUB_FINESTRA = (1, 5)   # giorni del mese

STATI_APERTI = {"DA SCHEDARE", "SCHEDATO", "REMINDER FATTO"}
GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]

# ----------------------------- CODA ------------------------------------------


def carica_coda() -> list[dict]:
    """Legge coda_bandi.json. Tollera sia {"bandi": [...]} sia una lista nuda.
    Le chiavi che iniziano con _ sono documentazione e vengono ignorate.
    Le voci malformate si scartano con warning, senza far fallire il run."""
    if not CODA_FILE.exists():
        print(f"[warn] {CODA_FILE} assente: nessun alert di calendario. "
              "Creare il file (schema documentato al suo interno).")
        return []
    try:
        dati = json.loads(CODA_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[err] {CODA_FILE} non è JSON valido: {e}")
        return []
    bandi = dati.get("bandi", []) if isinstance(dati, dict) else dati
    validi = []
    for i, b in enumerate(bandi):
        if not isinstance(b, dict):
            print(f"[warn] voce {i} scartata: non è un oggetto.")
            continue
        manca = [k for k in ("id", "ente", "scadenza_domanda") if not b.get(k)]
        if manca:
            print(f"[warn] voce {b.get('id', i)} scartata: campi mancanti {manca}.")
            continue
        try:
            b["_scad"] = date.fromisoformat(str(b["scadenza_domanda"]))
        except ValueError:
            print(f"[warn] voce {b['id']} scartata: scadenza_domanda non è "
                  "una data YYYY-MM-DD.")
            continue
        b["_prove"] = None
        if b.get("prove"):
            try:
                b["_prove"] = date.fromisoformat(str(b["prove"]))
            except ValueError:
                print(f"[warn] voce {b['id']}: campo prove ignorato "
                      "(non è YYYY-MM-DD).")
        validi.append(b)
    return validi


# ----------------------------- LOGICA ----------------------------------------


def _riga_data(d: date) -> str:
    return f"{GIORNI[d.weekday()]} {d.day}/{d.month}/{d.year}"


def alert_del_giorno(bandi: list[dict], oggi: date) -> list[dict]:
    trovati = []
    for b in bandi:
        stato = str(b.get("stato", "DA SCHEDARE")).upper().strip()
        if stato == "ARCHIVIATO":
            continue
        scad, prove = b["_scad"], b["_prove"]
        ora = f" alle ore {b['ora_scadenza']}" if b.get("ora_scadenza") else ""
        base = (
            f"**Bando:** {b['ente']} — {b.get('titolo', '')}\n"
            f"**Scadenza domanda:** {_riga_data(scad)}{ora} (dal testo del bando)\n"
            f"**Stato scheda:** {stato}\n"
            f"**Fonte primaria:** {b.get('fonte', '[MANCANTE — recuperare il bando]')}\n"
        )
        if b.get("note"):
            base += f"**Note:** {b['note']}\n"

        # A. Reminder scadenza domanda
        if scad - timedelta(days=PREAVVISO_DOMANDA) <= oggi <= scad:
            trovati.append({
                "chiave": f"{b['id']}::reminder::{scad.isoformat()}",
                "quando": scad,
                "titolo": f"[Scadenze] Domande {b['ente']} — entro "
                          f"{scad.strftime('%d/%m')}",
                "corpo": base + (
                    f"\nMancano **{(scad - oggi).days} giorni** alla chiusura "
                    "delle domande: è il picco di query, il **reminder** va "
                    "scritto adesso.\n\n"
                    "**Prima di scrivere:**\n"
                    "1. controllare le **rettifiche**: uscite GU successive, "
                    "pagina dell'ente, ricerca «[ente] rettifica OR riapertura»;\n"
                    "2. scadenza e ORA solo dal **testo del bando** (le date di "
                    "GU e inPA sono redazionali);\n"
                    "3. requisiti e riserve citati dall'**articolo del bando**.\n\n"
                    "**Dopo la pubblicazione:** stato → REMINDER FATTO in "
                    "`coda_bandi.json` E nella tabella dello scadenzario "
                    "(file interi), poi chiudere questa issue."
                ),
            })

        # B. Preavviso prove
        if prove and prove - timedelta(days=PREAVVISO_PROVE) <= oggi <= prove:
            trovati.append({
                "chiave": f"{b['id']}::prove::{prove.isoformat()}",
                "quando": prove,
                "titolo": f"[Scadenze] Prove {b['ente']} — "
                          f"{prove.strftime('%d/%m')}",
                "corpo": base + (
                    f"\nProve il **{_riga_data(prove)}**: finestra per il pezzo "
                    "su convocazioni, sedi e banca dati.\n\n"
                    "Verificare il **diario prove** sull'atto pubblicato "
                    "(GU/sito ente), mai per sentito dire. Dopo: stato → PROVE "
                    "in `coda_bandi.json` e scadenzario, chiudere l'issue."
                ),
            })

        # C. Conversione d'intent post-scadenza
        if oggi > scad and stato in STATI_APERTI:
            trovati.append({
                "chiave": f"{b['id']}::conversione::{scad.isoformat()}",
                "quando": scad,
                "titolo": f"[Scadenze] CONVERTIRE {b['ente']} — domande chiuse "
                          f"il {scad.strftime('%d/%m')}",
                "corpo": base + (
                    "\nLa scadenza è **passata** ma la scheda risulta ancora "
                    f"«{stato}». Regola: mai un «come fare domanda» al futuro.\n"
                    "→ Convertire la scheda sull'intent **prove/graduatoria** "
                    "oppure **archiviare**, aggiornando stato e nota in "
                    "`coda_bandi.json` E nella tabella dello scadenzario, poi "
                    "chiudere questa issue."
                ),
            })

    # D. Hub mensile (spento finché K1 non debutta)
    if HUB_ATTIVO and HUB_FINESTRA[0] <= oggi.day <= HUB_FINESTRA[1]:
        trovati.append({
            "chiave": f"K1::{oggi.year}-{oggi.month:02d}",
            "quando": oggi,
            "titolo": f"[Scadenze] Hub K1 — Concorsi attivi in Sicilia, "
                      f"{oggi.month}/{oggi.year}",
            "corpo": ("Finestra dell'hub mensile (giorni "
                      f"{HUB_FINESTRA[0]}-{HUB_FINESTRA[1]}). "
                      "⚠️ L'HUB SI SCRIVE SEMPRE PER ULTIMO, dopo le schede."),
        })

    trovati.sort(key=lambda x: x["quando"])
    return trovati


# ----------------------------- STATE + ISSUES --------------------------------


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
        {"title": title, "body": body, "labels": ["scadenzario-concorsi"]}
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
    p = argparse.ArgumentParser(description="Alert di calendario dei concorsi in coda.")
    p.add_argument("--dry-run", action="store_true", help="non apre issue, stampa soltanto")
    p.add_argument("--data", help="simula una data diversa da oggi (YYYY-MM-DD)")
    args = p.parse_args()

    oggi = date.fromisoformat(args.data) if args.data else date.today()
    print(f"[info] concorsi_scadenze — data di riferimento: {oggi.isoformat()} "
          f"({GIORNI[oggi.weekday()]})")

    bandi = carica_coda()
    if not bandi:
        print("[done] coda bandi vuota o assente: nessun alert.")
        return 0

    seen = load_seen()
    alert = alert_del_giorno(bandi, oggi)
    nuovi = [a for a in alert if a["chiave"] not in seen]
    print(f"[info] alert del giorno: {len(alert)} — nuovi: {len(nuovi)}")

    aperte = 0
    for a in nuovi:
        if aperte >= MAX_ISSUES_PER_RUN:
            print("[warn] raggiunto il tetto issue per run; il resto al prossimo giro.")
            break
        corpo = a["corpo"] + (
            "\n\n---\n_Alert generato da `concorsi_scadenze.py` sulla base di "
            "`coda_bandi.json`._"
        )
        if open_issue(a["titolo"], corpo, dry_run=args.dry_run):
            if not args.dry_run:
                seen.add(a["chiave"])
            aperte += 1

    if not args.dry_run:
        save_seen(seen)
    print(f"[done] issue aperte: {aperte}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
