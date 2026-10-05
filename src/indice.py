"""La pagina di apertura del vault.

SilverBullet, senza una pagina indice, apre su un editor vuoto — che con questo modello di
lavoro non serve a niente: le note le scrive la pipeline, non si scrivono a mano.

L'indice **lo genera la pipeline**, non una query dentro SilverBullet. Due ragioni: è
deterministico, e non dipende da come una versione futura interpreterà la propria sintassi di
interrogazione. Il costo è che va rigenerato quando si aggiunge una nota, ed è per questo che
`ingest --pubblica` lo fa da sé.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path

import dotenv

log = logging.getLogger(__name__)

RADICE = Path(__file__).resolve().parents[1]
# SilverBullet apre la pagina indicata da `indexPage` nella propria configurazione di server,
# che vale `index` e non è impostabile né da variabile d'ambiente né dal `CONFIG.md` dello
# spazio. La pagina si chiama quindi come il valore predefinito: nessuna configurazione da
# mantenere, e nessuna sorpresa a un aggiornamento dell'applicazione.
PAGINA = "index"


def voci(cartella_json: Path) -> list[tuple[str, str, list[str]]]:
    """Titolo, nome della pagina e tag di ogni nota, dalla più recente."""
    trovate = []
    for percorso in cartella_json.glob("*.json"):
        contenuto = json.loads(percorso.read_text(encoding="utf-8"))
        meta = contenuto["nota"]["meta"]
        tag = list(meta.get("tags") or []) + list(meta.get("tags_nuovi") or [])
        trovate.append((meta.get("titolo_nota") or percorso.stem, percorso.stem, tag))
    return sorted(trovate, key=lambda v: v[1], reverse=True)


def costruisci(cartella_json: Path) -> str:
    """La pagina indice: le note in ordine, e sotto le stesse raggruppate per argomento."""
    elenco = voci(cartella_json)
    per_tag: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for titolo, pagina, tag in elenco:
        for t in tag:
            per_tag[t].append((titolo, pagina))

    righe = [
        "# Indice",
        "",
        f"{len(elenco)} note. **Ctrl-Shift-F** per cercare nel testo, **Ctrl-K** per saltare a "
        "una nota per nome. Da telefono, le stesse funzioni stanno nella barra in alto.",
        "",
        "## Tutte le note, dalla più recente",
        "",
    ]
    righe += [f"- [[{pagina}|{titolo}]]" for titolo, pagina, _ in elenco]

    if per_tag:
        righe += ["", "## Per argomento", ""]
        for tag in sorted(per_tag):
            note = " · ".join(f"[[{pagina}|{titolo}]]" for titolo, pagina in sorted(per_tag[tag]))
            righe.append(f"**#{tag}** — {note}")
            righe.append("")
    return "\n".join(righe).rstrip() + "\n"


def pubblica_indice(vault, cartella_json: Path) -> None:
    """Manda indice e configurazione sul vault, sovrascrivendoli.

    Sono pagine generate: a differenza delle note non hanno un corpo che appartiene al server,
    quindi vanno rimpiazzate per intero a ogni giro.
    """
    vault.pubblica(PAGINA, costruisci(cartella_json), sovrascrivi=True)
    log.info("indice aggiornato sul vault")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.indice",
        description="Rigenera la pagina di apertura del vault a partire dai JSON delle note.",
    )
    parser.add_argument("--json", type=Path, help="cartella dei JSON (default: da config.toml)")
    parser.add_argument("--stampa", action="store_true", help="mostra l'indice senza inviarlo")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dotenv.load_dotenv(RADICE / ".env", override=False)
    if args.json:
        cartella = args.json.expanduser()
    else:
        from src.ingest import Configurazione

        cartella = Configurazione.carica().json

    if args.stampa:
        print(costruisci(cartella))
        return 0

    from src.pubblica import Vault

    pubblica_indice(Vault.da_ambiente(), cartella)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
