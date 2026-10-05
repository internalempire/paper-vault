"""Figure originali, selezione fondata sulla fonte e pubblicazione con allegati.

Nessuna chiamata LLM/rete; PDF reali sintetici e vault in memoria.
"""
import json

import pymupdf
import pytest

from src.extract import Documento, Pagina
from src.figure import (PENDENTI, aggiungi_al_markdown, didascalie, fondi_figure, main,
                        numeri_citati, percorso_sicuro, prepara, salva_ritaglio)
from src.metadata import Metadati
from src.pubblica import PubblicazioneFallita, pubblica_file
from src.render import involucro, render, render_da_file, salva
from src.scarica import scarica
from tests.test_pubblica import VaultFinto

FONTE = "Clinical assessment follows the diagnostic pathway shown in Figure 1."


@pytest.fixture
def nota():
    return {
        "meta": {"title_it": "Valutazione", "titolo_nota": "Valutazione",
                 "tipo": "narrative review", "criterio_riferimenti": "pagina"},
        "sezioni": [{"titolo": "Diagnosi", "contenuto_md": "Valutare i criteri diagnostici."}],
        "figure_richieste": [{"numero": "1", "passaggio_fonte": FONTE, "sezione_nota": "Diagnosi"}],
    }


@pytest.fixture
def documento(tmp_path):
    p = tmp_path / "paper.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=500, height=650)
        page.insert_text((40, 65), FONTE, fontsize=10)
        page.insert_text((70, 140), "Full figure title")
        page.draw_rect((70, 170, 210, 280), color=(0, 0, 0), fill=(0.8, 0.9, 1))
        page.draw_rect((250, 170, 400, 280), color=(0, 0, 0), fill=(1, 0.9, 0.8))
        page.draw_line((210, 210), (250, 210))
        page.insert_text((90, 220), "Panel A: original 5 mg")
        page.insert_text((270, 220), "Panel B")
        page.insert_text((45, 320), "Figure 1. Full diagnostic pathway with both panels.", fontsize=10)
        pdf.save(p)
    return Documento(p, "pdf", [Pagina(0, FONTE)])


def preparata(nota, documento, root):
    markdown = render(nota, Metadati(), pdf=str(documento.percorso))
    return prepara(nota, documento, markdown, root, "paper")


def test_selezione_dalla_fonte_anche_senza_richiamo_nella_sintesi(nota, documento, tmp_path):
    figure, avvisi = preparata(nota, documento, tmp_path)
    f = figure[0]
    assert f["stato"] == "da_verificare" and not avvisi
    assert f["selezione_verificata"]
    assert "Figure" not in nota["sezioni"][0]["contenuto_md"]
    image = pymupdf.Pixmap(str(tmp_path / f["percorso"]))
    assert image.width > 700 and image.height > 200
    assert f["bbox"][0] <= 70 and f["bbox"][2] >= 400
    assert f["bbox"][1] < 140 and f["bbox"][3] > 280


@pytest.mark.parametrize("citazione", [
    "Clinical assessment is invented but it mentions Figure 1.",
    "Clinical assessment follows the diagnostic pathway",
    "Figure 1. Full diagnostic pathway with both panels.",
])
def test_non_seleziona_citazioni_inventate_senza_richiamo_o_didascalie(nota, documento, tmp_path, citazione):
    nota["figure_richieste"][0]["passaggio_fonte"] = citazione
    documento.pagine[0].testo += " Figure 1. Full diagnostic pathway with both panels."
    figure, avvisi = preparata(nota, documento, tmp_path)
    assert avvisi and figure[0]["stato"] == "non_disponibile"
    assert not list(tmp_path.glob("assets/**/*.png"))


def test_figura_citata_solo_in_passaggi_non_selezionati_non_estratta(nota, documento, tmp_path):
    nota["figure_richieste"] = []
    assert preparata(nota, documento, tmp_path) == ([], [])
    assert not list(tmp_path.glob("assets/**/*.png"))


def test_destinazione_inesistente_impedisce_inserimento(nota, documento, tmp_path):
    nota["figure_richieste"][0]["sezione_nota"] = "Sezione inventata"
    figure, _ = preparata(nota, documento, tmp_path)
    assert not figure[0].get("selezione_verificata")


def test_numerazione_pannelli_intervalli_e_supplementi():
    assert numeri_citati("See Figs. 1-3 and Figure S2A.") == {"1", "2", "3", "S2"}
    assert numeri_citati("Fig. 12 shows something") == {"12"}
    assert numeri_citati("Table 1") == set()


def test_figura_multipagina_segnalata_senza_ritaglio_parziale(nota, documento, tmp_path):
    with pymupdf.open(documento.percorso) as pdf:
        page = pdf.new_page()
        page.insert_text((50, 70), "Figure 1 Continued. More panels and explanations.")
        pdf.saveIncr()
    figure, avvisi = preparata(nota, documento, tmp_path)
    assert "più blocchi/pagine" in avvisi[0]
    assert figure[0]["stato"] == "non_disponibile"


def test_html_non_silenziosamente_trattato_come_pdf(nota, documento, tmp_path):
    documento.formato = "html"
    figure, avvisi = preparata(nota, documento, tmp_path)
    assert "HTML" in avvisi[0] and not figure[0].get("percorso")


def test_approvazione_rigenera_markdown_e_preserva_provenienza(nota, documento, tmp_path):
    figure, _ = preparata(nota, documento, tmp_path)
    dati = involucro(nota, Metadati(), pdf=str(documento.percorso))
    dati["figure"] = figure
    p = salva(tmp_path / ".json" / "paper.json", dati)
    assert PENDENTI in render_da_file(p)
    assert "![Figura" not in render_da_file(p)
    assert main([str(p), "--note", str(tmp_path), "--approva", "1"]) == 0
    md = (tmp_path / "paper.md").read_text()
    assert PENDENTI not in md
    assert "![Figura 1](assets/paper/" in md
    assert md.index("![Figura") < md.index("## Riferimento bibliografico")
    assert json.loads(p.read_text())["figure"][0]["passaggio_fonte"] == FONTE
    assert (tmp_path / ".anteprime" / "paper.html").is_file()


def test_json_salvato_prima_del_ritaglio_non_pubblicabile(nota, documento, tmp_path):
    p = salva(tmp_path / "paper.json", involucro(nota, Metadati(), pdf=str(documento.percorso)))
    assert PENDENTI in render_da_file(p)


def test_approvazione_rifiuta_immagine_modificata(nota, documento, tmp_path):
    figure, _ = preparata(nota, documento, tmp_path)
    dati = involucro(nota, Metadati(), pdf=str(documento.percorso))
    dati["figure"] = figure
    p = salva(tmp_path / "paper.json", dati)
    (tmp_path / figure[0]["percorso"]).write_bytes(b"corrupt")
    with pytest.raises(SystemExit):
        main([str(p), "--note", str(tmp_path), "--approva", "1"])
    assert json.loads(p.read_text())["figure"][0]["stato"] == "da_verificare"


def test_esclusione_non_richiede_immagine(nota, documento, tmp_path):
    dati = involucro(nota, Metadati(), pdf=str(documento.percorso))
    dati["figure"] = [{**nota["figure_richieste"][0], "stato": "non_disponibile"}]
    p = salva(tmp_path / "paper.json", dati)
    main([str(p), "--note", str(tmp_path), "--escludi", "1"])
    assert PENDENTI not in render_da_file(p)


@pytest.mark.parametrize("name", ["../outside.png", "/tmp/outside.png", "assets/../../outside.png", "assets\\outside.png"])
def test_percorsi_fuori_vault_rifiutati(tmp_path, name):
    with pytest.raises(ValueError):
        percorso_sicuro(tmp_path, name)


def test_symlink_fuori_vault_rifiutato(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "assets").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        percorso_sicuro(root, "assets/outside.png")


class VaultConFile(VaultFinto):
    def __init__(self, note=None, files=None):
        super().__init__(note)
        self.files = dict(files or {})

    def leggi_file(self, nome):
        self.chiamate.append(f"GET {nome}")
        return self.files.get(nome)

    def scrivi_file(self, nome, dati):
        self.chiamate.append(f"PUT {nome}")
        self.files[nome] = dati

    def elenco_file(self):
        return list(self.files)


def pubblicabile(nota, documento, tmp_path):
    figure, _ = preparata(nota, documento, tmp_path)
    figure[0]["stato"] = "approvata"
    testo = aggiungi_al_markdown(render(nota, Metadati(), pdf="x"), figure)
    p = tmp_path / "paper.md"
    p.write_text(testo)
    return p, figure[0]


def test_allegati_prima_della_nota_e_idempotenza(nota, documento, tmp_path):
    p, f = pubblicabile(nota, documento, tmp_path)
    v = VaultConFile()
    pubblica_file(v, p)
    assert v.chiamate.index("PUT " + f["percorso"]) < v.chiamate.index("crea paper")
    assert v.files[f["percorso"]] == (tmp_path / f["percorso"]).read_bytes()
    v.chiamate.clear()
    pubblica_file(v, p, cancella=True)
    assert not any(c.startswith(("PUT", "crea", "aggiorna")) for c in v.chiamate)
    assert not p.exists() and (tmp_path / f["percorso"]).exists()


def test_allegato_non_verificato_non_pubblica_ne_cancella_nota(nota, documento, tmp_path):
    class Rotto(VaultConFile):
        def scrivi_file(self, nome, dati):
            self.files[nome] = b"truncated"
    p, f = pubblicabile(nota, documento, tmp_path)
    v = Rotto()
    with pytest.raises(PubblicazioneFallita, match="verifica dell'allegato"):
        pubblica_file(v, p, cancella=True)
    assert p.exists() and not v.contenuti


def test_nota_troncata_non_cancella_copia_locale(nota, documento, tmp_path):
    class Rotto(VaultConFile):
        def crea(self, titolo, contenuto):
            self.contenuti[titolo] = contenuto[:10]
    p, _ = pubblicabile(nota, documento, tmp_path)
    with pytest.raises(PubblicazioneFallita, match="identica"):
        pubblica_file(Rotto(), p, cancella=True)
    assert p.exists()


def test_figure_pendenti_bloccano_pubblicazione(tmp_path):
    p = tmp_path / "paper.md"
    p.write_text("# Nota\n" + PENDENTI)
    v = VaultConFile()
    with pytest.raises(PubblicazioneFallita, match="figure da verificare"):
        pubblica_file(v, p, cancella=True)
    assert p.exists() and not v.chiamate


def test_fusione_aggiunge_figura_preservando_correzione_remota(nota, documento, tmp_path):
    p, _ = pubblicabile(nota, documento, tmp_path)
    v = VaultConFile({"paper": render(nota, Metadati(), pdf="x").replace(
        "Valutare i criteri diagnostici.", "Correzione dell'utente da preservare.")})
    pubblica_file(v, p)
    assert "Correzione dell'utente" in v.contenuti["paper"]
    assert v.contenuti["paper"].count("![Figura 1]") == 1
    pubblica_file(v, p)
    assert v.contenuti["paper"].count("![Figura 1]") == 1


def test_sezione_rinominata_sul_server_blocca_fusione(nota, documento, tmp_path):
    p, _ = pubblicabile(nota, documento, tmp_path)
    v = VaultConFile({"paper": "# Nota\n\n## Diagnosi modificata\n\nCorrezione"})
    with pytest.raises(PubblicazioneFallita, match="sezione"):
        pubblica_file(v, p, cancella=True)
    assert p.exists() and "![Figura" not in v.contenuti["paper"]


def test_backup_binario_con_orfani_e_senza_sessioni(tmp_path):
    files = {"paper.md": b"# note", "assets/paper/f.png": b"\x89PNG\xff\x00",
             ".silverbullet.auth.json": b"secret", "Library/sys.md": b"system"}
    v = VaultConFile(files=files)
    (tmp_path / "orfano.md").write_text("da conservare")
    aggiornati, uguali, orfani = scarica(v, tmp_path)
    assert (aggiornati, uguali, orfani) == (2, 0, ["orfano.md"])
    assert (tmp_path / "assets/paper/f.png").read_bytes() == files["assets/paper/f.png"]
    assert not (tmp_path / ".silverbullet.auth.json").exists()
    assert not (tmp_path / "Library").exists()
    assert scarica(v, tmp_path) == (0, 2, ["orfano.md"])


def test_backup_rifiuta_percorsi_assoluti(tmp_path):
    with pytest.raises(ValueError):
        scarica(VaultConFile(files={"/tmp/escape.png": b"bad"}), tmp_path)


def test_pipeline_salva_figure_e_non_richiama_llm_alla_ripresa(nota, documento, tmp_path, monkeypatch):
    from src.ingest import Configurazione, ingest
    from tests.test_ingest import EstrattoreFinto
    monkeypatch.setattr("src.ingest.metadata.recupera", lambda *a, **kw: Metadati(doi="10.1234/demo", anno=2026))
    conf = Configurazione(note=tmp_path / "note", json=tmp_path / "note/.json", ocr=False)
    v = VaultConFile()
    esito = ingest(documento.percorso, conf, estrattore=EstrattoreFinto(nota), vault=v)
    dati = json.loads(esito.json.read_text())
    assert dati["figure"][0]["stato"] == "da_verificare"
    assert esito.nota.exists() and not v.contenuti
    assert any("figure da verificare" in a for a in esito.avvisi)
    class NoLLM:
        def classifica(self, *a, **kw):
            raise AssertionError("nessuna chiamata attesa")
        estrai = classifica
    # Ripresa dopo una preparazione interrotta: JSON pagato presente, ritagli mancanti.
    del dati["figure"]
    salva(esito.json, dati)
    nuovo = ingest(documento.percorso, conf, estrattore=NoLLM())
    assert json.loads(nuovo.json.read_text())["figure"][0]["stato"] == "da_verificare"
    assert PENDENTI in nuovo.nota.read_text()


def test_trasporto_binario_non_decodifica_png_come_utf8():
    import io
    from src.pubblica import Vault
    class Sessione:
        richieste = []
        def open(self, req, timeout):
            self.richieste.append(req)
            return io.BytesIO(b"\x89PNG\xff\x00")
    sessione = Sessione()
    v = Vault("https://example.invalid", "u", "p", _sessione=sessione)
    assert v.leggi_file("assets/paper/figura.png") == b"\x89PNG\xff\x00"
    v.scrivi_file("assets/paper/figura.png", b"\x89PNG\xff\x00")
    assert sessione.richieste[-1].get_header("Content-type") == "image/png"
    assert sessione.richieste[-1].data == b"\x89PNG\xff\x00"


def test_errore_di_scrittura_404_non_trattato_come_successo():
    import urllib.error
    from src.pubblica import Vault
    class Sessione:
        def open(self, req, timeout):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
    v = Vault("https://example.invalid", "u", "p", _sessione=Sessione())
    assert v.leggi_file("assets/missing.png") is None
    with pytest.raises(PubblicazioneFallita, match="PUT"):
        v.scrivi_file("assets/missing.png", b"png")
