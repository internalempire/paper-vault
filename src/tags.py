"""Il vocabolario controllato e la rassegna dei tag proposti.

`tags.md` è di sola lettura per la pipeline: il modello **propone** tag nuovi, non li aggiunge.
Se si aggiungessero da soli il vocabolario smetterebbe di controllare qualcosa — dopo trenta
paper si ritroverebbero `sepsi`, `shock-settico` e `settico` come categorie distinte, che è il
motivo per cui il vocabolario esiste (HANDOVER §4).

Decidere però un paper alla volta è il modo peggiore di farlo: si vede un documento e non
l'insieme. Questo modulo fa la rassegna — che cosa è stato proposto, quante volte, da quali
note, e quali tag esistenti gli somigliano — così la decisione si prende in una sola sessione.
La modifica di `tags.md` resta a mano: è una scelta su come cercherai, non un'operazione.

    python -m src.tags
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]

# Sotto questa somiglianza due tag non sono confondibili, e proporre l'accostamento è rumore.
_SOMIGLIANZA_MINIMA = 0.6

# Quanti tag simili mostrare per ciascuna proposta.
_QUANTI_SIMILI = 3


def vocabolario(percorso: Path) -> list[str]:
    """Il vocabolario controllato, letto dal filesystem.

    Solo le voci di elenco sono tag: il file deve poter contenere titoli, spiegazioni e tabelle
    senza che finiscano nel vocabolario passato al modello.
    """
    if not percorso.is_file():
        return []
    voci = []
    for riga in percorso.read_text(encoding="utf-8").splitlines():
        if not re.match(r"^\s*[-*]\s+\S", riga):
            continue
        if voce := riga.strip().lstrip("-*").strip().strip("`"):
            voci.append(voce.split()[0])
    return voci


# Una riga di tabella in tags.md: `| `tag`, `tag2` | destinazione | perché |`. È il registro
# delle proposte già decise — accorpate in un tag esistente, tradotte o lasciate cadere.
_RE_RIGA_TABELLA = re.compile(r"^\s*\|(?P<proposti>[^|]+)\|(?P<destinazione>[^|]*)\|")
_RE_TAG_FRA_APICI = re.compile(r"`([^`]+)`")


def risolti(percorso: Path) -> dict[str, str]:
    """Le proposte già decise, lette dalle tabelle di `tags.md`.

    Il file documenta ogni accorpamento con il motivo; quella tabella basta anche a ricordare
    che cosa è già stato guardato, senza un secondo file da tenere allineato. Un tag che compare
    lì non torna nella rassegna: la decisione è stata presa, che sia finito in un tag esistente
    o lasciato cadere.
    """
    if not percorso.is_file():
        return {}
    decisi: dict[str, str] = {}
    for riga in percorso.read_text(encoding="utf-8").splitlines():
        trovata = _RE_RIGA_TABELLA.match(riga)
        if not trovata:
            continue
        destinazione = " ".join(_RE_TAG_FRA_APICI.findall(trovata["destinazione"])) or "—"
        for tag in _RE_TAG_FRA_APICI.findall(trovata["proposti"]):
            decisi[tag.lower()] = destinazione
    return decisi


@dataclass
class Proposta:
    """Un tag proposto dal modello e non ancora nel vocabolario."""

    tag: str
    note: list[str] = field(default_factory=list)
    simili: list[str] = field(default_factory=list)

    @property
    def quante_volte(self) -> int:
        return len(self.note)


def proposte(
    cartella_json: Path, ammessi: list[str], decisi: dict[str, str] | None = None
) -> list[Proposta]:
    """I tag proposti nelle note che non sono in vocabolario né già decisi, dal più ricorrente."""
    in_vocabolario = {t.lower() for t in ammessi}
    decisi = decisi or {}
    per_tag: dict[str, list[str]] = defaultdict(list)
    for percorso in sorted(cartella_json.glob("*.json")):
        meta = json.loads(percorso.read_text(encoding="utf-8"))["nota"]["meta"]
        for tag in list(meta.get("tags") or []) + list(meta.get("tags_nuovi") or []):
            pulito = tag.lower().rstrip("*")
            if pulito not in in_vocabolario and pulito not in decisi:
                per_tag[tag.rstrip("*")].append(percorso.stem)
    elenco = [Proposta(tag, note, _simili(tag, ammessi)) for tag, note in per_tag.items()]
    return sorted(elenco, key=lambda p: (-p.quante_volte, p.tag))


def _simili(tag: str, ammessi: list[str]) -> list[str]:
    """I tag del vocabolario che potrebbero già coprire la proposta.

    Due criteri uniti: la somiglianza letterale — che pesca `iperkaliemia-farmacologica` accanto
    a `iperkaliemia` — e la condivisione di una parola, che pesca `ecocardiografia-in-shock`
    accanto sia a `ecocardiografia` sia a `shock`, cioè i due tag che insieme la sostituiscono.
    """
    parole = set(tag.lower().split("-"))
    per_parola = [a for a in ammessi if parole & set(a.lower().split("-"))]
    per_lettera = difflib.get_close_matches(tag, ammessi, n=_QUANTI_SIMILI, cutoff=_SOMIGLIANZA_MINIMA)
    visti, uniti = set(), []
    for a in per_parola + per_lettera:
        if a not in visti:
            visti.add(a)
            uniti.append(a)
    return uniti[:_QUANTI_SIMILI]


def mai_usati(
    cartella_json: Path, ammessi: list[str], decisi: dict[str, str] | None = None
) -> list[str]:
    """I tag del vocabolario che nessuna nota usa, né direttamente né attraverso un accorpamento.

    Un tag vale come usato anche quando la nota porta ancora la forma da cui è stato accorpato:
    `scompenso-cardiaco` è in uso benché la nota dica `heart-failure`, perché la tabella di
    `tags.md` dice che sono la stessa cosa. Senza questo passaggio l'elenco sarebbe soltanto
    l'altra faccia degli accorpamenti, e non direbbe niente di nuovo.

    Un tag davvero mai usato non è un errore — un vocabolario serve anche a suggerire — ma dopo
    un po' di note è quasi sempre una categoria che sembrava sensata a tavolino e non lo era.
    """
    decisi = decisi or {}
    usati: set[str] = set()
    for percorso in cartella_json.glob("*.json"):
        meta = json.loads(percorso.read_text(encoding="utf-8"))["nota"]["meta"]
        for tag in list(meta.get("tags") or []) + list(meta.get("tags_nuovi") or []):
            pulito = tag.rstrip("*").lower()
            usati.add(pulito)
            # Se il tag è stato accorpato, valgono come usate anche le sue destinazioni.
            usati.update(d.lower() for d in decisi.get(pulito, "").split())
    return [a for a in ammessi if a.lower() not in usati]


def riallinea(
    cartella_json: Path, ammessi: list[str], decisi: dict[str, str], cartella_note: Path
) -> list[tuple[str, list[str], list[str]]]:
    """Riscrive i tag delle note secondo il vocabolario e la tabella degli accorpamenti.

    Le note portano i tag di quando sono state scritte. Da quando le etichette in coda sono il
    meccanismo di navigazione del vault, un tag non allineato non è più solo disordine: è una
    nota che non si trova. Il riallineamento è gratuito — si riscrivono i tag nei JSON e si
    rigenera il markdown, senza richiamare il modello.

    Un tag già in vocabolario resta e passa fra i validati; uno accorpato diventa la sua
    destinazione; uno respinto con `—` sparisce; uno mai deciso resta fra i proposti, perché
    non tocca a questa funzione decidere.
    """
    from src.render import render_da_file

    in_vocabolario = {t.lower(): t for t in ammessi}
    cambiamenti = []
    for percorso in sorted(cartella_json.glob("*.json")):
        contenuto = json.loads(percorso.read_text(encoding="utf-8"))
        meta = contenuto["nota"]["meta"]
        prima = list(meta.get("tags") or []) + list(meta.get("tags_nuovi") or [])

        validati: list[str] = []
        proposti: list[str] = []
        for grezzo in prima:
            tag = grezzo.rstrip("*")
            for risolto in _destinazioni(tag, in_vocabolario, decisi):
                elenco = validati if risolto.lower() in in_vocabolario else proposti
                if risolto not in elenco:
                    elenco.append(risolto)

        # Il confronto è sulla **ripartizione**, non sulla concatenazione: una nota i cui tag
        # erano tutti fra i proposti e ora sono tutti validati ha la stessa sequenza ma un
        # significato diverso — nel frontmatter cambia l'asterisco.
        if (meta.get("tags") or []) == validati and (meta.get("tags_nuovi") or []) == proposti:
            continue
        meta["tags"], meta["tags_nuovi"] = validati, proposti
        percorso.write_text(
            json.dumps(contenuto, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (cartella_note / f"{percorso.stem}.md").write_text(
            render_da_file(percorso), encoding="utf-8"
        )
        cambiamenti.append((percorso.stem, prima, validati + proposti))
    return cambiamenti


def _destinazioni(tag: str, in_vocabolario: dict[str, str], decisi: dict[str, str]) -> list[str]:
    """In che cosa si traduce un tag: se stesso, la sua destinazione, o niente."""
    if tag.lower() in in_vocabolario:
        return [in_vocabolario[tag.lower()]]
    destinazione = decisi.get(tag.lower())
    if destinazione is None:
        return [tag]  # mai deciso: resta una proposta aperta
    if destinazione == "—":
        return []  # respinto
    return [in_vocabolario.get(d.lower(), d) for d in destinazione.split()]


def _rassegna(
    elenco: list[Proposta], inutilizzati: list[str], quante_note: int, quanti_decisi: int
) -> str:
    intestazione = f"{quante_note} note esaminate, {len(elenco)} proposte in sospeso"
    if quanti_decisi:
        intestazione += f" ({quanti_decisi} già decise in tags.md, non mostrate)"
    righe = [intestazione, ""]
    if not elenco:
        righe.append("Nessuna proposta in sospeso.")
    for p in elenco:
        note = ", ".join(n[:44] for n in p.note[:3])
        if p.quante_volte > 3:
            note += f", e altre {p.quante_volte - 3}"
        righe.append(f"  {p.tag}  ({p.quante_volte}×)")
        righe.append(f"      note:   {note}")
        righe.append(f"      simili: {', '.join(p.simili) if p.simili else '— nessuno'}")
    if inutilizzati:
        righe += ["", f"Tag del vocabolario mai usati ({len(inutilizzati)}):",
                  "  " + ", ".join(inutilizzati)]
    righe += ["", "Per accettarne uno, aggiungilo a mano in tags.md nel gruppo che gli compete.",
              "Un tag lasciato cadere non fa danno: resta nella nota che l'ha proposto e non si propaga."]
    return "\n".join(righe)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.tags",
        description="Rassegna dei tag proposti dalle note e non ancora nel vocabolario "
        "controllato. Non modifica tags.md: la decisione resta a mano.",
    )
    parser.add_argument("--json", type=Path, help="cartella dei JSON (default: da config.toml)")
    parser.add_argument("--tags", type=Path, default=RADICE / "tags.md")
    parser.add_argument(
        "--riallinea",
        action="store_true",
        help="riscrivi i tag delle note secondo il vocabolario e rigenera il markdown "
        "(non chiama l'API)",
    )
    args = parser.parse_args(argv)

    if args.json:
        cartella = args.json.expanduser()
    else:
        from src.ingest import Configurazione

        cartella = Configurazione.carica().json
    if not cartella.is_dir():
        print(f"nessun JSON in {cartella}")
        return 1

    ammessi = vocabolario(args.tags)
    decisi = risolti(args.tags)

    if args.riallinea:
        from src.ingest import Configurazione

        cambiamenti = riallinea(cartella, ammessi, decisi, Configurazione.carica().note)
        for slug, prima, dopo in cambiamenti:
            print(f"  {slug}")
            print(f"      {len(prima)} → {len(dopo)}: {' '.join(dopo)}")
            if tolti := [t for t in prima if t.rstrip('*') not in dopo]:
                print(f"      non più presenti: {' '.join(tolti)}")
        print(f"\n{len(cambiamenti)} note riallineate. Nessuna chiamata all'API.\n")
    in_sospeso = proposte(cartella, ammessi, decisi)
    quanti_decisi = _quanti_gia_decisi(cartella, ammessi, decisi)
    print(_rassegna(in_sospeso, mai_usati(cartella, ammessi, decisi),
                    len(list(cartella.glob("*.json"))), quanti_decisi))
    return 0


def _quanti_gia_decisi(cartella: Path, ammessi: list[str], decisi: dict[str, str]) -> int:
    """Quante proposte la rassegna sta nascondendo perché già decise: nascondere in silenzio
    sarebbe indistinguibile da un difetto del filtro."""
    in_vocabolario = {t.lower() for t in ammessi}
    visti = set()
    for percorso in cartella.glob("*.json"):
        meta = json.loads(percorso.read_text(encoding="utf-8"))["nota"]["meta"]
        for tag in list(meta.get("tags") or []) + list(meta.get("tags_nuovi") or []):
            pulito = tag.lower().rstrip("*")
            if pulito in decisi and pulito not in in_vocabolario:
                visti.add(pulito)
    return len(visti)


if __name__ == "__main__":
    raise SystemExit(main())
