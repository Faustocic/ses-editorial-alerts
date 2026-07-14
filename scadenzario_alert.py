#!/usr/bin/env python3
"""
Scadenzario Alert — alert di calendario per il ciclo editoriale "Evergreen Pagamenti".

Gemello di inps_monitor.py. Mentre quello sorveglia le NOTIZIE INPS, questo sorveglia
il CALENDARIO: ogni giorno controlla quali finestre editoriali si aprono oggi o entro
il preavviso, e apre una GitHub Issue con etichetta "scadenzario".

Perché esiste: fino al 13/7/2026 il file non era nella root del repo. Il workflow lo
cercava, non lo trovava, stampava il warning e saltava lo step. Risultato: nessun alert
di finestra editoriale è mai stato generato, e il pezzo #21 (ADI rinnovo) è arrivato a
ridosso della sua finestra in silenzio.

Regole implementate:
  - una sola issue per pezzo per mese-target (dedup su seen_scadenzario.json);
  - preavviso di 2 giorni sull'apertura della finestra;
  - le finestre della sessione A (28-30) riguardano il mese SUCCESSIVO (offset +1);
  - l'hub (#1) ha ordine 99: nelle issue è sempre marcato "SI SCRIVE PER ULTIMO".

Requisiti: nessuna dipendenza esterna, solo stdlib (Python 3.10+).
Stato: seen_scadenzario.json nel repo (committato dal workflow dopo ogni run).

Uso manuale / collaudo:
    python scadenzario_alert.py --dry-run
    python scadenzario_alert.py --data 2026-07-26 --dry-run
"""

import argparse
import calendar
import json
import os
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

# ----------------------------- CONFIG ---------------------------------------

PREAVVISO = 2                      # giorni di anticipo sull'apertura della finestra
STATE_FILE = Path("seen_scadenzario.json")
MAX_ISSUES_PER_RUN = 12            # paracadute anti-flood (il picco reale è 8: il 30 del mese)
UA = {"User-Agent": "Mozilla/5.0 (compatible; scadenzario-alert/1.0; +editorial calendar)"}

MESI = [
    "", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
]

# ----------------------------- LE FINESTRE -----------------------------------
# Ogni voce descrive UNA finestra editoriale.
#   giorni  : (primo, ultimo) giorno del mese in cui la finestra è aperta
#   offset  : 0 = il pezzo riguarda il mese della finestra
#             1 = il pezzo riguarda il mese SUCCESSIVO (sessione A e cedolino)
#   mesi    : None = tutti i mesi; altrimenti lista dei mesi in cui la finestra esiste
#   da      : (anno, mese) da cui il pezzo entra nel ciclo — None = già attivo
#   ordine  : ordine di lavorazione nella sessione (l'hub è 99: sempre per ultimo)
# Fonte: scadenzario-editoriale-2026.md v5. Se cambia lo scadenzario, cambia QUI.

FINESTRE = [
    # --- Ciclo mensile core -------------------------------------------------
    {
        "id": "3", "nome": "Cedolino pensione", "sessione": "A (anticipata)",
        "giorni": (20, 25), "offset": 1, "mesi": None, "da": None, "ordine": 10,
        "verificare": (
            "Voci del cedolino, trattenute, conguagli, comunicazioni INPS. "
            "Effetto delle voci una tantum del mese precedente (a luglio: quattordicesima). "
            "Il cedolino online esce di norma il 19-20. "
            "GRUPPO B DEL TEST URL: nuova URL ogni mese, slug con il mese, datePublished nuovo."
        ),
    },
    {
        "id": "9", "nome": "Scadenze fiscali", "sessione": "A",
        "giorni": (27, 30), "offset": 1, "mesi": None, "da": None, "ordine": 20,
        "verificare": "F24 del 16, IVA, ritenute, rate rottamazione, acconti (giu/nov), IMU (16/6 e 16/12).",
    },
    {
        "id": "2", "nome": "Pensioni", "sessione": "A",
        "giorni": (28, 30), "offset": 1, "mesi": None, "da": None, "ordine": 30,
        "verificare": (
            "Primo giorno bancabile: CALCOLARLO, mai a memoria. Eventi del mese: "
            "quattordicesima (lug), conguagli 730 (ago-nov), acconti (nov), "
            "tredicesima e rivalutazione (dic-gen)."
        ),
    },
    {
        "id": "1", "nome": "Pagamenti INPS (hub)", "sessione": "A",
        "giorni": (28, 30), "offset": 1, "mesi": None, "da": None, "ordine": 99,
        "verificare": (
            "Tutte le date del mese: pensioni, AUU, NASpI, ADI, SFL, Carta Acquisti (mesi dispari), "
            "eventi speciali. ⚠️ L'HUB SI SCRIVE SEMPRE PER ULTIMO, dopo i verticali."
        ),
    },
    {
        "id": "4", "nome": "NASpI", "sessione": "B",
        "giorni": (1, 3), "offset": 0, "mesi": None, "da": None, "ordine": 10,
        "verificare": (
            "Finestra ordinaria (storico 9-15), nuovi percettori (15-25). Competenza = mese precedente. "
            "NON esiste dipendenza dalla «sede territoriale competente»."
        ),
    },
    {
        "id": "5", "nome": "Assegno unico", "sessione": "B",
        "giorni": (1, 3), "offset": 0, "mesi": None, "da": None, "ordine": 20,
        "verificare": (
            "Le due date per le prestazioni in corso (storico ~17-21: CALCOLARE i giorni della settimana). "
            "Nuove domande e variazioni: ultima settimana. Regole ISEE del periodo."
        ),
    },
    {
        "id": "6", "nome": "ADI", "sessione": "B",
        "giorni": (1, 3), "offset": 0, "mesi": None, "da": None, "ordine": 30,
        "verificare": (
            "Data nuove domande/arretrati/prima mensilità di rinnovo al 50% (~15) e ricarica ordinaria "
            "(~26-28). Possibili anticipi di 1 giorno."
        ),
    },
    {
        "id": "8", "nome": "SFL", "sessione": "B",
        "giorni": (1, 3), "offset": 0, "mesi": None, "da": None, "ordine": 40,
        "verificare": "Gemello ADI: nuove domande ~15, erogazioni successive a fine mese. 500 €/mese.",
    },
    {
        "id": "21", "nome": "ADI: rinnovo e prima mensilità al 50%", "sessione": "C",
        "giorni": (10, 12), "offset": 0, "mesi": None, "da": None, "ordine": 5,
        "verificare": (
            "DEVE uscire PRIMA della ricarica del ~15, quando i nuclei rinnovati incassano il 50% "
            "e parte la query «perché mi hanno dato la metà». Nodi: art. 1 c. 158 L. 199/2025; "
            "niente più mese di sospensione; falso «bonus ponte» da 500 €; PAD solo se il nucleo è variato."
        ),
    },
    {
        "id": "C", "nome": "Refresh di metà mese dell'hub + controllo slittamenti", "sessione": "C",
        "giorni": (10, 14), "offset": 0, "mesi": None, "da": None, "ordine": 90,
        "verificare": (
            "L'intent cambia: da «quando arriva» a «non mi è arrivato». Convertire le misure già pagate "
            "sull'intent di verifica. Sostituire le stime con le date esatte. Cercare la misura su cui le "
            "nazionali stanno sbagliando. Solo dateModified, mai datePublished (Gruppo A)."
        ),
    },
    {
        "id": "7", "nome": "Stipendi NoiPA", "sessione": "C",
        "giorni": (10, 14), "offset": 0, "mesi": None, "da": (2026, 9), "ordine": 50,
        "verificare": "Data di esigibilità, emissioni speciali, arretrati, supplenze.",
    },
    {
        "id": "D", "nome": "Sessione D — revisione Search Console", "sessione": "D",
        "giorni": (5, 5), "offset": 0, "mesi": None, "da": (2026, 9), "ordine": 10,
        "verificare": (
            "15 minuti. Protocollo in protocollo-misurazione-gsc.md: soglie boost/fix/kill, verbo "
            "dominante dei titoli, avanzamento del test URL, registro."
        ),
    },

    # --- Stagionali ---------------------------------------------------------
    {
        "id": "10", "nome": "Rimborsi 730", "sessione": "stagionale",
        "giorni": (1, 5), "offset": 0, "mesi": [8, 9, 10, 11], "da": None, "ordine": 60,
        "verificare": (
            "Focus sullo scaglione corrente. Per i pensionati il conguaglio parte dal cedolino di agosto, "
            "mai prima. A novembre: taglio «senza sostituto d'imposta»."
        ),
    },
    {
        "id": "11a", "nome": "Quattordicesima pensionati (uscita di luglio)", "sessione": "stagionale",
        "giorni": (25, 30), "offset": 1, "mesi": [6], "da": None, "ordine": 60,
        "verificare": "Attendere il messaggio INPS annuale (2026: n. 2052 del 19/6). Soglie e importi nei parametri.",
    },
    {
        "id": "11b", "nome": "Quattordicesima pensionati (richiamo di dicembre)", "sessione": "stagionale",
        "giorni": (20, 25), "offset": 1, "mesi": [11], "da": None, "ordine": 60,
        "verificare": "Chi matura i requisiti dopo il 31/7 la riceve con la rata di dicembre, in dodicesimi.",
    },
    {
        "id": "12", "nome": "Tredicesima pensionati", "sessione": "stagionale",
        "giorni": (24, 30), "offset": 1, "mesi": [11], "da": None, "ordine": 60,
        "verificare": "Titolo già definito nello scadenzario. Date di dicembre + importi + tassazione.",
    },
    {
        "id": "13", "nome": "Tredicesima dipendenti", "sessione": "stagionale",
        "giorni": (26, 30), "offset": 1, "mesi": [11], "da": None, "ordine": 60,
        "verificare": "Date da CCNL, calcolo, tassazione piena. Gemello del #12.",
    },
    {
        "id": "14", "nome": "Carta Dedicata a Te", "sessione": "stagionale",
        "giorni": (1, 10), "offset": 0, "mesi": [9, 10], "da": None, "ordine": 60,
        "verificare": "Decreto + liste. ANGOLO LOCALE: graduatorie dei comuni messinesi e siciliani.",
    },
    {
        "id": "15", "nome": "Disoccupazione agricola", "sessione": "stagionale",
        "giorni": (1, 10), "offset": 0, "mesi": [1, 2, 3], "da": None, "ordine": 60,
        "verificare": "Domande entro il 31/3; pagamenti mar-giu. Alto valore territoriale.",
    },
    {
        "id": "20", "nome": "Incentivo stabilizzazione under 35 (reminder finale)", "sessione": "stagionale",
        "giorni": (1, 10), "offset": 0, "mesi": [11, 12], "da": None, "ordine": 60,
        "verificare": "Trasformazioni entro giovedì 31/12/2026, fondi limitati (18,2 mln 2026). Solo 2026.",
    },
    {
        "id": "17", "nome": "Calendario pensioni [anno] mese per mese", "sessione": "annuale",
        "giorni": (10, 20), "offset": 0, "mesi": [12], "da": None, "ordine": 70,
        "verificare": "CALCOLARE i giorni bancabili dei 12 mesi. Query fortissima di gennaio.",
    },
    {
        "id": "18", "nome": "ISEE [anno]: documenti e scadenze", "sessione": "annuale",
        "giorni": (10, 20), "offset": 0, "mesi": [12], "da": None, "ordine": 70,
        "verificare": (
            "Alimenta AUU e ADI con link interni. Novità 2026: art. 1 c. 208 L. 199/2025 "
            "(franchigia prima casa, scala di equivalenza). Richiami al 28/2 e al 30/6."
        ),
    },
    {
        "id": "19", "nome": "Importi rivalutati [anno]", "sessione": "annuale",
        "giorni": (5, 15), "offset": 0, "mesi": [1], "da": None, "ordine": 70,
        "verificare": "Alle circolari INPS di rivalutazione. Aggiornare ANCHE dati-parametri-2026.md.",
    },
]

# ----------------------------- LOGICA ----------------------------------------


def clamp_giorno(anno: int, mese: int, giorno: int) -> int:
    """Il 30 non esiste a febbraio: si taglia all'ultimo giorno reale del mese."""
    ultimo = calendar.monthrange(anno, mese)[1]
    return min(giorno, ultimo)


def mese_successivo(anno: int, mese: int) -> tuple[int, int]:
    return (anno + 1, 1) if mese == 12 else (anno, mese + 1)


def attivo(f: dict, anno: int, mese: int) -> bool:
    """Il pezzo è già entrato nel ciclo a questa data?"""
    if f["da"] is None:
        return True
    a_anno, a_mese = f["da"]
    return (anno, mese) >= (a_anno, a_mese)


def finestre_in_apertura(oggi: date) -> list[dict]:
    """Finestre aperte oggi o che si aprono entro il preavviso."""
    candidati = [(oggi.year, oggi.month), mese_successivo(oggi.year, oggi.month)]
    trovate = []

    for f in FINESTRE:
        for anno, mese in candidati:
            if f["mesi"] is not None and mese not in f["mesi"]:
                continue
            if not attivo(f, anno, mese):
                continue

            g_inizio, g_fine = f["giorni"]
            inizio = date(anno, mese, clamp_giorno(anno, mese, g_inizio))
            fine = date(anno, mese, clamp_giorno(anno, mese, g_fine))

            if not (inizio - timedelta(days=PREAVVISO) <= oggi <= fine):
                continue

            # mese di cui parla il pezzo (offset 1 = sessione A: si scrive il mese prima)
            t_anno, t_mese = (mese_successivo(anno, mese) if f["offset"] else (anno, mese))

            trovate.append({
                **f,
                "chiave": f"{t_anno}-{t_mese:02d}-{f['id']}",
                "target": f"{MESI[t_mese]} {t_anno}",
                "inizio": inizio,
                "fine": fine,
                "aperta": inizio <= oggi,
            })
            break  # una sola occorrenza per pezzo per run

    trovate.sort(key=lambda x: (x["inizio"], x["ordine"]))
    return trovate


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
        print(f"[dry-run] Issue: {title}")
        print(f"          {body.splitlines()[0]}")
        return True
    url = f"https://api.github.com/repos/{repo}/issues"
    payload = json.dumps(
        {"title": title, "body": body, "labels": ["scadenzario"]}
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


def componi_issue(f: dict, oggi: date) -> tuple[str, str]:
    quando = "APERTA" if f["aperta"] else f"si apre tra {(f['inizio'] - oggi).days} giorni"
    titolo = f"[Scadenzario] #{f['id']} {f['nome']} — {f['target']}"
    corpo = (
        f"**Finestra:** {f['inizio'].strftime('%d/%m')} → {f['fine'].strftime('%d/%m')} "
        f"({quando}) · **Sessione {f['sessione']}**\n"
        f"**Pezzo:** {f['nome']} — edizione **{f['target']}**\n\n"
        f"**Da verificare:**\n> {f['verificare']}\n\n"
        "---\n"
        "**Prima di scrivere:**\n"
        "1. partire dal master HTML in knowledge (o dal template di categoria se il pezzo è nuovo);\n"
        "2. verificare le date sulla fonte primaria (messaggi/news INPS, AE, NoiPA), poi su almeno "
        "2 fonti professionali. Gerarchia nei conflitti: primaria > CAF/patronati/consulenti > "
        "testate nazionali > siti SEO;\n"
        "3. i giorni della settimana si CALCOLANO dalla data corrente, mai a memoria;\n"
        "4. norme citate solo se verificate fino al comma;\n"
        "5. senza fonte primaria la stima va dichiarata («in base alle tempistiche ordinarie»).\n\n"
        "**Dopo la pubblicazione:** sostituire il master in knowledge, aggiornare lo scadenzario e "
        "**chiudere questa issue**.\n\n"
        "_Alert generato da `scadenzario_alert.py` sulla base di scadenzario-editoriale-2026.md._"
    )
    return titolo, corpo


# ----------------------------- MAIN ------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description="Alert delle finestre editoriali.")
    p.add_argument("--dry-run", action="store_true", help="non apre issue, stampa soltanto")
    p.add_argument("--data", help="simula una data diversa da oggi (YYYY-MM-DD)")
    args = p.parse_args()

    oggi = date.fromisoformat(args.data) if args.data else date.today()
    print(f"[info] scadenzario_alert — data di riferimento: {oggi.isoformat()} "
          f"({['lunedì','martedì','mercoledì','giovedì','venerdì','sabato','domenica'][oggi.weekday()]})")

    seen = load_seen()
    finestre = finestre_in_apertura(oggi)

    if not finestre:
        print("[done] nessuna finestra editoriale in apertura.")
        return 0

    nuove = [f for f in finestre if f["chiave"] not in seen]
    print(f"[info] finestre in apertura: {len(finestre)} — nuove: {len(nuove)}")

    aperte = 0
    for f in nuove:
        if aperte >= MAX_ISSUES_PER_RUN:
            print("[warn] raggiunto il tetto issue per run; il resto al prossimo giro.")
            break
        titolo, corpo = componi_issue(f, oggi)
        if open_issue(titolo, corpo, dry_run=args.dry_run):
            if not args.dry_run:
                seen.add(f["chiave"])
            aperte += 1

    if not args.dry_run:
        save_seen(seen)
    print(f"[done] issue aperte: {aperte}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
