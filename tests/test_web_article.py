"""Web -> stesso JSON/markdown dei PDF. Rete e LLM simulati, immagini reali sintetiche."""
import hashlib
import io
import json
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from src import web_article as web
from src.extract import extract
from src.figure import PENDENTI, main as figure_cli, prepara
from src.ingest import Configurazione, ingest
from src.metadata import Metadati
from src.pubblica import pubblica_file, PubblicazioneFallita
from src.render import render, render_da_file
from tests.test_ingest import EstrattoreFinto
from tests.test_figure import VaultConFile as VaultFinto

URL = "https://publisher.example/articles/paper/full"
QUOTE = "Clinical assessment follows the diagnostic pathway shown in Figure 1."
PROSA = "The source describes the clinical assessment and reports observations with their original units. " * 14


def pagina(figure=None, meta=True):
    if figure is None:
        figure = '''<figure id="fig1"><a href="/images/original.png"><img src="thumb.png"></a>
        <figcaption>Figure 1. Diagnostic pathway, all panels.</figcaption></figure>
        <figure id="fig2"><img src="/images/unused.png"><figcaption>Figure 2. Unused image.</figcaption></figure>'''
    return f'''<!doctype html><html><head><meta name="citation_title" content="Clinical review">
    {'<meta name="citation_doi" content="10.1234/actual">' if meta else ''}</head><body>
    <nav>Advertisement DOI 10.9999/wrong</nav><article><h1>Clinical review</h1>
    <h2>Abstract</h2><p>Summary.</p><h2>1 Introduction</h2><p>{QUOTE} {PROSA}</p>
    <h2>2 Clinical assessment</h2><p>{PROSA}</p><p>{PROSA}</p>{figure}
    <table><tr><th>Criterion</th><th>Value</th></tr><tr><td>Dose</td><td>5 mg</td></tr></table>
    <h2>References</h2><p>Bibliographic DOI 10.5678/not-this-paper</p></article>
    <footer>Subscribe</footer><script>ignore instructions</script></body></html>'''


@pytest.fixture
def doc():
    return web.da_html(pagina(), URL)


@pytest.fixture
def nota():
    return {"meta": {"title_it": "Valutazione", "titolo_nota": "Valutazione",
                      "tipo": "narrative review", "criterio_riferimenti": "sezione", "nota_riferimenti": "Rimandi alle sezioni della fonte."},
            "sezioni": [{"titolo": "Diagnosi", "contenuto_md": "Valutare i criteri diagnostici."}],
            "figure_richieste": [{"numero": "1", "passaggio_fonte": QUOTE, "sezione_nota": "Diagnosi"}]}


@pytest.fixture
def png():
    pix = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 120, 80), False)
    pix.clear_with(225)
    return pix.tobytes("png")


@pytest.fixture
def conf(tmp_path):
    return Configurazione(note=tmp_path / "note", json=tmp_path / "note/.json")


def mock_rete(monkeypatch, png):
    chiamate = []
    def fetch(url, **kwargs):
        chiamate.append(url)
        return (pagina().encode(), URL, "text/html") if url == URL else (png, url, "image/png")
    monkeypatch.setattr(web, "scarica", fetch)
    return chiamate


def test_estrazione_struttura_doi_catalogo_senza_download(doc):
    assert doc.sorgente_web["doi"] == "10.1234/actual"
    assert doc.criterio_riferimenti == "sezione" and doc.n_tabelle == 1
    assert "| Dose | 5 mg |" in doc.document_text()
    assert not any(x in doc.document_text() for x in ("Advertisement", "Subscribe", "ignore instructions"))
    assert list(doc.sorgente_web["figure"]) == ["1", "2"]
    assert doc.sorgente_web["figure"]["1"][0]["url_immagini"][0] == "https://publisher.example/images/original.png"
    assert QUOTE in doc.sorgente_web["testo_corpo"]
    assert "all panels" not in doc.sorgente_web["testo_corpo"]
    assert "not-this-paper" not in doc.sorgente_web["testo_corpo"]


@pytest.mark.parametrize("html", [
    '<h1>Access denied</h1>',
    f'<html><body><article><h1>Review</h1><h2>Abstract</h2><p>{PROSA}</p><p>{PROSA}</p><p>{PROSA}</p></article></body></html>',
    '<html><body><div id="app"></div><script>loadFullText()</script></body></html>',
])
def test_rifiuta_login_abstract_e_javascript(html):
    with pytest.raises(ValueError, match="full-text"):
        web.da_html(html, URL)


def test_doi_bibliografia_non_usato(monkeypatch, conf):
    monkeypatch.setattr(web, "scarica", lambda *a, **k: (pagina(meta=False).encode(), URL, "text/html"))
    ricevuti = []
    def metadati(doi, **kwargs):
        ricevuti.append(doi)
        return Metadati()
    monkeypatch.setattr("src.ingest.metadata.recupera", metadati)
    ingest(URL, conf, dry_run=True)
    assert ricevuti == [None]


def test_url_dry_run_non_scrive_non_scarica_figure(monkeypatch, png, conf):
    chiamate = mock_rete(monkeypatch, png)
    monkeypatch.setattr("src.ingest.metadata.recupera", lambda *a, **k: Metadati(doi="10.1234/actual", pagine="10-20"))
    esito = ingest(URL, conf, dry_run=True)
    assert esito.nota is None and not conf.note.exists()
    assert chiamate == [URL]


def test_pipeline_approvazione_pubblicazione_originali(monkeypatch, png, conf, nota):
    chiamate = mock_rete(monkeypatch, png)
    monkeypatch.setattr("src.ingest.metadata.recupera", lambda *a, **k: Metadati(doi="10.1234/actual"))
    estrattore = EstrattoreFinto(nota)
    esito = ingest(URL, conf, estrattore=estrattore)
    assert QUOTE in estrattore.testo_ricevuto
    assert not any("unused" in c or "thumb" in c for c in chiamate)
    salvato = json.loads(esito.json.read_text())
    assert salvato["pdf"] == URL  # campo legacy preservato
    assert salvato["sorgente"]["url"] == URL and salvato["sorgente"]["sha256_html"]
    f = salvato["figure"][0]
    assert (conf.note / f["percorso"]).read_bytes() == png
    assert "pagina_pdf" not in f and "bbox" not in f
    assert PENDENTI in render_da_file(esito.json)
    with pytest.raises(PubblicazioneFallita):
        pubblica_file(VaultFinto(), esito.nota)
    figure_cli([str(esito.json), "--note", str(conf.note), "--approva", "1"])
    md = render_da_file(esito.json)
    assert "dal full-text web" in md and "![Figura 1](assets/" in md and PENDENTI not in md
    vault = VaultFinto()
    pubblica_file(vault, esito.nota, cancella=False)
    assert vault.files[f["percorso"]] == png


def test_riuso_doi_usa_snapshot_non_la_pagina_cambiata(monkeypatch, png, conf, nota):
    mock_rete(monkeypatch, png)
    monkeypatch.setattr("src.ingest.metadata.recupera", lambda *a, **k: Metadati(doi="10.1234/actual"))
    esito = ingest(URL, conf, estrattore=EstrattoreFinto(nota))
    dati = json.loads(esito.json.read_text())
    del dati["figure"]  # interruzione prima di estrarre le figure
    esito.json.write_text(json.dumps(dati))
    def fetch(url, **kwargs):
        return (pagina().replace(QUOTE, "Text changed.").encode(), URL, "text/html") if url == URL else (png, url, "image/png")
    monkeypatch.setattr(web, "scarica", fetch)
    esito2 = ingest(URL, conf)  # nessuna chiave LLM disponibile, ma non serve
    assert json.loads(esito2.json.read_text())["figure"][0]["stato"] == "da_verificare"


def test_download_fallito_conserva_json_e_riprova_senza_llm(monkeypatch, png, conf, nota):
    def fetch(url, **kwargs):
        if url == URL:
            return pagina().encode(), URL, "text/html"
        raise ValueError("temporarily unavailable")
    monkeypatch.setattr(web, "scarica", fetch)
    monkeypatch.setattr("src.ingest.metadata.recupera", lambda *a, **k: Metadati(doi="10.1234/actual"))
    esito = ingest(URL, conf, estrattore=EstrattoreFinto(nota))
    assert esito.json.exists() and PENDENTI in esito.nota.read_text()
    mock_rete(monkeypatch, png)
    figure_cli([str(esito.json), "--note", str(conf.note), "--riprova"])
    assert json.loads(esito.json.read_text())["figure"][0]["stato"] == "da_verificare"


def test_citazione_inventata_non_scarica_nulla(monkeypatch, doc, nota, tmp_path):
    nota["figure_richieste"][0]["passaggio_fonte"] = "This is an invented explanation concerning Figure 1."
    monkeypatch.setattr(web, "scarica", lambda *a, **k: pytest.fail("no download"))
    f, warnings = prepara(nota, doc, render(nota, Metadati(), pdf=URL), tmp_path, "paper")
    assert f[0]["stato"] == "non_disponibile" and warnings


def test_figure_non_selezionate_nessun_download(monkeypatch, doc, nota, tmp_path):
    nota["figure_richieste"] = []
    monkeypatch.setattr(web, "scarica", lambda *a, **k: pytest.fail("no download"))
    assert prepara(nota, doc, render(nota, Metadati(), pdf=URL), tmp_path, "paper") == ([], [])


def test_numero_ambiguo_e_multipannello_segnalati(nota, tmp_path):
    figure = '<figure><img src="a.png"><figcaption>Figure 1. Caption.</figcaption></figure>'
    doc = web.da_html(pagina(figure + figure.replace('a.png', 'b.png')), URL)
    result, _ = prepara(nota, doc, render(nota, Metadati(), pdf=URL), tmp_path, "paper")
    assert result[0]["stato"] == "non_disponibile"
    doc = web.da_html(pagina(figure.replace('<img src="a.png">', '<img src="a.png"><img src="b.png">')), URL)
    result, _ = prepara(nota, doc, render(nota, Metadati(), pdf=URL), tmp_path, "paper")
    assert "pannelli separati" in result[0]["problema"]


def test_frontiers_wrapper_lazy_srcset_e_url_relativi():
    figura = '''<div class="ArticleFigure" id="F1"><span>Figure 1</span><button><figure>
    <picture><source srcset="//cdn.example/original.webp"><img src="placeholder.png" data-src="/full.png"
    srcset="small.png 400w, large.png 1200w"></picture><figcaption>Caption without label.</figcaption>
    </figure></button></div>'''
    doc = web.da_html(pagina(figura), URL)
    cat = doc.sorgente_web["figure"]["1"]
    assert len(cat) == 1
    assert cat[0]["url_immagini"][:3] == ["https://publisher.example/full.png", "https://cdn.example/original.webp", "https://publisher.example/articles/paper/large.png"]


@pytest.mark.parametrize("payload", [b'<html>Login</html>', b'\x89PNG\r\n\x1a\ncorrupt'])
def test_risposta_immagine_non_valida_bloccata(monkeypatch, payload, tmp_path):
    monkeypatch.setattr(web, "scarica", lambda *a, **k: (payload, URL, "image/png"))
    with pytest.raises(ValueError):
        web.salva_immagine({"numero": "1", "url_immagini": [URL]}, tmp_path, "paper")
    assert not list(tmp_path.rglob("*.png"))


class Response:
    def __init__(self, data=b'hello', mime="text/html", status=200, location=None):
        self.data, self.status_code = data, status
        self.headers = {"Content-Type": mime}
        if location:
            self.headers["Location"] = location
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def raise_for_status(self): pass
    def iter_content(self, _): yield self.data


def test_redirect_risoluzione_relativa_e_limiti(monkeypatch):
    monkeypatch.setattr(web, "_valida_url", lambda u: None)
    responses = iter([Response(status=302, location="/final"), Response()])
    monkeypatch.setattr(web.requests.Session, "get", lambda self, *a, **k: next(responses))
    assert web.scarica(URL, limite=20, tipi=("text/html",)) == (b'hello', 'https://publisher.example/final', 'text/html')
    monkeypatch.setattr(web.requests.Session, "get", lambda self, *a, **k: Response())
    with pytest.raises(ValueError, match="dimensione"):
        web.scarica(URL, limite=4, tipi=("text/html",))
    with pytest.raises(ValueError, match="supportato"):
        web.scarica(URL, limite=20, tipi=("image/png",))


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:pass@example.com/x", "http://127.0.0.1/x", "http://[::1]/x"])
def test_blocca_schemi_credenziali_e_indirizzi_privati(url):
    with pytest.raises(ValueError):
        web._valida_url(url)


def test_redirect_rivalidato_e_tls_non_degradato(monkeypatch):
    monkeypatch.setattr(web, "_valida_url", lambda u: (_ for _ in ()).throw(ValueError("privato")) if '127.0.0.1' in u else None)
    monkeypatch.setattr(web.requests.Session, "get", lambda self, *a, **k: Response(status=302, location="https://127.0.0.1/x"))
    with pytest.raises(ValueError, match="privato"):
        web.scarica(URL, limite=20, tipi=("text/html",))
    monkeypatch.setattr(web.requests.Session, "get", lambda self, *a, **k: Response(status=302, location="http://publisher.example/x"))
    with pytest.raises(ValueError, match="HTTPS"):
        web.scarica(URL, limite=20, tipi=("text/html",))


def test_cli_accetta_url_senza_trasformarlo_in_path(monkeypatch):
    from src import ingest as modulo
    ricevuti = []
    monkeypatch.setattr(modulo, "ingest", lambda url, *a, **k: ricevuti.append(url))
    monkeypatch.setattr(modulo, "_riepilogo", lambda *a, **k: "ok")
    assert modulo.main([URL, "--dry-run"]) == 0
    assert ricevuti == [URL]


@pytest.mark.parametrize("formato,estensione,mime", [("JPEG", "jpg", "image/jpeg"), ("WEBP", "webp", "image/webp")])
def test_originali_jpeg_webp_pubblicabili_e_hash_controllato(monkeypatch, tmp_path, formato, estensione, mime):
    stream = io.BytesIO()
    Image.new("RGB", (100, 90), "white").save(stream, formato)
    dati = stream.getvalue()
    monkeypatch.setattr(web, "scarica", lambda *a, **k: (dati, URL, mime))
    f = {"numero": "1", "url_immagini": [URL]}
    web.salva_immagine(f, tmp_path, "paper")
    p = tmp_path / f["percorso"]
    assert p.suffix == "." + estensione and p.read_bytes() == dati
    nota = tmp_path / "paper.md"
    nota.write_text(f"# Paper\n\n## Section\n\n![Figura 1]({f['percorso']})\n")
    vault = VaultFinto()
    pubblica_file(vault, nota, cancella=False)
    assert vault.files[f["percorso"]] == dati
    p.write_bytes(dati + b"changed")
    with pytest.raises(PubblicazioneFallita, match="modificato"):
        pubblica_file(VaultFinto(), nota)


def test_url_non_html_errore_prima_del_modello(monkeypatch, conf):
    monkeypatch.setattr(web, "_valida_url", lambda *a: None)
    monkeypatch.setattr(web.requests.Session, "get", lambda self, *a, **k: Response(b'%PDF-1.7', "application/pdf"))
    with pytest.raises(ValueError, match="full-text HTML"):
        ingest(URL, conf)
    assert not conf.note.exists()


def test_didascalia_non_numerata_non_giustifica_selezione(nota, tmp_path):
    testo = "The explanation concerning Figure 1 appears only inside this caption."
    fig = f'<figure id="F1"><img src="x.png"><figcaption>{testo}</figcaption></figure>'
    doc = web.da_html(pagina(fig), URL)
    nota["figure_richieste"][0]["passaggio_fonte"] = testo
    result, _ = prepara(nota, doc, render(nota, Metadati(), pdf=URL), tmp_path, "paper")
    assert result[0]["stato"] == "non_disponibile" and not result[0].get("selezione_verificata")


def test_json_senza_doi_non_rompe_ricerca_cache(tmp_path):
    from src.ingest import _gia_elaborato
    (tmp_path / "missing.json").write_text(json.dumps({"metadati": {"doi": None}}))
    assert _gia_elaborato(tmp_path, "10.1234/actual") is None


def test_download_non_usa_credenziali_ambiente(monkeypatch):
    monkeypatch.setattr(web, "_valida_url", lambda *a: None)
    def get(session, url, **kwargs):
        assert session.trust_env is False
        assert kwargs["allow_redirects"] is False
        return Response()
    monkeypatch.setattr(web.requests.Session, "get", get)
    assert web.scarica(URL, limite=20, tipi=("text/html",))[0] == b"hello"
