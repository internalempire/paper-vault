"""Genera un PDF sintetico che riproduce le insidie del corpus reale.

Serve a esercitare `src/extract.py` senza i PDF veri (che non sono nel repo per ragioni di
copyright): due colonne, un titolo a piena pagina che spezza le colonne, numeri di pagina
stampati che non coincidono con l'indice del PDF (301, 302, …), una tabella a più colonne
e i bookmark.
"""

from pathlib import Path

import pymupdf

DESTINAZIONE = Path(__file__).with_name("finto-paper-due-colonne.pdf")
PRIMA_PAGINA_STAMPATA = 301
N_PAGINE = 5

# La tabella di p. 305 è stampata di traverso, come le tabelle larghe delle riviste vere.
TABELLA_DI_TRAVERSO = [
    ["Forma", "BHB : acetoacetato", "Glicemia", "Severita acidosi", "Meccanismo chiave"],
    ["Chetosi da digiuno", "circa 1:1 (fino a 3:1)", "normale-bassa", "lieve, HCO3 raramente <18", "risposta fisiologica al mancato apporto"],
    ["DKA", "3-4:1", "alta, tipicamente >250 mg/dL", "severa, HCO3 spesso <10", "carenza insulinica assoluta o relativa"],
    ["Alcolica", "7-10:1 o piu", "bassa, ipoglicemia comune", "severa, HCO3 spesso <15", "NADH dal metabolismo dell etanolo"],
    ["SGLT2i", "tipicamente >3:1", "normale, se alta di solito <250", "moderata-severa", "glicosuria forzata e turnover ATP alterato"],
]

SINISTRA = (
    "COLONNA SINISTRA {n}. La chetosi da digiuno e autolimitata perche i corpi chetonici "
    "stimolano la secrezione insulinica e sopprimono direttamente la lipolisi, chiudendo "
    "il circuito di controllo."
)
DESTRA = (
    "COLONNA DESTRA {n}. Nella chetoacidosi diabetica questo feedback manca: il combustibile "
    "abbonda e il difetto e l'insulina, quindi l'accumulo di chetoacidi procede senza freno."
)


def costruisci() -> Path:
    doc = pymupdf.open()
    for i in range(N_PAGINE):
        pagina = doc.new_page(width=595, height=842)
        stampata = PRIMA_PAGINA_STAMPATA + i

        pagina.insert_textbox(
            pymupdf.Rect(50, 40, 545, 90),
            f"Titolo a piena pagina della sezione {i + 1}",
            fontsize=16,
        )
        pagina.insert_textbox(
            pymupdf.Rect(50, 110, 285, 400), SINISTRA.format(n=i + 1), fontsize=10
        )
        pagina.insert_textbox(
            pymupdf.Rect(310, 110, 545, 400), DESTRA.format(n=i + 1), fontsize=10
        )
        if i == 1:
            _tabella(pagina)
        if i == 4:
            _tabella_di_traverso(pagina)
        # Piè di pagina: il numero stampato accanto al nome della rivista e all'anno, che
        # sono le due esche su cui un riconoscitore ingenuo inciampa.
        pagina.insert_textbox(
            pymupdf.Rect(50, 790, 545, 820),
            f"Am J Kidney Dis    2026    {stampata}",
            fontsize=8,
        )

    doc.set_toc(
        [
            [1, "Fisiologia del digiuno", 1],
            [2, "Fase postassorbitiva", 1],
            [1, "Le singole forme", 2],
            [1, "Tabella di riferimento", 3],
        ]
    )
    doc.save(DESTINAZIONE)
    doc.close()
    return DESTINAZIONE


def _tabella(pagina: pymupdf.Page) -> None:
    """Una tabella a griglia disegnata, che find_tables() deve riconoscere."""
    righe = [
        ["Forma", "BHB : acetoacetato", "Glicemia"],
        ["Digiuno", "~1:1", "normale-bassa"],
        ["DKA", "3-4:1", ">250 mg/dL"],
        ["Alcolica", "7-10:1", "ipoglicemia comune"],
    ]
    x0, y0, larghezza_col, altezza_riga = 60, 430, 160, 26
    for r, riga in enumerate(righe):
        for c, cella in enumerate(riga):
            box = pymupdf.Rect(
                x0 + c * larghezza_col,
                y0 + r * altezza_riga,
                x0 + (c + 1) * larghezza_col,
                y0 + (r + 1) * altezza_riga,
            )
            pagina.draw_rect(box, color=(0, 0, 0), width=0.6)
            pagina.insert_textbox(box + (4, 6, -4, -2), cella, fontsize=9)


def _tabella_di_traverso(pagina: pymupdf.Page) -> None:
    """Una tabella larga ruotata di 90°, senza griglia disegnata.

    È il caso che `find_tables()` non vede e che, letto per fasce orizzontali, mescola le
    celle di righe diverse. Le coordinate sono calcolate nel sistema "diritto" (orizzontale)
    e riportate sulla pagina verticale, che è l'inverso di ciò che fa l'estrattore.
    """
    altezza_pagina = pagina.rect.height
    x_iniziale, larghezza_colonna = 50, 150
    y_iniziale, altezza_riga = 60, 95

    for r, riga in enumerate(TABELLA_DI_TRAVERSO):
        for c, cella in enumerate(riga):
            # riquadro nel sistema diritto (orizzontale): le righe scendono, le colonne vanno
            # a destra, esattamente come si leggerebbe la tabella girando il foglio
            dx0 = x_iniziale + c * larghezza_colonna
            dx1 = dx0 + larghezza_colonna - 30
            dy0 = y_iniziale + r * altezza_riga
            dy1 = dy0 + altezza_riga - 15
            # e la sua immagine sulla pagina verticale
            box = pymupdf.Rect(dy0, altezza_pagina - dx1, dy1, altezza_pagina - dx0)
            pagina.insert_textbox(box, cella, fontsize=8, rotate=90)


if __name__ == "__main__":
    print(costruisci())
