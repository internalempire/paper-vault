"""JSON intermedio → nota markdown.

Il rendering è deliberatamente separato dall'estrazione: il criterio di accettazione 5 della
Fase 1 chiede che il JSON basti a rigenerare la nota senza richiamare l'API, e la Fase 5 ci
costruirà sopra le note di sintesi multi-paper. Il layout quindi cambierà; il JSON no.

I template stanno in `templates/` e riproducono la struttura dei golden. Dove i golden e il
prompt v3 divergono, **vince il golden**: sono il corpus di regressione (HANDOVER §2).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

import jinja2

from src.metadata import Metadati
from src.schema import valida

PERCORSO_TEMPLATE = Path(__file__).resolve().parents[1] / "templates"

TEMPLATE_PER_TIPO = {
    "narrative review": "review.md.j2",
    "linea guida": "linea-guida.md.j2",
    # Un documento fuori categoria usa il template review, adattandolo (Passo 1 del prompt).
    "altro": "review.md.j2",
}

# Le due classi di raccomandazione negativa, nell'ordine in cui compaiono nella vista: prima
# ciò che fa male, poi ciò che è soltanto inutile. La distinzione è clinica, non formale, e la
# sola classe numerica non la rende evidente a distanza di mesi.
CATEGORIE_NEGATIVE = (
    ("danno", "`3: Harm` — rischio > beneficio", "Interventi **dannosi**, non semplicemente inutili."),
    (
        "nessun beneficio",
        "`3: No Benefit` — beneficio = rischio",
        "Interventi **inutili**: non aggiungono nulla, ma non sono di per sé dannosi.",
    ),
    (None, "Sconsigliati", "Raccomandazioni contro un intervento, senza distinzione di classe."),
)

_INTESTAZIONE_RIFERIMENTI = {"pagina": "p.", "sezione": "§", "intestazione": "Rif."}


def render(
    nota: dict[str, Any],
    metadati: Metadati,
    *,
    pdf: str,
    data_nota: dt.date | str | None = None,
) -> str:
    """La nota markdown completa, frontmatter incluso.

    `metadati` viene da CrossRef o PubMed, mai dal modello. `pdf` è il percorso relativo del
    PDF archiviato, che il frontmatter linka.
    """
    valida(nota)
    ambiente = _ambiente()
    template = ambiente.get_template(TEMPLATE_PER_TIPO[nota["meta"]["tipo"]])
    testo = template.render(
        nota=nota,
        metadati=metadati,
        pdf=pdf,
        data_nota=data_nota or dt.date.today().isoformat(),
    )
    return _ripulisci(testo)


def _ambiente() -> jinja2.Environment:
    ambiente = jinja2.Environment(
        loader=jinja2.FileSystemLoader(PERCORSO_TEMPLATE),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,  # l'uscita è markdown, non HTML
        undefined=jinja2.ChainableUndefined,
    )
    # L'applicazione del vault riconosce i tag solo come `#cancelletto` nel corpo della nota.
    ambiente.filters["etichetta"] = lambda t: f"#{t}"
    ambiente.globals.update(
        tutti_i_tag=tutti_i_tag,
        colonna_riferimenti=colonna_riferimenti,
        senza_prefisso_pagina=senza_prefisso_pagina,
        coda_riferimento=coda_riferimento,
        senza_riferimento_ripetuto=senza_riferimento_ripetuto,
        raggruppa_per_tema=raggruppa_per_tema,
        raggruppa_negative=raggruppa_negative,
        pulisci_riferimenti=pulisci_riferimenti,
        mostra_riferimenti=MOSTRA_RIFERIMENTI,
    )
    return ambiente


# --------------------------------------------------------------------------------------
# Funzioni a disposizione dei template
# --------------------------------------------------------------------------------------


def tutti_i_tag(meta: dict[str, Any]) -> list[str]:
    """I tag del vocabolario più quelli proposti, marcati con l'asterisco da validare."""
    nuovi = [f"{t.rstrip('*')}*" for t in meta.get("tags_nuovi") or []]
    return list(meta.get("tags") or []) + nuovi


def colonna_riferimenti(criterio: str) -> str:
    return _INTESTAZIONE_RIFERIMENTI.get(criterio, "Rif.")


# I riferimenti alla fonte non vengono più mostrati nella nota: scelta dell'utente, che li
# trova ridondanti e appesantiscono la lettura. **Restano nel JSON**, in `dosaggi[].rif`,
# `sezioni[].rif`, `raccomandazioni[].rif` e dentro i campi markdown: rimetterli è una modifica
# a questo modulo e una rigenerazione con `python -m src.render`, senza chiamate all'API.
MOSTRA_RIFERIMENTI = False

# Un numero di pagina, anche a intervallo e anche con il prefisso ripetuto: `303`, `302-303`,
# `e977`, `e986-e988`.
_PAGINA = r"[A-Za-z]{0,2}\d+(?:\s*[-–]\s*[A-Za-z]{0,2}\d+)*"

# Un rimando alla fonte: `p. 303`, `pp. e986-e988`, `§3.4`, `Tab. 2`, `Fig. 1`, `Box 1`.
_RE_RIFERIMENTO = re.compile(
    rf"^(?:p{{1,2}}\.\s*{_PAGINA}"
    r"|§\s*[\d.]+"
    r"|(?:Tab|Fig|Tabella|Figura|Box)\.?\s*\d+)$",
    re.IGNORECASE,
)
_RE_NUMERO_NUDO = re.compile(rf"^{_PAGINA}$")
_RE_PARENTESI_FINALE = re.compile(r"\s*\(([^()]{1,80})\)")
_RE_RIFERIMENTO_IN_CODA = re.compile(
    rf"\s*[,;]?\s*(?:p{{1,2}}\.\s*{_PAGINA}|§\s*[\d.]+)(?=\s*(?:[—–-]\s|$))",
    re.IGNORECASE,
)

_RE_PREFISSO_PAGINA = re.compile(r"^p{1,2}\.\s*")
_RE_CODA_FRA_PARENTESI = re.compile(r"\s*\(([^()]*)\)\s*$")


def senza_prefisso_pagina(rif: str | None) -> str:
    """Toglie il `p. ` iniziale nella tabella dosaggi, dove l'intestazione lo dice già.

    I riferimenti per sezione conservano invece il `§`, come nei golden: lì il simbolo fa
    parte del numero, non è un'etichetta.
    """
    if not rif:
        return ""
    return _RE_PREFISSO_PAGINA.sub("", rif)


def e_riferimento(contenuto: str) -> bool:
    """Vero se il contenuto di una parentesi è **solo** un rimando alla fonte.

    Si controlla pezzo per pezzo: `p. 305-306, Fig. 3` è un rimando, `p. 8 e non oltre` no.
    Una parentesi che contiene anche del testo resta dov'è: toglierla porterebbe via contenuto.
    """
    pezzi = [p.strip() for p in re.split(r"[,;·]| e ", contenuto) if p.strip()]
    if not pezzi:
        return False
    # Un elenco di pagine ripete il prefisso solo la prima volta: `p. e980, e982`. I pezzi
    # successivi sono numeri nudi, e valgono come rimando solo se qualcuno prima ha detto
    # che si sta parlando di pagine — altrimenti `(3, 4)` sparirebbe da un testo qualunque.
    marcato = False
    for pezzo in pezzi:
        if _RE_RIFERIMENTO.match(pezzo):
            marcato = True
        elif not (marcato and _RE_NUMERO_NUDO.match(pezzo)):
            return False
    return marcato


def pulisci_riferimenti(testo: str | None) -> str:
    """Toglie dal markdown i rimandi alla fonte, lasciando intatto tutto il resto."""
    if not testo:
        return ""
    if MOSTRA_RIFERIMENTI:
        return testo
    ripulito = _RE_PARENTESI_FINALE.sub(
        lambda m: "" if e_riferimento(m.group(1)) else m.group(0), testo
    )
    # I rimandi che non stanno fra parentesi: la coda di una didascalia, `Tabella 7, p. e1012`.
    ripulito = _RE_RIFERIMENTO_IN_CODA.sub("", ripulito)
    ripulito = re.sub(r" +([.,;:])", r"\1", ripulito)
    return re.sub(r"[ \t]{2,}", " ", ripulito).strip()


def senza_riferimento_ripetuto(testo: str, rif: str | None) -> str:
    """Toglie dal testo il rimando finale quando la colonna accanto lo riporta già.

    Il modello tende a chiudere ogni valore con `(p. 8)` anche quando il riferimento è un campo
    a sé. Nella tabella dosaggi il risultato è `… in 2-5 minuti (p. 8) | 8` su ogni riga: il
    doppione occupa la colonna più larga e allontana il valore dall'occhio, che è il contrario
    di quello per cui la tabella esiste.
    """
    if not MOSTRA_RIFERIMENTI:
        return pulisci_riferimenti(testo)
    if not testo or not rif:
        return testo or ""
    atteso = _RE_PREFISSO_PAGINA.sub("", rif).strip()
    return _RE_CODA_FRA_PARENTESI.sub(
        lambda m: "" if _RE_PREFISSO_PAGINA.sub("", m.group(1)).strip() == atteso else m.group(0),
        testo,
    ).strip()


def coda_riferimento(testo: str, rif: str | None) -> str:
    """Il riferimento fra parentesi da appendere, o niente se è già nel testo.

    Il modello a volte mette il rimando **anche** dentro la voce, oltre che nel campo `rif`.
    Appenderlo comunque produce `(p. 306) (p. 306)`, che sembra un errore di trascrizione
    proprio nel punto in cui la nota chiede di essere creduta.
    """
    if not MOSTRA_RIFERIMENTI or not rif or rif in (testo or ""):
        return ""
    return f" ({rif})"


def raggruppa_per_tema(
    raccomandazioni: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Le raccomandazioni consecutive che condividono il tema diventano una sottosezione.

    Il raggruppamento è per adiacenza, non per valore: l'ordine dell'elenco è quello in cui il
    documento le presenta, e riordinarlo per accorpare temi lontani cambierebbe la lettura.
    """
    gruppi: list[tuple[str, list[dict[str, Any]]]] = []
    for r in raccomandazioni:
        if gruppi and gruppi[-1][0] == r["tema"]:
            gruppi[-1][1].append(r)
        else:
            gruppi.append((r["tema"], [r]))
    return gruppi


def raggruppa_negative(
    negative: list[dict[str, Any]],
) -> list[tuple[str, str, list[dict[str, Any]]]]:
    """Le raccomandazioni negative divise per categoria, nell'ordine di `CATEGORIE_NEGATIVE`.

    Le categorie vuote spariscono. Se il sistema di grading non distingue danno e assenza di
    beneficio, resta il solo gruppo generico e la vista non inventa una distinzione che la
    fonte non fa.
    """
    gruppi = []
    for categoria, titolo, spiegazione in CATEGORIE_NEGATIVE:
        voci = [r for r in negative if (r.get("categoria_negativa") or None) == categoria]
        if voci:
            gruppi.append((titolo, spiegazione, voci))
    return gruppi


# --------------------------------------------------------------------------------------
# Pulizia finale
# --------------------------------------------------------------------------------------


def _ripulisci(testo: str) -> str:
    """Normalizza la spaziatura che i blocchi Jinja lasciano dietro di sé.

    Scrivere template markdown col controllo degli spazi bianco per bianco li rende
    illeggibili. Meglio lasciarli respirare e normalizzare qui: una riga vuota fra i blocchi,
    nessuno spazio in coda, un solo a capo finale.
    """
    testo = "\n".join(riga.rstrip() for riga in testo.splitlines())
    testo = re.sub(r"\n{3,}", "\n\n", testo)
    return testo.strip() + "\n"


# --------------------------------------------------------------------------------------
# Il JSON su disco
# --------------------------------------------------------------------------------------
#
# Il criterio di accettazione 5 chiede che il JSON intermedio basti a rigenerare il markdown
# senza richiamare l'API. Non basta però salvare l'uscita del modello: mancherebbero i
# metadati bibliografici, che vengono da CrossRef e che lo schema vieta al modello di
# produrre. Il file su disco è quindi un involucro — l'uscita del modello, più ciò che la
# pipeline sa — e resta l'uscita del modello a essere validata contro lo schema.


def involucro(
    nota: dict[str, Any], metadati: Metadati, *, pdf: str, data_nota: dt.date | str | None = None
) -> dict[str, Any]:
    return {
        "nota": valida(nota),
        "metadati": dataclasses.asdict(metadati),
        "pdf": pdf,
        "data_nota": str(data_nota or dt.date.today().isoformat()),
    }


def salva(percorso: str | Path, contenuto: dict[str, Any]) -> Path:
    percorso = Path(percorso)
    percorso.parent.mkdir(parents=True, exist_ok=True)
    percorso.write_text(
        json.dumps(contenuto, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return percorso


def render_da_file(percorso: str | Path) -> str:
    """Rigenera la nota da un JSON salvato, senza toccare l'API né il PDF."""
    contenuto = json.loads(Path(percorso).read_text(encoding="utf-8"))
    testo = render(
        contenuto["nota"],
        Metadati(**contenuto.get("metadati", {})),
        pdf=contenuto["pdf"],
        data_nota=contenuto.get("data_nota"),
    )
    from src.figure import aggiungi_al_markdown, PENDENTI

    if contenuto["nota"].get("figure_richieste") and "figure" not in contenuto:
        return testo.rstrip() + "\n\n" + PENDENTI + "\n"
    return aggiungi_al_markdown(testo, contenuto.get("figure", []))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.render",
        description="Rigenera la nota markdown da un JSON intermedio salvato. "
        "Non chiama l'API: serve a rivedere il layout senza rielaborare il documento.",
    )
    parser.add_argument("percorso", type=Path, help="JSON prodotto dalla pipeline")
    parser.add_argument("--out", type=Path, help="file .md da scrivere (default: stdout)")
    args = parser.parse_args(argv)

    markdown = render_da_file(args.percorso)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(markdown, encoding="utf-8")
        print(f"nota scritta in {args.out}", file=sys.stderr)
    else:
        print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
