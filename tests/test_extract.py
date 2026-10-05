"""Verifiche su src/extract.py.

Girano sui fixture sintetici in tests/fixtures/, non sul corpus reale: servono a bloccare le
regressioni sulle insidie note (due colonne, numeri di pagina stampati diversi dall'indice,
tabelle linearizzate, documenti senza paginazione), non a validare la qualità dell'estrazione
sui tre documenti di riferimento — quello resta un confronto contro i golden output.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.extract as extract_mod  # noqa: E402
from src.extract import SOGLIA_CARATTERI_PER_PAGINA, extract  # noqa: E402
from tests.fixtures.make_fixture_pdf import costruisci  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
HTML = FIXTURES / "finto-articolo-open-access.html"


@pytest.fixture(scope="module")
def pdf() -> Path:
    return costruisci()


# -- PDF ---------------------------------------------------------------------------------


def test_ordine_di_lettura_su_due_colonne(pdf: Path) -> None:
    """La colonna sinistra va letta per intero prima della destra, non riga per riga."""
    testo = extract(pdf).pagine[0].testo
    assert testo.index("COLONNA SINISTRA 1") < testo.index("COLONNA DESTRA 1")
    assert testo.index("circuito di controllo") < testo.index("COLONNA DESTRA 1")


def test_il_titolo_a_piena_pagina_precede_le_colonne(pdf: Path) -> None:
    testo = extract(pdf).pagine[0].testo
    assert testo.index("Titolo a piena pagina") < testo.index("COLONNA SINISTRA 1")


def test_numeri_di_pagina_stampati_non_indici_del_pdf(pdf: Path) -> None:
    """L'articolo comincia a pagina 301: citare "1" sarebbe un riferimento inventato."""
    doc = extract(pdf)
    assert doc.etichette_ricostruite
    assert [p.riferimento for p in doc.pagine] == ["301", "302", "303", "304", "305"]
    assert doc.criterio_riferimenti == "pagina"


def test_anno_e_volume_nel_pie_non_vengono_scambiati_per_pagine(pdf: Path) -> None:
    assert "2026" not in [p.etichetta for p in extract(pdf).pagine]


def test_la_prima_pagina_crossref_scioglie_le_ambiguita(pdf: Path) -> None:
    doc = extract(pdf, prima_pagina="301")
    assert [p.riferimento for p in doc.pagine] == ["301", "302", "303", "304", "305"]


def test_i_marcatori_di_pagina_usano_il_numero_stampato(pdf: Path) -> None:
    testo = extract(pdf).document_text()
    assert "[[pagina 301]]" in testo
    assert "[[pagina 1]]" not in testo


def test_tabella_resa_in_markdown_e_non_duplicata(pdf: Path) -> None:
    doc = extract(pdf)
    testo = doc.pagine[1].testo
    assert doc.pagine[1].n_tabelle == 1
    assert "| Forma | BHB : acetoacetato | Glicemia |" in testo
    assert "|---|---|---|" in testo
    # le celle non devono comparire anche linearizzate nel flusso di testo
    assert testo.count("7-10:1") == 1


def test_senza_riconoscimento_tabelle_le_celle_restano_nel_testo(pdf: Path) -> None:
    doc = extract(pdf, tabelle=False)
    assert doc.n_tabelle == 0
    assert "|---|---|---|" not in doc.pagine[1].testo
    assert "|---|" not in doc.pagine[4].testo  # vale anche per le griglie di traverso


def test_toc_recuperato(pdf: Path) -> None:
    toc = extract(pdf).toc
    assert (1, "Fisiologia del digiuno", 1) in toc
    assert any(liv == 2 for liv, _, _ in toc)


def test_stima_token(pdf: Path) -> None:
    doc = extract(pdf)
    assert doc.token_stimati == round(doc.n_caratteri / 4)
    assert doc.token_stimati > 0


# -- PDF scansionato ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def pdf_scansionato(pdf: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Lo stesso PDF, rasterizzato: nessuno strato di testo, come un documento scansionato."""
    sorgente = pymupdf.open(pdf)
    destinazione = pymupdf.open()
    for pagina in sorgente:
        pix = pagina.get_pixmap(dpi=72)
        nuova = destinazione.new_page(width=pagina.rect.width, height=pagina.rect.height)
        nuova.insert_image(nuova.rect, pixmap=pix)
    percorso = tmp_path_factory.mktemp("scansionato") / "scansionato.pdf"
    destinazione.save(percorso)
    return percorso


def test_pdf_senza_strato_di_testo_riconosciuto(pdf_scansionato: Path, caplog) -> None:
    doc = extract(pdf_scansionato, ocr=False)
    assert doc.n_caratteri / doc.n_pagine < SOGLIA_CARATTERI_PER_PAGINA
    assert not doc.ocr_applicato
    assert "strato di testo" in caplog.text


def test_senza_ocrmypdf_il_messaggio_e_esplicito(pdf_scansionato: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.extract.shutil.which", lambda _: None)
    with pytest.raises(RuntimeError, match="ocrmypdf non è installato"):
        extract(pdf_scansionato, ocr=True)


# -- HTML --------------------------------------------------------------------------------


def test_html_ricade_sui_numeri_di_sezione() -> None:
    doc = extract(HTML)
    assert doc.formato == "html"
    assert not doc.etichette_ricostruite
    assert doc.criterio_riferimenti == "sezione"
    assert "[[pagina" not in doc.document_text()


def test_html_conserva_la_numerazione_delle_sezioni() -> None:
    testo = extract(HTML).document_text()
    assert "### 3.4 The role of chloride" in testo


def test_html_tabella_in_markdown() -> None:
    doc = extract(HTML)
    assert doc.n_tabelle == 1
    assert "| Class | Site of action |" in doc.pagine[0].testo
    assert "|---|---|" in doc.pagine[0].testo


def test_html_scarta_script_stile_e_navigazione() -> None:
    testo = extract(HTML).document_text()
    for rumore in ("var tracker", "display:none", "Home", "Copyright 2026"):
        assert rumore not in testo


def test_file_mancante() -> None:
    with pytest.raises(FileNotFoundError):
        extract(FIXTURES / "non-esiste.pdf")


# -- tabelle stampate di traverso --------------------------------------------------------


def test_tabella_ruotata_ricostruita(pdf: Path) -> None:
    """Le tabelle larghe delle riviste sono stampate di traverso: find_tables() non le vede."""
    testo = extract(pdf).pagine[4].testo
    assert "[[testo stampato di traverso, ruotato di 90°]]" in testo
    assert "| Forma | BHB : acetoacetato | Glicemia | Severita acidosi | Meccanismo chiave |" in testo


def test_ogni_valore_resta_nella_propria_colonna(pdf: Path) -> None:
    """La garanzia che conta: un rapporto o una soglia non deve scivolare nella colonna accanto."""
    righe = {
        l.split("|")[1].strip(): [c.strip() for c in l.strip("|").split("|")]
        for l in extract(pdf).pagine[4].testo.splitlines()
        if l.startswith("|") and not l.startswith("|---")
    }
    intestazione = righe["Forma"]
    for forma, rapporto, glicemia in [
        ("Chetosi da digiuno", "circa 1:1 (fino a 3:1)", "normale-bassa"),
        ("DKA", "3-4:1", "alta, tipicamente >250 mg/dL"),
        ("Alcolica", "7-10:1 o piu", "bassa, ipoglicemia comune"),
        ("SGLT2i", "tipicamente >3:1", "normale, se alta di solito <250"),
    ]:
        riga = righe[forma]
        assert riga[intestazione.index("BHB : acetoacetato")] == rapporto
        assert riga[intestazione.index("Glicemia")] == glicemia


def test_intervallo_numerico_spezzato_a_capo_ricomposto(pdf: Path) -> None:
    """`7-` a fine riga e `10:1` a capo devono tornare `7-10:1`, non `7- 10:1`."""
    testo = extract(pdf).pagine[4].testo
    assert "7-10:1 o piu" in testo
    assert "7- 10:1" not in testo


def test_il_testo_diritto_non_finisce_nella_griglia(pdf: Path) -> None:
    """Sulla stessa pagina convivono le colonne diritte e la tabella di traverso."""
    testo = extract(pdf).pagine[4].testo
    griglia = [l for l in testo.splitlines() if l.startswith("|")]
    assert not any("COLONNA" in l or "lipolisi" in l for l in griglia)
    assert "COLONNA SINISTRA 5" in testo  # il testo diritto resta, fuori dalla tabella


def test_ordine_delle_colonne_anche_con_tabella_ruotata(pdf: Path) -> None:
    testo = extract(pdf).pagine[4].testo
    assert testo.index("COLONNA SINISTRA 5") < testo.index("COLONNA DESTRA 5")


def test_unisci_a_capo() -> None:
    from src.extract import _unisci_a_capo

    assert _unisci_a_capo("elevated (7-", "10:1") == "elevated (7-10:1"  # intervallo numerico
    assert _unisci_a_capo("meta-", "bolism") == "metabolism"  # parola sillabata
    assert _unisci_a_capo("Normal to", "low") == "Normal to low"


def test_i_caratteri_di_controllo_non_arrivano_al_modello(pdf: Path) -> None:
    """Certe composizioni lasciano nel testo backspace e form feed: invisibili, ma attaccati a
    un numero di pagina o a un DOI li rendono irriconoscibili."""
    testo = extract(pdf).document_text()
    assert not [c for c in testo if ord(c) < 32 and c not in "\n\t"]


def test_numero_di_pagina_riconosciuto_nonostante_un_carattere_di_controllo(
    tmp_path: Path,
) -> None:
    """Nel piè di pagina del BMJ il numero è seguito da un backspace: senza ripulirlo, metà
    pagine non danno il numero e il documento ricade sui rimandi di sezione. Qui il backspace
    diventa un carattere di sostituzione, perché il font non ha il glifo per disegnarlo — è la
    stessa insidia con un'altra faccia, e il margine va ripulito di entrambe."""
    percorso = tmp_path / "controllo.pdf"
    doc = pymupdf.open()
    for i in range(4):
        pagina = doc.new_page(width=595, height=842)
        pagina.insert_textbox(
            pymupdf.Rect(50, 100, 545, 400),
            f"Corpo della pagina {i}. " + "Testo di riempimento, perche una pagina troppo "
            "vuota verrebbe scambiata per una scansione e mandata all OCR. " * 3,
            fontsize=10,
        )
        # il numero stampato, con la stessa insidia del PDF vero
        pagina.insert_textbox(pymupdf.Rect(50, 800, 545, 830), f"{7 + i}\x08", fontsize=8)
    doc.save(percorso)
    doc.close()

    estratto = extract(percorso)
    assert estratto.etichette_ricostruite
    assert [p.riferimento for p in estratto.pagine] == ["7", "8", "9", "10"]


def test_codifica_sana_non_da_falsi_allarmi(pdf: Path) -> None:
    assert not extract(pdf).codifica_sospetta


def test_font_simbolici_mappati_male_riconosciuti(tmp_path: Path) -> None:
    """`µm` che esce come `mm` è un fattore mille che non stona a leggerlo: va detto."""
    percorso = tmp_path / "codifica.pdf"
    doc = pymupdf.open()
    pagina = doc.new_page(width=595, height=842)
    pagina.insert_textbox(
        pymupdf.Rect(50, 100, 545, 500),
        "Le venule postcapillari misurano 7-20 mm þ e le piccole vene ¨ 20-150 mm þ. "
        "Il testo prosegue þ con altri simboli ¨ resi male þ dal font del documento þ.",
        fontsize=11,
    )
    doc.save(percorso)
    doc.close()
    assert extract(percorso).codifica_sospetta


def test_un_carattere_raro_isolato_non_basta(tmp_path: Path) -> None:
    """Un nome proprio o un simbolo matematico non sono una codifica rotta."""
    percorso = tmp_path / "nome.pdf"
    doc = pymupdf.open()
    pagina = doc.new_page(width=595, height=842)
    pagina.insert_textbox(
        pymupdf.Rect(50, 100, 545, 600),
        "Studio condotto da Søren Ð. e collaboratori. " + "Testo di riempimento ordinario. " * 40,
        fontsize=11,
    )
    doc.save(percorso)
    doc.close()
    assert not extract(percorso).codifica_sospetta


def test_pagina_con_troppi_disegni_salta_la_ricerca_tabelle(monkeypatch):
    """Una figura vettoriale fittissima non deve costare mezzo minuto di find_tables().

    Nasce da un caso vero: una pagina con 197.485 operazioni di disegno teneva occupato
    find_tables() per trenta secondi senza trovare nemmeno una tabella.
    """

    class PaginaFinta:
        number = 0

        def get_drawings(self):
            return [None] * (extract_mod._DISEGNI_MASSIMI_PER_TABELLE + 1)

        def find_tables(self):  # non deve mai essere chiamata
            raise AssertionError("find_tables non andava chiamata")

    assert extract_mod._tabelle_markdown(PaginaFinta()) == ([], [])


def test_pagina_normale_cerca_le_tabelle():
    """La guardia non deve scattare sulle pagine con tabelle vere, che stanno molto sotto."""

    class PaginaFinta:
        number = 0
        cercate = False

        def get_drawings(self):
            return [None] * 160  # il massimo osservato su una pagina con tabelle vere

        def find_tables(self):
            PaginaFinta.cercate = True

            class Vuoto:
                tables: list = []

            return Vuoto()

    extract_mod._tabelle_markdown(PaginaFinta())
    assert PaginaFinta.cercate
