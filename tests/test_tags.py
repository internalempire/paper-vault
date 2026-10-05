"""Verifiche su src/tags.py: vocabolario e rassegna delle proposte.

La rassegna serve a decidere in una sola sessione invece che un paper alla volta. Perché sia
utile deve mostrare **solo** ciò che è ancora aperto: se ripropone ogni volta le stesse decisioni
già prese, si smette di guardarla.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tags import mai_usati, proposte, risolti, vocabolario  # noqa: E402

RADICE = Path(__file__).resolve().parents[1]

VOCABOLARIO = """# Vocabolario

Testo di spiegazione che non deve diventare un tag.

## Rene
- iperkaliemia
- insufficienza-renale-cronica

## Cuore
- scompenso-cardiaco

## Accorpamenti applicati

| Proposto | Confluito in | Perché |
|---|---|---|
| `heart-failure` | `scompenso-cardiaco` | i tag sono in italiano |
| `obesity`, `frailty` | `obesita`, `fragilita` | idem |
| `interstizio-polmonare` | — | troppo specifico |
"""


@pytest.fixture
def tags(tmp_path: Path) -> Path:
    percorso = tmp_path / "tags.md"
    percorso.write_text(VOCABOLARIO, encoding="utf-8")
    return percorso


def nota(cartella: Path, slug: str, *, tags: list[str], nuovi: list[str] = ()) -> None:
    cartella.mkdir(parents=True, exist_ok=True)
    (cartella / f"{slug}.json").write_text(
        json.dumps({"nota": {"meta": {"tags": tags, "tags_nuovi": list(nuovi)}}}),
        encoding="utf-8",
    )


@pytest.fixture
def note(tmp_path: Path) -> Path:
    cartella = tmp_path / "json"
    nota(cartella, "nota-a", tags=["iperkaliemia"], nuovi=["heart-failure", "sodio-urinario"])
    nota(cartella, "nota-b", tags=["iperkaliemia"], nuovi=["sodio-urinario"])
    nota(cartella, "nota-c", tags=["interstizio-polmonare"])
    return cartella


# -- vocabolario -------------------------------------------------------------------------


def test_solo_le_voci_di_elenco_sono_tag(tags: Path) -> None:
    assert vocabolario(tags) == [
        "iperkaliemia", "insufficienza-renale-cronica", "scompenso-cardiaco"
    ]


def test_vocabolario_inesistente(tmp_path: Path) -> None:
    assert vocabolario(tmp_path / "non-esiste.md") == []


# -- decisioni già prese -----------------------------------------------------------------


def test_la_tabella_e_il_registro_delle_decisioni(tags: Path) -> None:
    decisi = risolti(tags)
    assert decisi["heart-failure"] == "scompenso-cardiaco"
    assert decisi["obesity"] == "obesita fragilita"  # una riga può accorparne più d'uno
    assert decisi["interstizio-polmonare"] == "—"  # respinto senza accorpare


def test_una_proposta_gia_decisa_non_torna(tags: Path, note: Path) -> None:
    ammessi = vocabolario(tags)
    aperte = {p.tag for p in proposte(note, ammessi, risolti(tags))}
    assert aperte == {"sodio-urinario"}


def test_senza_registro_tornano_tutte(tags: Path, note: Path) -> None:
    """È il comportamento di prima: utile per capire cosa il filtro sta nascondendo."""
    aperte = {p.tag for p in proposte(note, vocabolario(tags))}
    assert aperte == {"sodio-urinario", "heart-failure", "interstizio-polmonare"}


def test_le_proposte_sono_ordinate_per_ricorrenza(tags: Path, note: Path) -> None:
    nota(note, "nota-d", tags=[], nuovi=["nuovissimo"])
    elenco = proposte(note, vocabolario(tags), risolti(tags))
    assert [p.tag for p in elenco] == ["sodio-urinario", "nuovissimo"]
    assert elenco[0].quante_volte == 2


def test_la_proposta_dice_da_quali_note_viene(tags: Path, note: Path) -> None:
    p = proposte(note, vocabolario(tags), risolti(tags))[0]
    assert p.note == ["nota-a", "nota-b"]


# -- tag simili ---------------------------------------------------------------------------


def test_i_simili_uniscono_somiglianza_e_parole_condivise(tags: Path, note: Path) -> None:
    nota(note, "nota-e", tags=[], nuovi=["iperkaliemia-farmacologica"])
    nota(note, "nota-f", tags=[], nuovi=["malattia-renale-cronica"])
    per_tag = {p.tag: p.simili for p in proposte(note, vocabolario(tags), risolti(tags))}
    assert "iperkaliemia" in per_tag["iperkaliemia-farmacologica"]  # somiglianza letterale
    assert "insufficienza-renale-cronica" in per_tag["malattia-renale-cronica"]  # parola condivisa


# -- tag mai usati -------------------------------------------------------------------------


def test_un_tag_accorpato_conta_come_usato(tags: Path, note: Path) -> None:
    """`scompenso-cardiaco` è in uso benché la nota dica ancora `heart-failure`: senza questo,
    l'elenco sarebbe solo l'altra faccia degli accorpamenti."""
    assert "scompenso-cardiaco" not in mai_usati(note, vocabolario(tags), risolti(tags))


def test_un_tag_davvero_inutilizzato_compare(tags: Path, note: Path) -> None:
    assert mai_usati(note, vocabolario(tags), risolti(tags)) == ["insufficienza-renale-cronica"]


# -- il file vero ----------------------------------------------------------------------------


def test_il_vocabolario_del_progetto_e_ben_formato() -> None:
    voci = vocabolario(RADICE / "tags.md")
    assert len(voci) > 40
    assert len(set(voci)) == len(voci), "tag duplicati"
    assert all(v.replace("-", "").isalnum() for v in voci), "forme non conformi"


def test_gli_accorpamenti_del_progetto_puntano_a_tag_esistenti() -> None:
    """Un accorpamento verso un tag che non c'è manderebbe la nota in un vicolo cieco."""
    voci = {v.lower() for v in vocabolario(RADICE / "tags.md")}
    for proposto, destinazione in risolti(RADICE / "tags.md").items():
        for pezzo in destinazione.split():
            assert pezzo == "—" or pezzo.lower() in voci, f"{proposto} → {pezzo} non esiste"


def test_il_riallineamento_promuove_anche_a_parità_di_sequenza(tags: Path, tmp_path: Path) -> None:
    """Una nota i cui tag erano tutti proposti e ora sono tutti in vocabolario ha la stessa
    sequenza ma un significato diverso: nel frontmatter cambia l'asterisco."""
    from src.tags import riallinea

    # `riallinea` rigenera il markdown, quindi il JSON dev'essere un involucro valido.
    cartella = tmp_path / "json2"
    cartella.mkdir()
    (cartella / "n.json").write_text(json.dumps({
        "nota": {"meta": {
            "title_it": "Prova", "titolo_nota": "Prova", "tipo": "narrative review",
            "criterio_riferimenti": "pagina",
            "tags": [], "tags_nuovi": ["iperkaliemia", "scompenso-cardiaco"]}},
        "metadati": {}, "pdf": "/tmp/x.pdf", "data_nota": "2026-01-01",
    }), encoding="utf-8")
    note_md = tmp_path / "note2"
    note_md.mkdir()

    cambiamenti = riallinea(cartella, vocabolario(tags), risolti(tags), note_md)
    assert len(cambiamenti) == 1
    meta = json.loads((cartella / "n.json").read_text(encoding="utf-8"))["nota"]["meta"]
    assert meta["tags"] == ["iperkaliemia", "scompenso-cardiaco"]
    assert meta["tags_nuovi"] == []
