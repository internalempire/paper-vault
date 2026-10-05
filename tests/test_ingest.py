"""Verifiche su src/ingest.py: la pipeline intera, con il modello simulato.

Le chiamate al modello e a CrossRef sono sostituite; tutto il resto — estrazione dal PDF vero,
ricerca del DOI, scrittura dei file, spostamento in archivio — gira per davvero.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import ingest as modulo  # noqa: E402
from src.ingest import Configurazione, ingest, vocabolario  # noqa: E402
from src.llm import Uso  # noqa: E402
from src.metadata import Metadati  # noqa: E402
from tests.fixtures.make_fixture_pdf import costruisci  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

METADATI = Metadati(
    autori=["Palmer BF", "Clegg DJ"],
    titolo="Pathophysiology of Ketoacidosis: Core Curriculum 2026",
    rivista="American Journal of Kidney Diseases", rivista_abbreviata="Am J Kidney Dis",
    anno=2026, volume="88", fascicolo="2", pagine="301-314",
    doi="10.1053/j.ajkd.2026.02.646",
)


class EstrattoreFinto:
    """Restituisce sempre la stessa nota, e registra cosa gli è arrivato."""

    def __init__(self, nota: dict, tipo: str | None = None):
        self.nota = nota
        self.tipo = tipo or nota["meta"]["tipo"]
        self.testo_ricevuto = ""
        self.vocabolario_ricevuto: list[str] = []

    def classifica(self, prompt, testo):
        return self.tipo, None, Uso("claude-haiku-4-5", 2000, 30)

    def estrai(self, prompt, testo, *, vocabolario):
        self.testo_ricevuto = testo
        self.vocabolario_ricevuto = vocabolario
        return type("R", (), {"nota": self.nota, "uso": Uso("claude-opus-5", 17000, 3000),
                              "riparazioni": 0})()


@pytest.fixture
def nota() -> dict:
    return json.loads((FIXTURES / "nota-review.json").read_text(encoding="utf-8"))


@pytest.fixture
def configurazione(tmp_path: Path) -> Configurazione:
    return Configurazione(
        note=tmp_path / "note",
        json=tmp_path / "note" / ".json",
        tags=tmp_path / "tags.md",
    )


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    """Una copia del fixture: la pipeline sposta l'originale in archivio."""
    copia = tmp_path / "in-arrivo" / "paper.pdf"
    copia.parent.mkdir(parents=True)
    shutil.copy(costruisci(), copia)
    return copia


@pytest.fixture(autouse=True)
def crossref(monkeypatch):
    monkeypatch.setattr(modulo.metadata, "recupera", lambda *a, **k: METADATI)


def esegui(pdf, configurazione, nota, **kwargs):
    estrattore = EstrattoreFinto(nota)
    esito = ingest(pdf, configurazione, estrattore=estrattore, data_nota="2026-08-24", **kwargs)
    return esito, estrattore


# -- percorso completo -------------------------------------------------------------------


def test_produce_nota_e_json(pdf, configurazione, nota) -> None:
    esito, _ = esegui(pdf, configurazione, nota)
    assert esito.nota.is_file() and esito.json.is_file()
    assert esito.nota.read_text(encoding="utf-8").startswith("# ")


def test_il_documento_non_viene_ne_spostato_ne_copiato(pdf, configurazione, nota) -> None:
    """Una sola copia del PDF, nella posizione decisa dall'utente. Un archivio parallelo
    significherebbe due copie che divergono appena una viene annotata."""
    prima = pdf.read_bytes()
    esito, _ = esegui(pdf, configurazione, nota)
    assert pdf.is_file() and pdf.read_bytes() == prima
    assert esito.documento_sorgente == pdf.resolve()
    altrove = [p for p in configurazione.note.rglob("*") if p.suffix in (".pdf", ".html")]
    assert not altrove, f"copie inattese: {altrove}"


def test_il_nome_esce_dal_titolo_italiano(pdf, configurazione, nota) -> None:
    """CrossRef dà il titolo originale; la nota vive in un vault italiano."""
    esito, _ = esegui(pdf, configurazione, nota)
    assert esito.slug == "2026-palmer-resistenza-diuretica-sindrome-cardiorenale"
    assert "ketoacidosis" not in esito.slug


def test_il_json_registra_dove_sta_il_documento(pdf, configurazione, nota) -> None:
    """Il percorso del PDF non è più nella nota — da remoto non è comunque un collegamento —
    ma resta nel JSON, che è ciò che permette di rigenerare e di risalire alla fonte."""
    esito, _ = esegui(pdf, configurazione, nota)
    salvato = json.loads(esito.json.read_text(encoding="utf-8"))
    assert salvato["pdf"] == str(pdf.resolve())
    assert str(pdf.resolve()) not in esito.nota.read_text(encoding="utf-8")


def test_il_json_salvato_rigenera_la_nota(pdf, configurazione, nota) -> None:
    """Criterio di accettazione 5: il JSON basta, senza richiamare l'API."""
    from src.render import render_da_file

    esito, _ = esegui(pdf, configurazione, nota)
    assert render_da_file(esito.json) == esito.nota.read_text(encoding="utf-8")


def test_i_metadati_non_vengono_dal_modello(pdf, configurazione, nota) -> None:
    salvato = json.loads(esegui(pdf, configurazione, nota)[0].json.read_text(encoding="utf-8"))
    assert salvato["metadati"]["autori"] == ["Palmer BF", "Clegg DJ"]
    assert "autori" not in salvato["nota"]["meta"]


def test_il_modello_riceve_il_testo_con_i_numeri_stampati(pdf, configurazione, nota) -> None:
    _, estrattore = esegui(pdf, configurazione, nota)
    assert "[[pagina 301]]" in estrattore.testo_ricevuto
    assert "[[pagina 1]]" not in estrattore.testo_ricevuto


def test_il_costo_somma_le_due_chiamate(pdf, configurazione, nota) -> None:
    esito, _ = esegui(pdf, configurazione, nota)
    assert esito.uso.token_ingresso == 19_000
    assert esito.uso.modello == "misto"


# -- idempotenza -------------------------------------------------------------------------


def test_rilanciare_non_spende_una_seconda_chiamata(pdf, configurazione, nota, tmp_path) -> None:
    """Criterio di accettazione 4."""
    esegui(pdf, configurazione, nota)

    class Esplode:
        def classifica(self, *a, **k):
            raise AssertionError("non deve chiamare l'API")

        estrai = classifica

    esito = ingest(pdf, configurazione, estrattore=Esplode(), data_nota="2026-08-24")
    assert esito.nota.is_file()


def test_forza_rielabora(pdf, configurazione, nota, tmp_path) -> None:
    esegui(pdf, configurazione, nota)
    _, estrattore = esegui(pdf, configurazione, nota, forza=True)
    assert estrattore.testo_ricevuto, "con --forza la chiamata deve partire"


# -- dry run -----------------------------------------------------------------------------


def test_dry_run_non_chiama_e_non_scrive(pdf, configurazione, nota) -> None:
    esito = ingest(pdf, configurazione, estrattore=None, dry_run=True)
    assert esito.nota is None and esito.json is None
    assert pdf.exists()
    assert esito.metadati.doi == METADATI.doi
    assert esito.documento.token_stimati > 0


# -- avvisi ------------------------------------------------------------------------------


def test_disaccordo_fra_classificazione_ed_estrazione_segnalato(pdf, configurazione, nota) -> None:
    """Il classificatore vede solo l'inizio: non è un errore, ma il tipo è dubbio."""
    estrattore = EstrattoreFinto(nota, tipo="linea guida")
    esito = ingest(pdf, configurazione, estrattore=estrattore, data_nota="2026-08-24")
    assert any("vale la pena rileggere" in a for a in esito.avvisi)


def test_tag_nuovi_segnalati(pdf, configurazione, nota) -> None:
    esito, _ = esegui(pdf, configurazione, nota)
    assert any("VExUS" in a for a in esito.avvisi)


def test_metadati_mancanti_segnalati(pdf, configurazione, nota, monkeypatch) -> None:
    monkeypatch.setattr(modulo.metadata, "recupera", lambda *a, **k: Metadati(doi="10.1/x"))
    esito, _ = esegui(pdf, configurazione, nota)
    assert any("da completare a mano" in a for a in esito.avvisi)


def test_documento_troppo_lungo_segnalato(pdf, configurazione, nota) -> None:
    configurazione.soglia_token_avviso = 10
    esito, _ = esegui(pdf, configurazione, nota)
    assert any("taglio per sezioni" in a for a in esito.avvisi)


def test_un_fallimento_lascia_il_pdf_dov_era(pdf, configurazione, nota) -> None:
    """Un documento che ha fatto fallire la pipeline si deve poter ridare in pasto."""

    class Rotto:
        def classifica(self, *a, **k):
            return "narrative review", None, Uso()

        def estrai(self, *a, **k):
            raise RuntimeError("il modello ha rifiutato")

    with pytest.raises(RuntimeError):
        ingest(pdf, configurazione, estrattore=Rotto())
    assert pdf.exists()


# -- vocabolario e configurazione --------------------------------------------------------


def test_vocabolario_letto_dal_filesystem(tmp_path: Path) -> None:
    percorso = tmp_path / "tags.md"
    percorso.write_text("# Tag\n\n- sepsi\n- shock-settico\n\n- terapia-intensiva\n", "utf-8")
    assert vocabolario(percorso) == ["sepsi", "shock-settico", "terapia-intensiva"]


def test_senza_tags_md_il_vocabolario_e_vuoto(pdf, configurazione, nota) -> None:
    _, estrattore = esegui(pdf, configurazione, nota)
    assert estrattore.vocabolario_ricevuto == []


def test_configurazione_dall_esempio() -> None:
    """config.example.toml deve restare leggibile dal codice che lo documenta."""
    c = Configurazione.carica(Path(__file__).resolve().parents[1] / "config.example.toml")
    assert c.fornitore in ("anthropic", "openai")
    assert c.soglia_token_avviso == 150_000


def test_senza_config_valori_predefiniti(tmp_path: Path) -> None:
    c = Configurazione.carica(tmp_path / "non-esiste.toml")
    assert c.fornitore == "anthropic"


# -- scelta del fornitore ------------------------------------------------------------------


@pytest.fixture
def chiavi(monkeypatch):
    """Chiavi finte: `crea_estrattore` costruisce il client, che senza chiave rifiuta."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-finta")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-finta")


def test_il_fornitore_determina_l_implementazione(configurazione, chiavi) -> None:
    from src.ingest import crea_estrattore
    from src.llm_anthropic import EstrattoreAnthropic
    from src.llm_openai import EstrattoreOpenAI

    configurazione.fornitore = "openai"
    assert isinstance(crea_estrattore(configurazione), EstrattoreOpenAI)
    configurazione.fornitore = "anthropic"
    assert isinstance(crea_estrattore(configurazione), EstrattoreAnthropic)


def test_modelli_vuoti_prendono_i_predefiniti_del_fornitore(configurazione, chiavi) -> None:
    """Non fissare qui una scelta ancora aperta (HANDOVER §10.3)."""
    from src.ingest import crea_estrattore
    from src.llm_openai import MODELLO_ESTRAZIONE

    configurazione.fornitore = "openai"
    assert crea_estrattore(configurazione).modello_estrazione == MODELLO_ESTRAZIONE


def test_il_modello_configurato_vince(configurazione, chiavi) -> None:
    from src.ingest import crea_estrattore

    configurazione.fornitore = "openai"
    configurazione.modello_estrazione = "un-modello-scelto-a-mano"
    assert crea_estrattore(configurazione).modello_estrazione == "un-modello-scelto-a-mano"


def test_fornitore_sconosciuto(configurazione) -> None:
    from src.ingest import crea_estrattore

    configurazione.fornitore = "acme"
    with pytest.raises(ValueError, match="sconosciuto"):
        crea_estrattore(configurazione)


# -- la chiave -----------------------------------------------------------------------------


def test_il_env_non_puo_finire_in_un_repo() -> None:
    """L'unico modo in cui un .env fa danno è essere versionato."""
    radice = Path(__file__).resolve().parents[1]
    righe = (radice / ".gitignore").read_text(encoding="utf-8").split()
    assert ".env" in righe


def test_l_esempio_non_contiene_una_chiave_vera() -> None:
    esempio = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY" in esempio
    assert "sk-ant-..." in esempio  # il segnaposto, non una chiave


def test_la_chiave_non_e_un_campo_di_configurazione() -> None:
    """config.toml è fatto per essere letto e confrontato; la chiave non ci deve passare."""
    esempio = (Path(__file__).resolve().parents[1] / "config.example.toml").read_text("utf-8")
    assert "ANTHROPIC_API_KEY" not in esempio
    assert "api_key" not in esempio


def test_chiave_mancante_dice_dove_metterla(configurazione, monkeypatch) -> None:
    from src.ingest import ChiaveMancante, crea_estrattore

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    configurazione.fornitore = "openai"
    with pytest.raises(ChiaveMancante, match=r"OPENAI_API_KEY.*\.env"):
        crea_estrattore(configurazione)


def test_percorso_relativo_ancorato_al_progetto(tmp_path: Path) -> None:
    """`note = "note"` deve indicare la stessa cartella da qualunque directory si lanci."""
    conf = tmp_path / "config.toml"
    conf.write_text('[percorsi]\nnote = "note"\njson = "note/.json"\n', encoding="utf-8")
    c = Configurazione.carica(conf)
    radice = Path(__file__).resolve().parents[1]
    assert c.note == radice / "note"
    assert c.json == radice / "note" / ".json"


def test_percorso_assoluto_rispettato(tmp_path: Path) -> None:
    """Quando il vault si sposterà sul NAS il percorso sarà assoluto (Fase 2)."""
    conf = tmp_path / "config.toml"
    conf.write_text(f'[percorsi]\nnote = "{tmp_path}/vault"\n', encoding="utf-8")
    assert Configurazione.carica(conf).note == tmp_path / "vault"


def test_doi_di_un_altro_articolo_segnalato(pdf, configurazione, nota, monkeypatch) -> None:
    """Se il DOI fosse di un articolo citato in prima pagina, la nota uscirebbe con autori e
    citazione sbagliati e nient'altro stonerebbe."""
    sbagliati = Metadati(**{**METADATI.__dict__, "titolo": "Un articolo completamente diverso"})
    monkeypatch.setattr(modulo.metadata, "recupera", lambda *a, **k: sbagliati)
    esito, _ = esegui(pdf, configurazione, nota)
    assert any("potrebbe essere di un altro articolo" in a for a in esito.avvisi)


def test_titolo_coerente_nessun_avviso(pdf, configurazione, nota, monkeypatch) -> None:
    coerenti = Metadati(**{**METADATI.__dict__, "titolo": "Titolo a piena pagina della sezione"})
    monkeypatch.setattr(modulo.metadata, "recupera", lambda *a, **k: coerenti)
    esito, _ = esegui(pdf, configurazione, nota)
    assert not any("altro articolo" in a for a in esito.avvisi)


def test_il_vocabolario_ignora_spiegazioni_e_tabelle(tmp_path: Path) -> None:
    """`tags.md` deve poter essere documentato: solo le voci di elenco sono tag."""
    percorso = tmp_path / "tags.md"
    percorso.write_text(
        "# Vocabolario controllato\n\n"
        "Questo file viene letto dalla pipeline e passato nel prompt.\n\n"
        "## Rene\n\n- iperkaliemia\n- insufficienza-renale-acuta\n\n"
        "| Accorpato | In |\n|---|---|\n| heart-failure | scompenso-cardiaco |\n",
        encoding="utf-8",
    )
    assert vocabolario(percorso) == ["iperkaliemia", "insufficienza-renale-acuta"]


def test_il_vocabolario_prende_il_primo_token(tmp_path: Path) -> None:
    """Una voce può portarsi dietro una nota: il tag è il primo pezzo."""
    percorso = tmp_path / "tags.md"
    percorso.write_text("- VExUS — punteggio di congestione venosa\n", encoding="utf-8")
    assert vocabolario(percorso) == ["VExUS"]


# -- pubblicazione ---------------------------------------------------------------------------


class VaultFinto:
    def __init__(self, rompe: bool = False):
        self.pubblicate: dict[str, str] = {}
        self.rompe = rompe

    def leggi(self, titolo):
        return self.pubblicate.get(titolo)

    def pubblica(self, titolo, contenuto, *, sovrascrivi=False):
        if self.rompe:
            from src.pubblica import PubblicazioneFallita

            raise PubblicazioneFallita("vault irraggiungibile: simulato")
        self.pubblicate[titolo] = contenuto
        return "creata"


def test_con_pubblica_la_nota_va_sul_vault_e_sparisce_da_qui(pdf, configurazione, nota) -> None:
    v = VaultFinto()
    esito = ingest(pdf, configurazione, estrattore=EstrattoreFinto(nota),
                   data_nota="2026-08-24", vault=v)
    assert esito.pubblicata == "creata"
    assert esito.slug in v.pubblicate
    assert not (configurazione.note / f"{esito.slug}.md").exists()
    assert esito.json.is_file(), "il JSON resta: serve a rigenerare e all'idempotenza"


def test_una_pubblicazione_fallita_non_fa_perdere_la_nota(pdf, configurazione, nota) -> None:
    """La nota è costata una chiamata: se il vault non risponde resta qui, e lo si dice."""
    esito = ingest(pdf, configurazione, estrattore=EstrattoreFinto(nota),
                   data_nota="2026-08-24", vault=VaultFinto(rompe=True))
    assert (configurazione.note / f"{esito.slug}.md").is_file()
    assert any("pubblicazione fallita" in a for a in esito.avvisi)


def test_senza_vault_la_nota_resta_in_locale(pdf, configurazione, nota) -> None:
    esito, _ = esegui(pdf, configurazione, nota)
    assert esito.nota.is_file() and esito.pubblicata is None


def test_pubblicare_aggiorna_anche_l_indice(pdf, configurazione, nota, monkeypatch) -> None:
    """L'indice è la pagina di apertura del vault: se resta indietro, una nota nuova non si
    trova finché non la si cerca per nome."""
    v = VaultFinto()
    v.leggi = lambda titolo: v.pubblicate.get(titolo)
    ingest(pdf, configurazione, estrattore=EstrattoreFinto(nota), data_nota="2026-08-24", vault=v)
    assert "index" in v.pubblicate, "SilverBullet apre la pagina chiamata `index`"
    assert "# Indice" in v.pubblicate["index"]
