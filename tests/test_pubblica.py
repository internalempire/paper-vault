"""Verifiche su src/pubblica.py, con un vault simulato.

Nessun test tocca la rete. Quello che si verifica è la regola che governa il modulo: una nota
già pubblicata **non viene mai sovrascritta per intero**, perché il corpo appartiene al server
dal momento in cui qualcuno la apre e la corregge.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.pubblica import (  # noqa: E402
    PubblicazioneFallita,
    Vault,
    fondi,
    pubblica_file,
    separa,
)

NOTA = """# Chetoacidosi

*Palmer BF, Clegg DJ — Am J Kidney Dis, 2026*

## Argomento

Testo della nota.

#chetoacidosi #DKA
"""


class VaultFinto(Vault):
    """Tiene le note in memoria e registra le chiamate, invece di parlare con la rete."""

    def __init__(self, contenuti: dict[str, str] | None = None):
        super().__init__(base_url="http://finto", utente="u", password="p")
        self.contenuti = dict(contenuti or {})
        self.chiamate: list[str] = []

    def leggi(self, titolo):
        self.chiamate.append(f"leggi {titolo}")
        return self.contenuti.get(titolo)

    def crea(self, titolo, contenuto):
        self.chiamate.append(f"crea {titolo}")
        self.contenuti[titolo] = contenuto

    def aggiorna(self, titolo, contenuto):
        self.chiamate.append(f"aggiorna {titolo}")
        self.contenuti[titolo] = contenuto


# -- scomposizione -------------------------------------------------------------------------


def test_separa_le_tre_parti() -> None:
    p = separa(NOTA)
    assert p.testa == "# Chetoacidosi\n\n*Palmer BF, Clegg DJ — Am J Kidney Dis, 2026*"
    assert p.etichette == "#chetoacidosi #DKA"
    assert "## Argomento" in p.corpo and "#chetoacidosi" not in p.corpo


def test_una_nota_senza_citazione_ha_solo_il_titolo_in_testa() -> None:
    p = separa("# Titolo\n\n## Argomento\n\nTesto.\n")
    assert p.testa == "# Titolo"
    assert p.corpo.startswith("## Argomento")


def test_un_corsivo_nel_corpo_non_e_una_citazione() -> None:
    """Solo la riga subito sotto il titolo, e solo se è tutta in corsivo."""
    p = separa("# Titolo\n\n## Argomento\n\n*Una frase in corsivo.*\n")
    assert p.testa == "# Titolo"
    assert "*Una frase in corsivo.*" in p.corpo


def test_un_titolo_non_e_una_riga_di_etichette() -> None:
    """`# Titolo` ha lo spazio dopo il cancelletto, `#tag` no."""
    assert separa("---\na: b\n---\n\n# Solo un titolo\n").etichette == ""


def test_ricomposizione_fedele() -> None:
    assert separa(NOTA).unisci() == NOTA


def test_nota_senza_etichette() -> None:
    p = separa("# Titolo\n\nTesto.\n")
    assert p.etichette == ""
    assert p.unisci() == "# Titolo\n\nTesto.\n"


# -- la fusione chirurgica -------------------------------------------------------------------


def test_la_fusione_conserva_le_correzioni_fatte_sul_server() -> None:
    """È la ragione d'essere del modulo: il corpo appartiene a chi ha aperto la nota."""
    sul_server = NOTA.replace("Testo della nota.", "Testo corretto a mano dal telefono.")
    locale = NOTA.replace("#chetoacidosi #DKA", "#chetoacidosi #chetoacidosi-diabetica")

    fusa = fondi(sul_server, locale)
    assert "Testo corretto a mano dal telefono." in fusa  # il corpo del server sopravvive
    assert "#chetoacidosi-diabetica" in fusa  # i tag riallineati arrivano
    assert "#DKA" not in fusa  # il tag vecchio se ne va


def test_la_fusione_non_tocca_il_corpo_se_i_tag_non_cambiano() -> None:
    sul_server = NOTA.replace("Testo della nota.", "Aggiunta mia.")
    assert fondi(sul_server, NOTA) == sul_server


# -- pubblicazione ----------------------------------------------------------------------------


def test_una_nota_nuova_viene_creata() -> None:
    v = VaultFinto()
    assert v.pubblica("chetoacidosi", NOTA) == "creata"
    assert v.contenuti["chetoacidosi"] == NOTA


def test_una_nota_gia_presente_non_viene_sovrascritta() -> None:
    sul_server = NOTA.replace("Testo della nota.", "Correzione fatta dall'app.")
    v = VaultFinto({"chetoacidosi": sul_server})
    nuovi_tag = NOTA.replace("#chetoacidosi #DKA", "#chetoacidosi #chetoacidosi-diabetica")

    assert "aggiornata" in v.pubblica("chetoacidosi", nuovi_tag)
    assert "Correzione fatta dall'app." in v.contenuti["chetoacidosi"]
    assert "chetoacidosi-diabetica" in v.contenuti["chetoacidosi"]


def test_ripubblicare_la_stessa_nota_non_costa_niente() -> None:
    v = VaultFinto({"chetoacidosi": NOTA})
    assert v.pubblica("chetoacidosi", NOTA) == "invariata"
    assert not [c for c in v.chiamate if c.startswith(("crea", "aggiorna"))]


def test_la_verifica_precede_la_cancellazione(tmp_path: Path) -> None:
    """Se il vault non restituisce la nota, la copia locale resta: è l'unico modo di perderla."""

    class VaultSmemorato(VaultFinto):
        def crea(self, titolo, contenuto):
            self.chiamate.append(f"crea {titolo}")  # accetta e non conserva

    percorso = tmp_path / "chetoacidosi.md"
    percorso.write_text(NOTA, encoding="utf-8")
    with pytest.raises(PubblicazioneFallita, match="non va cancellata"):
        pubblica_file(VaultSmemorato(), percorso, cancella=True)
    assert percorso.exists()


def test_la_cancellazione_avviene_dopo_la_conferma(tmp_path: Path) -> None:
    percorso = tmp_path / "chetoacidosi.md"
    percorso.write_text(NOTA, encoding="utf-8")
    v = VaultFinto()
    assert pubblica_file(v, percorso, cancella=True) == "creata"
    assert not percorso.exists()
    assert v.contenuti["chetoacidosi"] == NOTA


def test_senza_cancella_la_copia_locale_resta(tmp_path: Path) -> None:
    percorso = tmp_path / "chetoacidosi.md"
    percorso.write_text(NOTA, encoding="utf-8")
    pubblica_file(VaultFinto(), percorso)
    assert percorso.exists()


# -- configurazione ---------------------------------------------------------------------------


def test_variabili_mancanti_dicono_quali(monkeypatch) -> None:
    for v in ("VAULT_URL", "VAULT_UTENTE", "VAULT_PASSWORD"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(PubblicazioneFallita, match=r"VAULT_URL.*\.env"):
        Vault.da_ambiente()


def test_configurazione_dall_ambiente(monkeypatch) -> None:
    monkeypatch.setenv("VAULT_URL", "https://nicmaf-vault.duckdns.org/")
    monkeypatch.setenv("VAULT_UTENTE", "nicola")
    monkeypatch.setenv("VAULT_PASSWORD", "segreta")
    v = Vault.da_ambiente()
    assert v.base_url == "https://nicmaf-vault.duckdns.org"  # senza barra finale


def test_la_sovrascrittura_esplicita_rimpiazza_anche_il_corpo() -> None:
    """Serve quando cambia la **struttura** della nota: lì la fusione conserverebbe il corpo
    vecchio, e il corpo vecchio è il problema."""
    vecchia_struttura = "---\ntitle: x\n---\n\n# Chetoacidosi\n\nTesto vecchio.\n"
    v = VaultFinto({"chetoacidosi": vecchia_struttura})
    assert v.pubblica("chetoacidosi", NOTA, sovrascrivi=True) == "sovrascritta per intero"
    assert v.contenuti["chetoacidosi"] == NOTA
    assert "---" not in v.contenuti["chetoacidosi"]


def test_la_sovrascrittura_non_scrive_se_e_gia_uguale() -> None:
    v = VaultFinto({"chetoacidosi": NOTA})
    assert v.pubblica("chetoacidosi", NOTA, sovrascrivi=True) == "invariata"
    assert not [c for c in v.chiamate if c.startswith("aggiorna")]


def test_il_comando_pubblica_tutte_le_note(tmp_path: Path, monkeypatch) -> None:
    from src import pubblica as modulo

    cartella = tmp_path / "note"
    (cartella / ".json").mkdir(parents=True)
    for nome in ("a", "b"):
        (cartella / f"{nome}.md").write_text(NOTA, encoding="utf-8")

    v = VaultFinto()
    monkeypatch.setattr(modulo.Vault, "da_ambiente", classmethod(lambda cls: v))
    monkeypatch.setattr("src.ingest.Configurazione.carica",
                        classmethod(lambda cls, p=None: __import__("src.ingest", fromlist=["x"])
                                    .Configurazione(note=cartella, json=cartella / ".json")))
    assert modulo.main([]) == 0
    assert {"a", "b", "index"} <= set(v.contenuti)
    assert not list(cartella.glob("*.md")), "le copie locali vanno cancellate dopo la conferma"


def test_il_comando_segnala_i_fallimenti(tmp_path: Path, monkeypatch) -> None:
    from src import pubblica as modulo

    cartella = tmp_path / "note"
    (cartella / ".json").mkdir(parents=True)
    (cartella / "a.md").write_text(NOTA, encoding="utf-8")

    class VaultRotto(VaultFinto):
        def crea(self, titolo, contenuto):
            raise PubblicazioneFallita("simulato")

    monkeypatch.setattr(modulo.Vault, "da_ambiente", classmethod(lambda cls: VaultRotto()))
    monkeypatch.setattr("src.ingest.Configurazione.carica",
                        classmethod(lambda cls, p=None: __import__("src.ingest", fromlist=["x"])
                                    .Configurazione(note=cartella, json=cartella / ".json")))
    assert modulo.main([]) == 1
    assert (cartella / "a.md").exists(), "una nota non pubblicata non va cancellata"
