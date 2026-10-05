"""Copia di sicurezza del vault.

Il server è l'unica sede delle note. I JSON in locale rigenerano il **testo originale** di
qualunque nota, ma non le correzioni fatte dall'interfaccia — e quelle correzioni sono il motivo
per cui il vault è un editor e non un lettore.

Questo comando scarica il vault così com'è. Non è una sincronizzazione: è uno specchio a senso
unico, dal server verso casa, che si sovrascrive a ogni giro. Va lanciato quando fa comodo, e
diventa un prerequisito nel momento in cui qualcosa scrive nel vault senza passare dalla
pipeline — per esempio un montaggio WebDAV.

    python -m src.scarica
"""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
from pathlib import Path

import dotenv

log = logging.getLogger(__name__)

RADICE = Path(__file__).resolve().parents[1]

# SilverBullet porta con sé una libreria di pagine proprie sotto `Library/`. Non sono note: si
# ricreano da sole a ogni avvio e salvarle riempirebbe la copia di sicurezza di roba che non è
# tua. Chi le personalizza può includerle con `--tutto`.
CARTELLE_DELL_APPLICAZIONE = ("Library/",)


def scarica(vault, destinazione: Path, *, tutto: bool = False) -> tuple[int, int, list[str]]:
    """Scrive nella destinazione tutte le pagine del vault.

    Restituisce quante sono state aggiornate, quante erano già identiche, e quali file locali
    non esistono più sul server — **senza cancellarli**: una nota sparita dal vault può essere
    stata cancellata per errore, e la copia di sicurezza è l'ultimo posto in cui inseguire un
    errore del genere.
    """
    destinazione.mkdir(parents=True, exist_ok=True)
    from src.figure import percorso_sicuro

    pagine = vault.elenco_file()
    # Non copiare sessioni, credenziali e file di servizio nascosti.
    pagine = [p for p in pagine if not any(c.startswith(".") for c in p.split("/"))]
    if not tutto:
        pagine = [p for p in pagine if not p.startswith(CARTELLE_DELL_APPLICAZIONE)]
    aggiornate = invariate = 0
    for nome in pagine:
        percorso = percorso_sicuro(destinazione, nome)
        contenuto = vault.leggi_file(nome)
        if contenuto is None:
            raise RuntimeError(f"file elencato ma non scaricabile: {nome}; backup incompleto")
        # Le pagine possono stare in sottocartelle: SilverBullet ne porta con sé una propria
        # (`Library/…`), e nulla vieta di organizzare le note in cartelle un domani.
        percorso.parent.mkdir(parents=True, exist_ok=True)
        if percorso.exists() and percorso.read_bytes() == contenuto:
            invariate += 1
            continue
        # Un download/scrittura interrotto non deve troncare la copia precedente.
        with tempfile.NamedTemporaryFile(dir=percorso.parent, delete=False) as f:
            temporaneo = Path(f.name)
            try:
                f.write(contenuto)
                f.flush()
                os.fsync(f.fileno())
                os.replace(temporaneo, percorso)
            finally:
                temporaneo.unlink(missing_ok=True)
        aggiornate += 1
    presenti = set(pagine)
    orfani = sorted(
        str(p.relative_to(destinazione))
        for p in destinazione.rglob("*") if p.is_file()
        and not any(c.startswith(".") for c in p.relative_to(destinazione).parts)
        and (tutto or not str(p.relative_to(destinazione)).startswith(CARTELLE_DELL_APPLICAZIONE))
        and str(p.relative_to(destinazione)) not in presenti
    )
    return aggiornate, invariate, orfani


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.scarica",
        description="Scarica il vault in una copia di sicurezza locale. Specchio a senso unico: "
        "non rimanda niente sul server e non cancella niente in locale.",
    )
    parser.add_argument(
        "--in", dest="destinazione", type=Path,
        help="cartella di destinazione (default: `.backup` accanto ai JSON)",
    )
    parser.add_argument(
        "--tutto", action="store_true",
        help="salva anche le pagine della libreria di SilverBullet, che di norma si escludono",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dotenv.load_dotenv(RADICE / ".env", override=False)

    from src.ingest import Configurazione
    from src.pubblica import Vault

    configurazione = Configurazione.carica()
    destinazione = args.destinazione or configurazione.note / ".backup"

    aggiornate, invariate, orfani = scarica(
        Vault.da_ambiente(), destinazione, tutto=args.tutto
    )
    print(f"  {aggiornate} aggiornate, {invariate} già identiche → {destinazione}")
    if orfani:
        print(f"  {len(orfani)} presenti solo in copia, non più sul vault: {', '.join(orfani)}")
        print("  (non sono state cancellate: se non è voluto, è da qui che si recuperano)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
