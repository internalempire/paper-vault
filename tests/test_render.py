"""Verifiche su src/render.py e sui template.

Il criterio è quello dello HANDOVER §12.3: confronto diretto contro i golden. Le note JSON di
esempio sono estratti dei tre documenti reali, non trascrizioni integrali, quindi il confronto
è **strutturale** — successione delle sezioni, forma del frontmatter, resa delle raccomandazioni
— non testuale. Il contenuto lo verificherà il confronto a occhio sulla prima nota vera.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.metadata import Metadati, slug  # noqa: E402
from src.render import pulisci_riferimenti, render  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"

PALMER = Metadati(
    autori=["Palmer BF", "Clegg DJ"],
    titolo="Pathophysiology of Ketoacidosis: Core Curriculum 2026",
    rivista="American Journal of Kidney Diseases",
    rivista_abbreviata="Am J Kidney Dis",
    anno=2026, volume="88", fascicolo="2", pagine="301-314",
    doi="10.1053/j.ajkd.2026.02.646",
)
ALETRAS = Metadati(
    autori=["Aletras G", "Bachlitzanaki M", "Stratinaki M", "Foukarakis E", "Petrakis I",
            "Pantazis Y", "Hamilos M", "Stylianou K"],
    titolo="Diuretic resistance in cardiorenal syndrome: mechanisms, monitoring and "
    "phenotype-tailored management",
    rivista="Frontiers in Cardiovascular Medicine",
    rivista_abbreviata="Front Cardiovasc Med",
    anno=2026, volume="12", pagine="1731305", doi="10.3389/fcvm.2025.1731305",
)
AHA = Metadati(
    autori=["Creager MA", "Barnes GD", "Giri J", "Piazza G", "Secemsky EA", "Weinberg I",
            "Jaff MR", "Kabrhel C", "Rosovsky RP"],
    titolo="2026 AHA/ACC/ACCP/ACEP/CHEST/SCAI/SHM/SIR/SVM/SVN Guideline for the Evaluation "
    "and Management of Acute Pulmonary Embolism in Adults",
    rivista="Circulation", rivista_abbreviata="Circulation",
    anno=2026, volume="153", pagine="e977-e1051", doi="10.1161/CIR.0000000000001415",
)


def nota(nome: str) -> dict:
    return json.loads((FIXTURES / f"{nome}.json").read_text(encoding="utf-8"))


def golden(nome: str) -> str:
    return (GOLDEN / f"{nome}.md").read_text(encoding="utf-8")


def titoli(markdown: str, livello: int = 2) -> list[str]:
    """I titoli, senza i rimandi alla fonte.

    I golden li riportano ancora, il renderer non li emette più: il confronto è sulla
    struttura, non sulla notazione dei rimandi.
    """
    trovati = re.findall(rf"^{'#' * livello} (.+)$", markdown, flags=re.MULTILINE)
    return [pulisci_riferimenti(t) for t in trovati]


def intestazione(markdown: str) -> list[str]:
    """Titolo e riga di citazione: le prime due righe utili della nota."""
    righe = [r for r in markdown.splitlines() if r.strip()]
    return righe[:2]


def e_sottosequenza(piccola: list[str], grande: list[str]) -> bool:
    iteratore = iter(grande)
    return all(v in iteratore for v in piccola)


# -- confronto con i golden --------------------------------------------------------------


def test_la_nota_comincia_da_titolo_e_citazione() -> None:
    """Niente frontmatter YAML: l'app del vault non lo interpreta e lo renderebbe come un
    blocco in grassetto in cima a ogni nota. I metadati completi restano nel JSON."""
    reso = render(nota("nota-review"), ALETRAS, pdf="library/x.pdf", data_nota="2026-08-18")
    titolo, citazione = intestazione(reso)
    assert titolo == "# Resistenza diuretica nella sindrome cardiorenale"
    assert citazione == "*Aletras G, Bachlitzanaki M, Stratinaki M, et al. — Front Cardiovasc Med, 2026*"
    assert "---" not in reso.splitlines()[0]


def test_la_citazione_breve_non_ripete_il_doi() -> None:
    """La citazione completa con il DOI sta in fondo: ripeterla in testa allunga e basta."""
    reso = render(nota("nota-linea-guida"), AHA, pdf="x", data_nota="2026-08-17")
    assert "doi:" not in intestazione(reso)[1]
    assert "doi:10.1161" in reso  # ma in fondo c'è


def test_sezioni_della_review_nellordine_del_golden() -> None:
    reso = render(nota("nota-review"), ALETRAS, pdf="library/x.pdf", data_nota="2026-08-18")
    atteso = golden("2026-aletras-resistenza-diuretica-cardiorenale")
    assert e_sottosequenza(titoli(reso), titoli(atteso))
    assert titoli(reso)[:3] == ["Argomento", "In breve", "Dosaggi e soglie"]
    assert titoli(reso)[-1] == "Riferimento bibliografico"


def test_sezioni_della_linea_guida_come_nel_golden() -> None:
    """Qui il confronto è esatto: il fixture copre tutte le sezioni del golden."""
    reso = render(nota("nota-linea-guida"), AHA, pdf="library/x.pdf", data_nota="2026-08-17")
    atteso = golden("2026-aha-acc-embolia-polmonare-acuta")
    assert titoli(reso) == titoli(atteso)


def test_le_trappole_restano_dove_le_ha_messe_il_modello() -> None:
    """`dopo_sezione` colloca le trappole dentro il corpo, come nel golden Aletras."""
    reso = render(nota("nota-review"), ALETRAS, pdf="library/x.pdf", data_nota="2026-08-18")
    sezioni = titoli(reso)
    assert sezioni.index("Fenotipo 1 — Scompenso cardiaco destro") + 1 == sezioni.index(
        "Cosa non fare — trappole terapeutiche nello scompenso destro"
    )


def test_citazione_identica_al_golden() -> None:
    riga = next(
        r for r in golden("2026-palmer-fisiopatologia-chetoacidosi").splitlines()
        if r.startswith("Palmer BF, Clegg DJ.")
    )
    assert PALMER.citazione() == riga


def test_slug_uguale_al_nome_del_golden() -> None:
    assert slug(PALMER, "Fisiopatologia della chetoacidosi") == \
        "2026-palmer-fisiopatologia-chetoacidosi"


# -- vista "Cosa non fare" ---------------------------------------------------------------


def test_la_vista_negativa_non_toglie_le_raccomandazioni_dal_corpo() -> None:
    """La duplicazione è voluta e vive nel template, non nei dati (HANDOVER §6)."""
    reso = render(nota("nota-linea-guida"), AHA, pdf="library/x.pdf", data_nota="2026-08-17")
    corpo, vista = reso.split("## Cosa non fare")
    testo = "Paziente adeguatamente anticoagulato: NON posizionare filtri di routine."
    assert testo in corpo and testo in vista


def test_la_vista_negativa_distingue_danno_da_nessun_beneficio() -> None:
    reso = render(nota("nota-linea-guida"), AHA, pdf="library/x.pdf", data_nota="2026-08-17")
    vista = reso.split("## Cosa non fare")[1]
    assert vista.index("`3: Harm`") < vista.index("`3: No Benefit`")  # prima ciò che fa male
    assert "rischio > beneficio" in vista and "beneficio = rischio" in vista


def test_senza_raccomandazioni_negative_la_vista_non_compare() -> None:
    n = nota("nota-linea-guida")
    for r in n["raccomandazioni"]:
        r["negativa"], r["categoria_negativa"] = False, None
    reso = render(n, AHA, pdf="library/x.pdf", data_nota="2026-08-17")
    assert "## Cosa non fare" not in reso


def test_grading_senza_distinzione_non_ne_inventa_una() -> None:
    """Con GRADE (forte/condizionata contro) non esistono `danno` e `nessun beneficio`."""
    n = nota("nota-linea-guida")
    for r in n["raccomandazioni"]:
        if r["negativa"]:
            r["cor"], r["categoria_negativa"] = "forte contro", None
    reso = render(n, AHA, pdf="library/x.pdf", data_nota="2026-08-17")
    vista = reso.split("## Cosa non fare")[1]
    assert "### Sconsigliati" in vista
    assert "3: Harm" not in vista


# -- dosaggi -----------------------------------------------------------------------------


def test_la_tabella_dosaggi_non_ha_la_colonna_dei_riferimenti() -> None:
    """Scelta dell'utente: i rimandi appesantiscono la lettura. Restano nel JSON."""
    reso = render(nota("nota-linea-guida"), AHA, pdf="x", data_nota="2026-08-17")
    assert "| Ambito | Parametro | Valore |" in reso
    assert "| Valore | p. |" not in reso and "| Valore | § |" not in reso


def test_nessun_rimando_alla_fonte_nella_nota_resa() -> None:
    for nome, metadati in (("nota-linea-guida", AHA), ("nota-review", ALETRAS)):
        reso = render(nota(nome), metadati, pdf="x", data_nota="2026-01-01")
        assert not re.search(r"\(p{1,2}\.\s*\w", reso), nome
        assert not re.search(r"\(§\s*\d", reso), nome


def test_i_riferimenti_restano_nel_json_di_partenza() -> None:
    """Rimetterli è una modifica al renderer più una rigenerazione, senza chiamate all'API."""
    dati = nota("nota-review")
    assert any(d.get("rif") for d in dati["dosaggi"])
    assert dati["meta"]["nota_riferimenti"]


def test_la_nota_sui_riferimenti_sparisce_con_i_riferimenti() -> None:
    """Dichiarava il criterio dei rimandi: senza rimandi non ha più niente da dichiarare."""
    reso = render(nota("nota-review"), ALETRAS, pdf="x", data_nota="2026-08-18")
    assert "Nota sui riferimenti" not in reso


def test_una_parentesi_che_non_e_un_rimando_resta() -> None:
    """`(p. 8 e non oltre)` non è un rimando: toglierla porterebbe via contenuto."""
    n = nota("nota-review")
    n["dosaggi"][0]["valore"] = "20-40 mg (dose iniziale ragionevole)"
    reso = render(n, ALETRAS, pdf="x", data_nota="2026-01-01")
    assert "20-40 mg (dose iniziale ragionevole)" in reso


def test_i_rimandi_spariscono_anche_dentro_il_testo_libero() -> None:
    n = nota("nota-review")
    n["sezioni"][0]["contenuto_md"] = "Il cloro conta più del sodio (§3.4). Vale sempre."
    reso = render(n, ALETRAS, pdf="x", data_nota="2026-01-01")
    assert "Il cloro conta più del sodio. Vale sempre." in reso


def test_ambiguita_riportata_e_non_risolta() -> None:
    reso = render(nota("nota-review"), ALETRAS, pdf="x", data_nota="2026-08-18")
    assert "`[formulazione ambigua nella fonte]`" in reso
    assert "**1-2 h**" in reso and "**2-6 h**" in reso


def test_documento_senza_dosaggi_lo_dichiara() -> None:
    reso = render(nota("nota-minima"), PALMER, pdf="x", data_nota="2026-08-16")
    assert "## Dosaggi e soglie" in reso
    assert "Nessun dosaggio o soglia operativa riportato nel documento." in reso


# -- metadati e tag ----------------------------------------------------------------------


def test_i_tag_proposti_compaiono_fra_le_etichette() -> None:
    """Nella nota non si distinguono più dai validati: la distinzione vive nel JSON e la
    sorveglia `python -m src.tags`."""
    reso = render(nota("nota-review"), ALETRAS, pdf="x", data_nota="2026-08-18")
    assert "#VExUS" in reso and "#VExUS*" not in reso


def test_metadati_mancanti_segnalati_non_inventati() -> None:
    """Se CrossRef e PubMed falliscono i campi restano vuoti e la nota lo dice (§6.3)."""
    vuoti = Metadati()
    reso = render(nota("nota-minima"), vuoti, pdf="x", data_nota="2026-08-16")
    assert "non recuperati da CrossRef né da PubMed" in reso
    assert intestazione(reso)[1].startswith("##"), "senza metadati non c'è riga di citazione"
    assert vuoti.mancanti == ["autori", "titolo", "rivista", "anno", "doi"]


# -- proprietà generali ------------------------------------------------------------------


@pytest.mark.parametrize("nome", ["nota-review", "nota-linea-guida", "nota-minima"])
def test_il_rendering_e_deterministico(nome: str) -> None:
    """Criterio di accettazione 5: il JSON basta a rigenerare la nota, sempre uguale."""
    n, m = nota(nome), PALMER
    assert render(n, m, pdf="x", data_nota="2026-01-01") == render(
        n, m, pdf="x", data_nota="2026-01-01"
    )


@pytest.mark.parametrize("nome", ["nota-review", "nota-linea-guida", "nota-minima"])
def test_nessuna_riga_vuota_di_troppo(nome: str) -> None:
    reso = render(nota(nome), PALMER, pdf="x", data_nota="2026-01-01")
    assert "\n\n\n" not in reso
    assert not any(r != r.rstrip() for r in reso.splitlines())
    assert reso.endswith("\n")


def test_un_documento_fuori_categoria_usa_il_template_review() -> None:
    reso = render(nota("nota-minima"), PALMER, pdf="x", data_nota="2026-01-01")
    assert "## Argomento" in reso and "## In breve" in reso


# -- rigenerazione dal JSON salvato ------------------------------------------------------


def test_la_nota_si_rigenera_dal_json_senza_api(tmp_path: Path) -> None:
    """Criterio di accettazione 5. I metadati CrossRef stanno nell'involucro, non nella nota:
    lo schema li vieta al modello, ma senza di loro il JSON non basterebbe a rigenerare."""
    from src.render import involucro, render_da_file, salva

    n = nota("nota-linea-guida")
    atteso = render(n, AHA, pdf="library/x.pdf", data_nota="2026-08-17")
    percorso = salva(
        tmp_path / "nota.json", involucro(n, AHA, pdf="library/x.pdf", data_nota="2026-08-17")
    )
    assert render_da_file(percorso) == atteso


def test_involucro_rifiuta_una_nota_non_conforme(tmp_path: Path) -> None:
    from src.render import involucro
    from src.schema import NotaNonValida

    n = nota("nota-review")
    n["meta"]["autori"] = "inventato"
    with pytest.raises(NotaNonValida):
        involucro(n, ALETRAS, pdf="x")


def test_il_json_salvato_di_esempio_si_rigenera() -> None:
    """Il file a cui rimanda il README deve continuare a funzionare."""
    from src.render import render_da_file

    reso = render_da_file(FIXTURES / "salvato-linea-guida.json")
    assert reso.startswith("# ")
    assert "## Cosa non fare" in reso


def test_il_tipo_rilevato_resta_nel_json() -> None:
    """Il tipo del documento non compare più nella nota: serve alla pipeline, non a chi legge."""
    reso = render(nota("nota-minima"), PALMER, pdf="x", data_nota="2026-01-01")
    assert "tipo_rilevato" not in reso and "tipo:" not in reso
    assert nota("nota-minima")["meta"]["tipo_rilevato"] == "editoriale"


def test_il_rimando_sparisce_dalla_tabella_delle_trappole() -> None:
    n = nota("nota-review")
    n["trappole"]["voci"][0]["approccio"] = "Frazionare le dosi (§4)"
    n["trappole"]["voci"][0]["rif"] = "§4"
    reso = render(n, ALETRAS, pdf="x", data_nota="2026-01-01")
    assert "Frazionare le dosi |" in reso
    assert "§4" not in reso


def test_il_rimando_sparisce_dai_valori_dei_dosaggi() -> None:
    n = nota("nota-linea-guida")
    n["dosaggi"][0]["valore"] = "500 µg/L (p. e985)"
    reso = render(n, AHA, pdf="x", data_nota="2026-01-01")
    assert "| 500 µg/L |" in reso
    assert "e985" not in reso


# -- etichette in coda ---------------------------------------------------------------------


def test_le_etichette_chiudono_la_nota() -> None:
    """L'app del vault riconosce i tag solo come `#cancelletto` nel corpo."""
    reso = render(nota("nota-review"), ALETRAS, pdf="x", data_nota="2026-01-01")
    ultima = reso.strip().splitlines()[-1]
    assert ultima.startswith("#")
    assert "#sindrome-cardiorenale" in ultima and "#VExUS" in ultima


def test_le_etichette_non_portano_l_asterisco() -> None:
    """`#VExUS*` non sarebbe riconosciuta come etichetta. La distinzione fra tag validato e
    proposto resta nel frontmatter, che è quello che legge la pipeline."""
    ultima = render(nota("nota-review"), ALETRAS, pdf="x",
                    data_nota="2026-01-01").strip().splitlines()[-1]
    assert "*" not in ultima


def test_le_etichette_comprendono_proposti_e_validati() -> None:
    n = nota("nota-review")
    attesi = len(n["meta"]["tags"]) + len(n["meta"]["tags_nuovi"])
    ultima = render(n, ALETRAS, pdf="x", data_nota="2026-01-01").strip().splitlines()[-1]
    assert len(ultima.split()) == attesi


def test_una_nota_senza_tag_non_ha_la_riga() -> None:
    n = nota("nota-minima")
    n["meta"]["tags"], n["meta"]["tags_nuovi"] = [], []
    reso = render(n, PALMER, pdf="x", data_nota="2026-01-01")
    assert not reso.strip().splitlines()[-1].startswith("#")


def test_le_etichette_stanno_dopo_la_bibliografia() -> None:
    reso = render(nota("nota-linea-guida"), AHA, pdf="x", data_nota="2026-01-01")
    assert reso.index("## Riferimento bibliografico") < reso.rindex("#embolia-polmonare")
