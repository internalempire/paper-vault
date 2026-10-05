"""Estrazione del testo dal documento sorgente.

Responsabilità (Fase 1, §6.1 dello HANDOVER):

- estrarre il testo con PyMuPDF, rispettando l'ordine di lettura su due colonne;
- accorgersi che il PDF è scansionato e passarlo da ``ocrmypdf`` prima di procedere;
- recuperare i bookmark con ``get_toc()``, che servono se un giorno serve il map-reduce;
- ricostruire i **numeri di pagina stampati** — non gli indici del PDF — perché è a quelli
  che la nota deve rimandare, e un numero inventato è un fallimento di accettazione;
- riprodurre le tabelle come tabelle markdown invece di lasciarle linearizzare;
- stimare i token, per sapere quando il documento singolo smetterà di entrare in una chiamata.

Accetta anche gli HTML open access: il terzo documento del corpus è in quel formato.

Uso come modulo:

    from src.extract import extract
    doc = extract("paper.pdf")
    doc.document_text()

Uso da riga di comando, per ispezionare un documento senza spendere una chiamata API:

    python -m src.extract paper.pdf --report
    python -m src.extract paper.pdf --text --out estratto.txt
"""

from __future__ import annotations

import argparse
import html.parser
import logging
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

log = logging.getLogger(__name__)

# PyMuPDF scrive i propri avvisi su stdout: dirottali sul logger, altrimenti finiscono
# dentro il testo estratto quando lo si manda in pipe.
pymupdf.set_messages(pylogging=True, pylogging_name=f"{__name__}.pymupdf")
pymupdf.no_recommend_layout()  # il suggerimento va su stdout e sporcherebbe il testo estratto

# Caratteri che in un articolo medico non compaiono mai per davvero. Quando spuntano in
# quantità significa che il PDF disegna i simboli con un font a codifica propria e che la
# corrispondenza con l'Unicode è saltata: al posto di `µ` esce `m`, al posto di `+` esce `þ`.
# È il difetto più insidioso che un PDF possa avere, perché il testo che ne esce è plausibile
# — `7-20 mm` invece di `7-20 µm` è un fattore mille che non stona a leggerlo.
_CARATTERI_IMPOSSIBILI = "þð¤˚¸˝ˆ˜¨"

# Uno o due possono essere un nome proprio o un simbolo matematico: contano quantità e densità.
_MINIMO_CARATTERI_IMPOSSIBILI = 5
_DENSITA_CARATTERI_IMPOSSIBILI = 1e-4  # uno ogni diecimila caratteri

# Sotto questa media di caratteri per pagina il PDF è, con ogni probabilità, immagini.
SOGLIA_CARATTERI_PER_PAGINA = 120

# Sotto questa larghezza la striscia bianca al centro non è una grondaia fra due colonne, ma
# lo spazio fra due celle di una tabella a piena pagina.
_LARGHEZZA_MINIMA_GRONDAIA = 4

# Oltre questa soglia una pagina non è una pagina con tabelle: è una figura vettoriale, e
# find_tables() ci perde mezzo minuto per non trovare niente. Il massimo osservato su una
# pagina con tabelle vere è ~160, quindi la soglia lascia tre ordini di grandezza di margine.
_DISEGNI_MASSIMI_PER_TABELLE = 5000

# Una griglia con meno righe di così, o troppo vuota, è un frammento di figura agganciato per
# sbaglio da find_tables(), non una tabella.
_RIGHE_MINIME_TABELLA = 3
_QUOTA_MINIMA_CELLE_PIENE = 0.5

# Ricostruzione delle tabelle stampate di traverso.
_PAROLE_MINIME_GRIGLIA = 30
_COLONNE_MINIME_GRIGLIA = 3
_LARGHEZZA_MINIMA_SEPARATORE = 3
_QUOTA_RIGHE_PER_SEPARATORE = 0.9
_SALTO_MINIMO_FRA_COLONNE = 12

# Una x attraversata da meno di questa quota delle fasce di testo è candidata a grondaia.
_QUOTA_ATTRAVERSAMENTI_GRONDAIA = 0.15

# Passo con cui si contano le fasce orizzontali di testo, in punti.
_ALTEZZA_FASCIA = 10

# Fascia di pagina in cui cercare il numero stampato, in frazione dell'altezza.
_FASCIA_INTESTAZIONE = 0.08

# Trattino morbido, eventualmente seguito dall'andata a capo che lo ha reso visibile.
_RE_TRATTINO_MORBIDO = re.compile("\u00ad[ \t]*\n?[ \t]*")

# Caratteri di controllo che certe composizioni tipografiche lasciano nel flusso di testo —
# backspace, form feed, shift out. Sono invisibili e non significano nulla, ma attaccati a un
# numero di pagina o a un DOI li rendono irriconoscibili.
_RE_CONTROLLO = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Nel margine si aggiunge il carattere di sostituzione, che compare quando il font non ha il
# glifo per ciò che il PDF chiede di disegnare. Nel corpo del testo va lasciato — segnala un
# problema di codifica che è meglio vedere — ma su un numero di pagina è solo rumore.
_RE_RUMORE_DI_MARGINE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffd]")

_RE_NUMERO_PAGINA = re.compile(r"^([A-Za-z]{0,2})[-–]?(\d{1,5})$")

# Le righe di margine sono corte: titolo corrente, rivista, numero. La prosa non lo è.
_MAX_CARATTERI_RIGA_MARGINE = 60


@dataclass
class Pagina:
    """Una pagina del documento, già ordinata per la lettura."""

    indice: int  # 0-based, indice nel PDF
    testo: str
    etichetta: str | None = None  # numero di pagina stampato, se ricostruito
    n_tabelle: int = 0

    @property
    def n_caratteri(self) -> int:
        return len(self.testo)

    @property
    def riferimento(self) -> str:
        """Come citare questa pagina: l'etichetta stampata se c'è, altrimenti l'indice."""
        return self.etichetta if self.etichetta else str(self.indice + 1)


@dataclass
class Documento:
    """Il documento estratto, pronto per essere passato al modello."""

    percorso: Path
    formato: str  # "pdf" | "html"
    pagine: list[Pagina] = field(default_factory=list)
    toc: list[tuple[int, str, int]] = field(default_factory=list)
    ocr_applicato: bool = False
    etichette_ricostruite: bool = False
    sorgente_web: dict | None = None

    @property
    def n_pagine(self) -> int:
        return len(self.pagine)

    @property
    def n_caratteri(self) -> int:
        return sum(p.n_caratteri for p in self.pagine)

    @property
    def n_tabelle(self) -> int:
        return sum(p.n_tabelle for p in self.pagine)

    @property
    def token_stimati(self) -> int:
        """Stima grossolana (~4 caratteri per token). Serve a monitorare la crescita, non a fatturare."""
        return round(self.n_caratteri / 4)

    @property
    def codifica_sospetta(self) -> bool:
        """Vero se il PDF sembra usare font simbolici mappati male sull'Unicode.

        Non è recuperabile in automatico: la corrispondenza dipende dal font e non è dichiarata
        nel documento. Si può però dirlo, ed è meglio di una nota con le unità sbagliate che
        sembra corretta.
        """
        testo = self.document_text(marcatori=False)
        if not testo:
            return False
        quanti = sum(testo.count(c) for c in _CARATTERI_IMPOSSIBILI)
        return (
            quanti >= _MINIMO_CARATTERI_IMPOSSIBILI
            and quanti / len(testo) >= _DENSITA_CARATTERI_IMPOSSIBILI
        )

    @property
    def criterio_riferimenti(self) -> str:
        """Il criterio che il prompt deve adottare per i rimandi.

        ``pagina`` solo se i numeri stampati sono stati ricostruiti davvero. Un HTML senza
        paginazione, o un PDF di cui non si è trovato il numero stampato, ricade su ``sezione``:
        meglio un rimando alla sezione che un numero di pagina inventato.
        """
        return "pagina" if self.etichette_ricostruite else "sezione"

    def document_text(self, *, marcatori: bool = True) -> str:
        """Il testo integrale, con un marcatore per pagina che àncora i rimandi."""
        if not marcatori:
            return "\n\n".join(p.testo for p in self.pagine)
        parti = []
        for p in self.pagine:
            if self.formato == "pdf":
                parti.append(f"[[pagina {p.riferimento}]]\n{p.testo}")
            else:
                parti.append(p.testo)
        return "\n\n".join(parti)

    def toc_text(self) -> str:
        return "\n".join(
            f"{'  ' * (liv - 1)}{titolo}  (pagina {pag})" for liv, titolo, pag in self.toc
        )


# --------------------------------------------------------------------------------------
# Ordinamento dei blocchi: colonne, bande, regioni ruotate
# --------------------------------------------------------------------------------------

# Le direzioni di scrittura che si incontrano, dalla più comune. Le tabelle larghe delle
# riviste sono spesso stampate di traverso su una pagina verticale: `(0, -1)` è quel caso.
_DIREZIONI = ((1.0, 0.0), (0.0, -1.0), (0.0, 1.0), (-1.0, 0.0))


def _riquadro_diritto(
    riquadro: tuple[float, float, float, float], direzione: tuple[float, float], rect: pymupdf.Rect
) -> tuple[float, float, float, float]:
    """Le coordinate di un riquadro nel frame in cui il suo testo si legge orizzontale.

    `Page.set_rotation()` cambia `page.rect` ma lascia le bbox del testo nel sistema originale,
    quindi ruotare la pagina non basta: la trasformazione va fatta qui. Senza, una tabella
    stampata di traverso viene ordinata per colonne invece che per righe, e ogni valore si
    stacca dalla riga a cui appartiene — su una tabella di dosaggi è il modo più diretto per
    attribuire una soglia alla popolazione sbagliata.
    """
    x0, y0, x1, y1 = riquadro
    larghezza, altezza = rect.width, rect.height
    if direzione == (0.0, -1.0):  # ruotato di 90° antiorario: si raddrizza girando in orario
        return (altezza - y1, x0, altezza - y0, x1)
    if direzione == (0.0, 1.0):
        return (y0, larghezza - x1, y1, larghezza - x0)
    if direzione == (-1.0, 0.0):
        return (larghezza - x1, altezza - y1, larghezza - x0, altezza - y0)
    return (x0, y0, x1, y1)


def _blocchi_per_direzione(
    pagina: pymupdf.Page,
) -> dict[tuple[float, float], list[tuple[float, float, float, float, str]]]:
    """I blocchi di testo raggruppati per direzione di scrittura, già raddrizzati.

    Ogni riga viene tenuta separata invece che unita al proprio blocco: nelle tabelle di
    traverso PyMuPDF raggruppa in un unico blocco celle che appartengono a righe diverse, e
    fondere il testo prima di riordinarlo rimescolerebbe proprio ciò che si vuole ricostruire.
    """
    rect = pagina.rect
    gruppi: dict[tuple[float, float], list[tuple]] = {}
    for indice, blocco in enumerate(pagina.get_text("dict")["blocks"]):
        if blocco.get("type") != 0:
            continue
        for riga in blocco["lines"]:
            direzione = tuple(float(round(v)) for v in riga["dir"])
            if direzione not in _DIREZIONI:
                continue
            testo = "".join(s["text"] for s in riga["spans"]).strip()
            if not testo:
                continue
            x0, y0, x1, y1 = _riquadro_diritto(riga["bbox"], direzione, rect)
            gruppi.setdefault(direzione, []).append((x0, y0, x1, y1, testo, indice))
    return gruppi


def _rect_diritto(direzione: tuple[float, float], rect: pymupdf.Rect) -> pymupdf.Rect:
    if direzione in ((0.0, -1.0), (0.0, 1.0)):
        return pymupdf.Rect(0, 0, rect.height, rect.width)
    return pymupdf.Rect(0, 0, rect.width, rect.height)


def _grondaia(righe: list[tuple], rect: pymupdf.Rect) -> float | None:
    """La x della grondaia fra due colonne, se la pagina è davvero su due colonne.

    Non basta cercare una striscia verticale libera: anche fra due colonne di una tabella
    c'è una striscia libera, e leggerla come grondaia significa spezzare la tabella a metà,
    con i valori staccati dalla riga a cui appartengono. Su una tabella di dosaggi è il modo
    più diretto per attribuire una soglia alla popolazione sbagliata.

    Il discriminante è il **numero** di strisce: un impaginato a due colonne ne ha esattamente
    una, una griglia ne ha una per confine di colonna. Le strisce si cercano contando quante
    righe di testo attraversano ogni x — non richiedendo che nessuna lo faccia — altrimenti un
    titolo che scavalca le colonne cancella la grondaia sotto di sé.
    """
    if len(righe) < 4:
        return None

    larghezza = int(rect.width) + 1
    attraversamenti = [0] * (larghezza + 1)
    for x0, _, x1, _, _, _ in righe:
        for x in range(max(0, int(x0)), min(larghezza, int(x1)) + 1):
            attraversamenti[x] += 1

    # Quante fasce orizzontali di testo ci sono: la soglia di "poco attraversato" va rapportata
    # a quelle, non al numero di righe.
    fasce = len({round(r[1] / _ALTEZZA_FASCIA) for r in righe})
    soglia = max(1, int(fasce * _QUOTA_ATTRAVERSAMENTI_GRONDAIA))

    minimo, massimo = int(larghezza * 0.1), int(larghezza * 0.9)
    strisce: list[tuple[float, int]] = []
    inizio = None
    for x in range(minimo, massimo + 1):
        if attraversamenti[x] <= soglia and inizio is None:
            inizio = x
        elif attraversamenti[x] > soglia and inizio is not None:
            if x - inizio >= _LARGHEZZA_MINIMA_GRONDAIA:
                strisce.append(((inizio + x) / 2, x - inizio))
            inizio = None
    if inizio is not None and massimo - inizio >= _LARGHEZZA_MINIMA_GRONDAIA:
        strisce.append(((inizio + massimo) / 2, massimo - inizio))

    if len(strisce) != 1:
        return None  # nessuna striscia, oppure una griglia: si legge per righe
    centro, _ = strisce[0]
    if not larghezza * 0.3 <= centro <= larghezza * 0.7:
        return None
    if not any(r[2] <= centro for r in righe) or not any(r[0] >= centro for r in righe):
        return None
    return centro


def _ordina_blocchi(blocchi: list[tuple], rect: pymupdf.Rect) -> list[tuple[str, int]]:
    """Il testo dei blocchi nell'ordine di lettura.

    Su due colonne legge colonna per colonna. I blocchi che attraversano la grondaia — titoli,
    tabelle e figure a piena pagina — chiudono la banda corrente: quello che sta sopra si legge
    prima, quello che sta sotto dopo.
    """
    if not blocchi:
        return []

    def per_riga(gruppo: list[tuple]) -> list[tuple]:
        return sorted(gruppo, key=lambda b: (round(b[1], 1), b[0]))

    centro = _grondaia(blocchi, rect)
    if centro is None:
        return [(b[4], b[5]) for b in per_riga(blocchi)]

    fuori: list[tuple] = []
    sinistra: list[tuple] = []
    destra: list[tuple] = []
    for b in per_riga(blocchi):
        if b[0] < centro and b[2] > centro:
            fuori.extend(per_riga(sinistra) + per_riga(destra))
            sinistra, destra = [], []
            fuori.append(b)
        elif b[2] <= centro:
            sinistra.append(b)
        else:
            destra.append(b)
    fuori.extend(per_riga(sinistra) + per_riga(destra))
    return [(b[4], b[5]) for b in fuori]


def _dentro(riquadro: tuple[float, float, float, float], contenitori: list[pymupdf.Rect]) -> bool:
    """Vero se il riquadro sta, in larga parte, dentro una delle aree da escludere."""
    rect = pymupdf.Rect(riquadro)
    area = abs(rect.get_area())
    if area == 0:
        return True
    return any(abs((rect & c).get_area()) / area > 0.5 for c in contenitori)


def _ricomponi(righe: list[tuple[str, int]]) -> list[str]:
    """Riunisce in un capoverso le righe consecutive che venivano dallo stesso blocco."""
    capoversi: list[str] = []
    precedente: int | None = None
    for testo, blocco in righe:
        if blocco == precedente and capoversi:
            capoversi[-1] += "\n" + testo
        else:
            capoversi.append(testo)
        precedente = blocco
    return capoversi


def _estrai_pagina(pagina: pymupdf.Page, *, tabelle: bool) -> tuple[str, int]:
    """Il testo di una pagina, una passata per direzione di scrittura.

    Il riconoscimento delle tabelle gira solo sul testo diritto: nelle regioni di traverso
    `find_tables()` produce griglie frammentate, peggiori del testo ordinato correttamente,
    che il modello sa comunque ricomporre in tabella (regola 6 del prompt).
    """
    gruppi = _blocchi_per_direzione(pagina)
    if not gruppi:
        return "", 0

    rese, aree = _tabelle_markdown(pagina) if tabelle else ([], [])
    parti: list[str] = []
    n_griglie = 0
    for direzione in sorted(gruppi, key=lambda d: -len(gruppi[d])):
        blocchi = gruppi[direzione]
        if direzione == (1.0, 0.0) and aree:
            blocchi = [b for b in blocchi if not _dentro(b[:4], aree)]
        corpo = _ordina_blocchi(blocchi, _rect_diritto(direzione, pagina.rect))
        if not corpo:
            continue
        if direzione == (1.0, 0.0):
            parti.extend(_ricomponi(corpo))
            continue

        gradi = {(0.0, -1.0): 90, (0.0, 1.0): 270, (-1.0, 0.0): 180}[direzione]
        parti.append(f"[[testo stampato di traverso, ruotato di {gradi}°]]")
        prima, griglia, dopo = (
            _griglia_di_traverso(pagina, direzione) if tabelle else ([], None, [])
        )
        if griglia is None:
            parti.extend(_ricomponi(corpo))
            continue
        parti.extend(prima)
        parti.append(griglia)
        parti.extend(dopo)
        n_griglie += 1
    parti.extend(rese)
    return _normalizza("\n\n".join(parti)), len(rese) + n_griglie


def _normalizza(testo: str) -> str:
    """Ricuce le parole spezzate dal trattino morbido che i PDF delle riviste inseriscono.

    `mod\u00ad\nest` è la parola `modest`: lasciata così arriva al modello come due token
    senza senso, e in un valore numerico produrrebbe una cifra spezzata.
    """
    return _RE_CONTROLLO.sub("", _RE_TRATTINO_MORBIDO.sub("", testo)).strip()


# --------------------------------------------------------------------------------------
# Tabelle
# --------------------------------------------------------------------------------------


def _tabelle_markdown(pagina: pymupdf.Page) -> tuple[list[str], list[pymupdf.Rect]]:
    """Le tabelle della pagina rese in markdown, più le aree che occupano.

    Le aree servono a togliere dal flusso di testo le celle già riprodotte: senza questo
    passaggio ogni tabella comparirebbe due volte, una linearizzata e una formattata.
    """
    disegni = len(pagina.get_drawings())
    if disegni > _DISEGNI_MASSIMI_PER_TABELLE:
        # find_tables ricostruisce la griglia dalle linee disegnate: su una figura vettoriale
        # fittissima ci impiega mezzo minuto e non trova comunque niente, perché quelle linee
        # non formano celle. Le pagine con tabelle vere stanno tre ordini di grandezza sotto
        # questa soglia, quindi qui non si perde nessuna tabella.
        log.warning(
            "pagina %d: %d operazioni di disegno, ricerca tabelle saltata",
            pagina.number + 1, disegni,
        )
        return [], []

    try:
        trovate = pagina.find_tables()
    except Exception as exc:  # find_tables è euristico e su certi PDF alza eccezioni
        log.warning("pagina %d: ricerca tabelle fallita (%s)", pagina.number + 1, exc)
        return [], []

    rese, aree = [], []
    for tabella in trovate.tables:
        righe = [[_cella(c) for c in riga] for riga in tabella.extract()]
        righe = [r for r in righe if any(c for c in r)]
        if len(righe) < _RIGHE_MINIME_TABELLA or tabella.col_count < 2:
            # Una tabella a una colonna è un elenco; una a due righe è quasi sempre un
            # frammento di figura agganciato per sbaglio. Restano nel flusso di testo.
            continue
        piene = sum(1 for r in righe for c in r if c)
        if piene / (len(righe) * max(len(r) for r in righe)) < _QUOTA_MINIMA_CELLE_PIENE:
            continue  # griglia sparsa: è un disegno, non una tabella
        larghezza = max(len(r) for r in righe)
        righe = [r + [""] * (larghezza - len(r)) for r in righe]
        intestazione, corpo = righe[0], righe[1:]
        md = ["| " + " | ".join(intestazione) + " |", "|" + "---|" * larghezza]
        md += ["| " + " | ".join(r) + " |" for r in corpo]
        rese.append("\n".join(md))
        aree.append(pymupdf.Rect(tabella.bbox))
    return rese, aree


def _cella(valore: str | None) -> str:
    if not valore:
        return ""
    return re.sub(r"\s+", " ", valore).replace("|", "\\|").strip()


# --------------------------------------------------------------------------------------
# Tabelle stampate di traverso
# --------------------------------------------------------------------------------------


def _parole_diritte(
    pagina: pymupdf.Page, direzione: tuple[float, float]
) -> list[tuple[float, float, float, float, str]]:
    """Le parole scritte in una data direzione, con le coordinate raddrizzate.

    La granularità della parola è necessaria: PyMuPDF fonde in un'unica riga celle adiacenti
    che condividono la linea di base, e una riga che scavalca il confine fra due colonne
    porterebbe con sé il valore della colonna accanto.

    L'aggancio parola → riga usa gli indici che `get_text("words")` riporta, non la geometria:
    il riquadro di una riga ruotata è alto e stretto, e una parola breve del testo diritto ci
    cade dentro per intero. Gli indici di blocco però contano i soli blocchi di testo, mentre
    quelli del dizionario contano anche le immagini: la numerazione va rifatta qui.
    """
    direzioni: dict[tuple[int, int], tuple[float, float]] = {}
    blocco_di_testo = 0
    for blocco in pagina.get_text("dict")["blocks"]:
        if blocco.get("type") != 0:
            continue
        for indice, riga in enumerate(blocco["lines"]):
            direzioni[(blocco_di_testo, indice)] = tuple(float(round(v)) for v in riga["dir"])
        blocco_di_testo += 1

    return [
        (*_riquadro_diritto(parola[:4], direzione, pagina.rect), parola[4])
        for parola in pagina.get_text("words")
        if direzioni.get((parola[5], parola[6])) == direzione
    ]


def _fasce(intervalli: list[tuple[float, float]], lunghezza: int) -> list[tuple[int, int]]:
    """Le fasce occupate lungo un asse, separate dal bianco."""
    occupato = bytearray(lunghezza + 2)
    for a, b in intervalli:
        for i in range(max(0, int(a)), min(lunghezza, int(b)) + 1):
            occupato[i] = 1
    fasce, inizio = [], None
    for i in range(lunghezza + 1):
        if occupato[i] and inizio is None:
            inizio = i
        elif not occupato[i] and inizio is not None:
            fasce.append((inizio, i))
            inizio = None
    if inizio is not None:
        fasce.append((inizio, lunghezza))
    return fasce


def _separatori_di_colonna(
    righe: list[list[tuple]], larghezza: int
) -> list[float]:
    """Le x che separano le colonne: quelle libere in quasi tutte le righe di griglia.

    Riceve le sole righe di griglia, senza didascalia né note a piè di tabella: quelle
    attraversano l'intera larghezza e cancellerebbero ogni separatore. Sulle righe rimaste la
    soglia può stare alta, ed è ciò che tiene fuori i separatori fantasma prodotti da una
    colonna il cui testo va a capo sempre nello stesso punto.
    """
    libere = [0] * (larghezza + 2)
    for riga in righe:
        occupato = bytearray(larghezza + 2)
        for x0, _, x1, _, _ in riga:
            for x in range(max(0, int(x0)), min(larghezza, int(x1)) + 1):
                occupato[x] = 1
        for x in range(larghezza + 1):
            if not occupato[x]:
                libere[x] += 1

    soglia = len(righe) * _QUOTA_RIGHE_PER_SEPARATORE
    separatori, inizio = [], None
    for x in range(larghezza + 1):
        if libere[x] >= soglia and inizio is None:
            inizio = x
        elif libere[x] < soglia and inizio is not None:
            if x - inizio >= _LARGHEZZA_MINIMA_SEPARATORE:
                separatori.append((inizio + x) / 2)
            inizio = None
    return separatori


def _e_prosa(riga: list[tuple]) -> bool:
    """Vero se la riga è testo corrente — didascalia, nota — e non una riga di tabella.

    In una riga di tabella fra una colonna e l'altra c'è un salto netto; in un capoverso le
    parole si susseguono a distanza di uno spazio.
    """
    if len(riga) < 2:
        return True
    ordinate = sorted(riga)
    salto = max(b[0] - a[2] for a, b in zip(ordinate, ordinate[1:]))
    return salto < _SALTO_MINIMO_FRA_COLONNE


def _cella_da_parole(parole: list[tuple]) -> str:
    """Il testo di una cella, rimettendo in fila le righe in cui va a capo."""
    if not parole:
        return ""
    altezza = statistics.median(p[3] - p[1] for p in parole) or 1
    sottorighe: dict[int, list[tuple]] = {}
    for parola in parole:
        sottorighe.setdefault(int(parola[1] / (altezza * 0.6)), []).append(parola)
    testo = ""
    for _, gruppo in sorted(sottorighe.items()):
        pezzo = " ".join(p[4] for p in sorted(gruppo, key=lambda p: p[0]))
        testo = _unisci_a_capo(testo, pezzo) if testo else pezzo
    return _cella(testo)


def _unisci_a_capo(testo: str, seguito: str) -> str:
    """Riunisce due spezzoni separati dall'andata a capo dentro una cella.

    Il caso che conta è l'intervallo numerico: `7-` seguito da `10:1` deve tornare `7-10:1`,
    non `7- 10:1`. Una parola sillabata perde invece il trattino.
    """
    if not seguito or not testo.endswith("-"):
        return f"{testo} {seguito}"
    if seguito[0].isdigit():
        return testo + seguito
    if seguito[0].islower():
        return testo[:-1] + seguito
    return f"{testo} {seguito}"


def _griglia_di_traverso(
    pagina: pymupdf.Page, direzione: tuple[float, float]
) -> tuple[list[str], str | None, list[str]]:
    """Ricostruisce la tabella di una regione stampata di traverso.

    Restituisce il testo che la precede, la tabella in markdown e il testo che la segue.
    Se la regione non è una griglia restituisce `(righe, None, [])`, e chi chiama ricade
    sull'ordinamento per righe.

    `find_tables()` non serve qui: sul testo ruotato produce griglie frammentate. Ma lasciare
    che il modello ricostruisca da sé una tabella larga letta per fasce orizzontali significa
    consegnargli le celle di righe diverse mescolate — ed è il caso in cui un dosaggio finisce
    attribuito alla popolazione sbagliata.
    """
    parole = _parole_diritte(pagina, direzione)
    if len(parole) < _PAROLE_MINIME_GRIGLIA:
        return [], None, []

    rect = _rect_diritto(direzione, pagina.rect)
    larghezza, altezza = int(rect.width), int(rect.height)
    righe = [
        [p for p in parole if a - 1 <= (p[1] + p[3]) / 2 <= b + 1]
        for a, b in _fasce([(p[1], p[3]) for p in parole], altezza)
    ]
    righe = [r for r in righe if r]
    if len(righe) < _RIGHE_MINIME_TABELLA:
        return [], None, []

    di_griglia = [r for r in righe if not _e_prosa(r)]
    if len(di_griglia) < _RIGHE_MINIME_TABELLA:
        return [], None, []
    separatori = _separatori_di_colonna(di_griglia, larghezza)
    # I separatori ai margini non delimitano colonne di contenuto.
    minimo = min(p[0] for p in parole)
    massimo = max(p[2] for p in parole)
    separatori = [x for x in separatori if minimo < x < massimo]
    if len(separatori) + 1 < _COLONNE_MINIME_GRIGLIA:
        return [], None, []

    def colonna(parola: tuple) -> int:
        centro = (parola[0] + parola[2]) / 2
        return sum(1 for x in separatori if centro > x)

    prima: list[str] = []
    corpo: list[list[str]] = []
    dopo: list[str] = []
    for riga in righe:
        if _e_prosa(riga):
            testo = _cella_da_parole(riga)
            (dopo if corpo else prima).append(testo)
            continue
        celle = [[] for _ in range(len(separatori) + 1)]
        for parola in riga:
            celle[colonna(parola)].append(parola)
        corpo.append([_cella_da_parole(c) for c in celle])

    if len(corpo) < _RIGHE_MINIME_TABELLA:
        return [], None, []

    # Colonne vuote in tutte le righe: sono spazi di impaginazione, non colonne.
    tenute = [i for i in range(len(corpo[0])) if any(r[i] for r in corpo)]
    corpo = [[r[i] for i in tenute] for r in corpo]
    larghezza_tabella = len(tenute)
    markdown = [
        "| " + " | ".join(corpo[0]) + " |",
        "|" + "---|" * larghezza_tabella,
        *("| " + " | ".join(r) + " |" for r in corpo[1:]),
    ]
    return prima, "\n".join(markdown), dopo


# --------------------------------------------------------------------------------------
# Numeri di pagina stampati
# --------------------------------------------------------------------------------------


def _candidati_numero(pagina: pymupdf.Page) -> list[tuple[str, str, int]]:
    """I token di intestazione e piè di pagina che potrebbero essere il numero stampato.

    Restituisce terne ``(banda, prefisso, numero)``. Considera solo le righe brevi: il numero
    di pagina sta su una riga con al più il titolo corrente e il nome della rivista, mentre un
    numero pescato in mezzo a un capoverso di prosa è quasi sempre altro.
    """
    rect = pagina.rect
    banda = rect.height * _FASCIA_INTESTAZIONE
    fasce = {
        "alto": pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + banda),
        "basso": pymupdf.Rect(rect.x0, rect.y1 - banda, rect.x1, rect.y1),
    }
    candidati = []
    for nome, fascia in fasce.items():
        for riga in _RE_RUMORE_DI_MARGINE.sub("", pagina.get_textbox(fascia)).splitlines():
            if len(riga) > _MAX_CARATTERI_RIGA_MARGINE:
                continue
            for token in riga.split():
                m = _RE_NUMERO_PAGINA.match(
                    _RE_RUMORE_DI_MARGINE.sub("", token).strip(".,;:()[]")
                )
                if m:
                    candidati.append((nome, m.group(1), int(m.group(2))))
    return candidati


_RE_STRINGA_ESADECIMALE = re.compile(r"<([0-9A-Fa-f\s]+)>")


def _decodifica_stringa_pdf(etichetta: str) -> str:
    """Scioglie le stringhe esadecimali che PyMuPDF lascia grezze nelle *page label*.

    Le riviste a paginazione elettronica dichiarano il prefisso come stringa PDF esadecimale:
    la linea guida AHA/ACC ha `<FEFF0065>977`, dove `<FEFF0065>` è la "e" in UTF-16BE. Senza
    questo passaggio la nota rimanderebbe a `<FEFF0065>977` invece che a `e977`.
    """

    def scioglie(m: re.Match[str]) -> str:
        try:
            grezzo = bytes.fromhex(re.sub(r"\s", "", m.group(1)))
        except ValueError:
            return m.group(0)
        if grezzo[:2] == b"\xfe\xff":
            return grezzo[2:].decode("utf-16-be", errors="replace")
        return grezzo.decode("latin-1", errors="replace")

    return _RE_STRINGA_ESADECIMALE.sub(scioglie, etichetta)


def _etichette_stampate(
    documento: pymupdf.Document, prima_pagina: str | None = None
) -> list[str] | None:
    """Ricostruisce i numeri di pagina stampati, o restituisce ``None`` se non è sicuro.

    Prima prova le *page label* dichiarate nel PDF, che sono autorevoli quando ci sono — ma
    solo se il catalogo le dichiara davvero: ``get_label()`` restituisce stringa vuota, non
    un errore, quando l'albero ``PageLabels`` manca.

    Altrimenti cerca in intestazione e piè di pagina un token numerico che cresca di uno a
    ogni pagina: è il solo criterio che distingue il numero di pagina dall'anno di
    pubblicazione o dal numero del volume, che pure stanno nel margine. Sopravvive ai prefissi
    delle riviste a paginazione elettronica (``e977`` … ``e1051``).

    Restano i casi in cui due serie crescono di pari passo — tipicamente la numerazione delle
    sezioni nell'intestazione e quella delle pagine nel piè. Si sciolgono così:

    1. se CrossRef ha dato la prima pagina dell'articolo, vince la serie che comincia da lì;
    2. altrimenti vince il piè di pagina sull'intestazione;
    3. se l'ambiguità resta, ``None``. La nota ricadrà sui numeri di sezione: un rimando più
       grossolano è sempre preferibile a un numero di pagina inventato.
    """
    if documento.xref_get_key(documento.pdf_catalog(), "PageLabels")[0] != "null":
        etichette = [_decodifica_stringa_pdf(p.get_label()) for p in documento]
        # Numeri ripetuti significano che l'albero non descrive la paginazione dell'articolo
        # (capita con i frontespizi in numeri romani ripartiti male): meglio l'euristica.
        if all(etichette) and len(set(etichette)) == len(etichette):
            return etichette
        log.warning("page label dichiarate ma inutilizzabili: si passa all'euristica sui margini")

    # (banda, prefisso, offset) -> pagine che sostengono l'ipotesi
    supporto: dict[tuple[str, str, int], set[int]] = {}
    for pagina in documento:
        for banda, prefisso, numero in _candidati_numero(pagina):
            supporto.setdefault((banda, prefisso, numero - pagina.number), set()).add(pagina.number)

    def rango(ipotesi: tuple[str, str, int]) -> tuple[int, int, int]:
        banda, prefisso, offset = ipotesi
        atteso = f"{prefisso}{offset}"
        return (
            1 if prima_pagina and atteso == prima_pagina else 0,
            len(supporto[ipotesi]),
            1 if banda == "basso" else 0,
        )

    ipotesi = [i for i in supporto if len(supporto[i]) / documento.page_count >= 0.6]
    if not ipotesi:
        return None

    ipotesi.sort(key=rango, reverse=True)
    if len(ipotesi) > 1 and rango(ipotesi[0]) == rango(ipotesi[1]):
        etichette_0 = (ipotesi[0][1], ipotesi[0][2])
        etichette_1 = (ipotesi[1][1], ipotesi[1][2])
        if etichette_0 != etichette_1:
            log.warning(
                "numeri di pagina ambigui (%s vs %s): si ricade sui numeri di sezione",
                f"{etichette_0[0]}{etichette_0[1]}…",
                f"{etichette_1[0]}{etichette_1[1]}…",
            )
            return None

    _, prefisso, offset = ipotesi[0]
    return [f"{prefisso}{i + offset}" for i in range(documento.page_count)]


# --------------------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------------------


def _serve_ocr(documento: pymupdf.Document) -> bool:
    if documento.page_count == 0:
        return False
    caratteri = sum(len(p.get_text("text").strip()) for p in documento)
    return caratteri / documento.page_count < SOGLIA_CARATTERI_PER_PAGINA


def _applica_ocr(percorso: Path) -> Path:
    """Passa il PDF da ocrmypdf e restituisce il percorso del file con lo strato di testo."""
    if shutil.which("ocrmypdf") is None:
        raise RuntimeError(
            f"{percorso.name} non ha uno strato di testo utilizzabile e ocrmypdf non è installato. "
            "Installalo (brew install ocrmypdf) oppure rilancia con --no-ocr per estrarre comunque."
        )
    destinazione = Path(tempfile.mkdtemp(prefix="paper-pipeline-ocr-")) / percorso.name
    log.info("nessuno strato di testo: OCR con ocrmypdf (può richiedere qualche minuto)")
    subprocess.run(
        ["ocrmypdf", "--skip-text", "--language", "eng+ita", str(percorso), str(destinazione)],
        check=True,
        capture_output=True,
    )
    return destinazione


# --------------------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------------------


class _HtmlInTesto(html.parser.HTMLParser):
    """Riduce un HTML a testo, conservando titoli, elenchi e tabelle.

    Gli articoli open access senza paginazione arrivano in questo formato, e la loro
    struttura di sezioni numerate è l'unico aggancio disponibile per i rimandi. Le tabelle
    vengono ricostruite in markdown, per la stessa ragione per cui si ricostruiscono quelle
    dei PDF: linearizzate perdono il confronto fra colonne, che è la parte utile.
    """

    _SALTA = {"script", "style", "noscript", "svg", "head", "nav", "footer"}
    _BLOCCHI = {"p", "div", "section", "article", "br", "figcaption", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parti: list[str] = []
        self.n_tabelle = 0
        self._salta = 0
        self._righe: list[list[str]] | None = None  # tabella in costruzione
        self._cella: list[str] | None = None

    # -- struttura -------------------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if tag in self._SALTA:
            self._salta += 1
        elif tag == "table":
            self._righe = []
        elif tag == "tr" and self._righe is not None:
            self._righe.append([])
        elif tag in ("td", "th") and self._righe is not None:
            self._cella = []
        elif re.fullmatch(r"h[1-6]", tag):
            self._scrivi("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self._scrivi("\n- ")
        elif tag in self._BLOCCHI:
            self._scrivi("\n")

    def handle_endtag(self, tag):
        if tag in self._SALTA:
            self._salta = max(0, self._salta - 1)
        elif tag == "table":
            self._chiudi_tabella()
        elif tag in ("td", "th") and self._righe is not None:
            testo = re.sub(r"\s+", " ", "".join(self._cella or [])).strip()
            if self._righe:
                self._righe[-1].append(testo.replace("|", "\\|"))
            self._cella = None
        elif re.fullmatch(r"h[1-6]", tag) or tag in self._BLOCCHI:
            self._scrivi("\n")

    def handle_data(self, data):
        if self._salta:
            return
        testo = re.sub(r"[ \t\r\f\v]+", " ", data)
        if not testo.strip():
            return
        if self._cella is not None:
            self._cella.append(testo)
        elif self._righe is None:
            self.parti.append(testo)

    # -- resa ------------------------------------------------------------------------
    def _scrivi(self, testo: str) -> None:
        if not self._salta and self._righe is None:
            self.parti.append(testo)

    def _chiudi_tabella(self) -> None:
        righe = [r for r in (self._righe or []) if any(c for c in r)]
        self._righe, self._cella = None, None
        if not righe:
            return
        larghezza = max(len(r) for r in righe)
        if larghezza < 2:  # una colonna sola è un elenco, non una tabella
            self.parti.append("\n" + "\n".join(f"- {r[0]}" for r in righe) + "\n")
            return
        righe = [r + [""] * (larghezza - len(r)) for r in righe]
        md = ["| " + " | ".join(righe[0]) + " |", "|" + "---|" * larghezza]
        md += ["| " + " | ".join(r) + " |" for r in righe[1:]]
        self.parti.append("\n\n" + "\n".join(md) + "\n\n")
        self.n_tabelle += 1

    def testo(self) -> str:
        grezzo = "".join(self.parti)
        grezzo = re.sub(r" *\n *", "\n", grezzo)
        return re.sub(r"\n{3,}", "\n\n", grezzo).strip()


def _estrai_html(percorso: Path) -> Documento:
    parser = _HtmlInTesto()
    parser.feed(percorso.read_text(encoding="utf-8", errors="replace"))
    testo = parser.testo()
    toc = [
        (len(m.group(1)), m.group(2).strip(), 0)
        for m in re.finditer(r"^(#{1,6}) +(.+)$", testo, flags=re.MULTILINE)
    ]
    return Documento(
        percorso=percorso,
        formato="html",
        pagine=[Pagina(indice=0, testo=testo, n_tabelle=parser.n_tabelle)],
        toc=toc,
        etichette_ricostruite=False,
    )


# --------------------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------------------


def extract(
    percorso: str | Path,
    *,
    ocr: bool = True,
    tabelle: bool = True,
    prima_pagina: str | None = None,
) -> Documento:
    """Estrae il documento e lo restituisce pronto per il prompt.

    ``ocr=False`` impedisce il passaggio da ocrmypdf anche quando lo strato di testo manca:
    utile per ispezionare in fretta un documento, inutile per una nota vera.

    ``prima_pagina`` è il numero della prima pagina dell'articolo secondo CrossRef (``301``,
    ``e977``). Quando c'è, scioglie le ambiguità nel riconoscimento dei numeri stampati.
    """
    from src.web_article import e_url, estrai

    if e_url(percorso):
        return estrai(str(percorso))
    percorso = Path(percorso)
    if not percorso.is_file():
        raise FileNotFoundError(percorso)
    if percorso.suffix.lower() in (".html", ".htm"):
        return _estrai_html(percorso)

    documento = pymupdf.open(percorso)
    ocr_applicato = False
    if _serve_ocr(documento):
        if ocr:
            documento.close()
            documento = pymupdf.open(_applica_ocr(percorso))
            ocr_applicato = True
        else:
            log.warning(
                "%s sembra privo di strato di testo e l'OCR è disattivato: estrazione parziale",
                percorso.name,
            )

    etichette = _etichette_stampate(documento, prima_pagina)
    pagine = []
    for pagina in documento:
        testo, n_tabelle = _estrai_pagina(pagina, tabelle=tabelle)
        pagine.append(
            Pagina(
                indice=pagina.number,
                testo=testo,
                etichetta=etichette[pagina.number] if etichette else None,
                n_tabelle=n_tabelle,
            )
        )

    toc = [(liv, tit, pag) for liv, tit, pag in documento.get_toc()]
    documento.close()

    doc = Documento(
        percorso=percorso,
        formato="pdf",
        pagine=pagine,
        toc=toc,
        ocr_applicato=ocr_applicato,
        etichette_ricostruite=etichette is not None,
    )
    log.info(
        "%s: %d pagine, %d caratteri, ~%d token, %d tabelle, riferimenti per %s%s",
        percorso.name,
        doc.n_pagine,
        doc.n_caratteri,
        doc.token_stimati,
        doc.n_tabelle,
        doc.criterio_riferimenti,
        " (dopo OCR)" if ocr_applicato else "",
    )
    return doc


# --------------------------------------------------------------------------------------
# CLI di ispezione
# --------------------------------------------------------------------------------------


def _report(doc: Documento) -> str:
    righe = [
        f"fonte                 {(doc.sorgente_web or {}).get('url_finale', doc.percorso)}",
        f"formato               {doc.formato}",
        f"pagine                {doc.n_pagine}",
        f"caratteri             {doc.n_caratteri}",
        f"token stimati         ~{doc.token_stimati}",
        f"tabelle riconosciute  {doc.n_tabelle}",
        f"OCR applicato         {'sì' if doc.ocr_applicato else 'no'}",
        f"numeri stampati       {'ricostruiti' if doc.etichette_ricostruite else 'NON trovati'}",
        f"criterio riferimenti  {doc.criterio_riferimenti}",
        f"bookmark (TOC)        {len(doc.toc)}",
    ]
    if doc.formato == "pdf" and doc.pagine:
        primi = ", ".join(p.riferimento for p in doc.pagine[:5])
        ultimi = ", ".join(p.riferimento for p in doc.pagine[-3:])
        righe.append(f"etichette pagina      {primi} … {ultimi}")
        vuote = [p.indice + 1 for p in doc.pagine if p.n_caratteri < SOGLIA_CARATTERI_PER_PAGINA]
        if vuote:
            righe.append(f"pagine quasi vuote    {vuote}")
    if doc.toc:
        righe.append("")
        righe.append("TOC:")
        righe.append(doc.toc_text())
    return "\n".join(righe)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.extract",
        description="Estrae il testo di un PDF o HTML e ne mostra le caratteristiche, "
        "senza spendere chiamate API.",
    )
    parser.add_argument("percorso", help="PDF, HTML locale o URL del full-text")
    parser.add_argument("--report", action="store_true", help="stampa il riepilogo (default)")
    parser.add_argument("--text", action="store_true", help="stampa il testo estratto")
    parser.add_argument("--out", type=Path, help="scrive il testo estratto su file")
    parser.add_argument(
        "--prima-pagina",
        help="numero della prima pagina secondo CrossRef (es. 301, e977): scioglie le ambiguità",
    )
    parser.add_argument("--no-ocr", action="store_true", help="non invocare ocrmypdf")
    parser.add_argument("--no-tables", action="store_true", help="non riconoscere le tabelle")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    doc = extract(
        args.percorso,
        ocr=not args.no_ocr,
        tabelle=not args.no_tables,
        prima_pagina=args.prima_pagina,
    )

    if args.out:
        args.out.write_text(doc.document_text(), encoding="utf-8")
        print(f"testo scritto in {args.out}", file=sys.stderr)
    if args.text:
        print(doc.document_text())
    if args.report or not (args.text or args.out):
        print(_report(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
