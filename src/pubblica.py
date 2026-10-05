"""Invio delle note al vault remoto (SilverBullet).

Il server è la copia autorevole: le note si modificano dall'interfaccia dell'applicazione, non
in locale. Ne discende la regola che governa questo modulo — **una nota già pubblicata non
viene mai sovrascritta per intero**.

Quando il vocabolario dei tag cambia e le note vengono riallineate (`python -m src.tags
--riallinea`), quello che cambia nel file locale è soltanto il frontmatter e la riga di
etichette in coda. Rimandare su l'intera nota per aggiornare due righe cancellerebbe le
correzioni fatte a mano dall'app. Si fa quindi una **fusione chirurgica**: si legge la nota dal
server, se ne tiene il corpo, e si sostituiscono solo le due parti che la pipeline possiede.

Chi possiede che cosa:

| | proprietario |
|---|---|
| testa: titolo e riga di citazione | la pipeline: vengono dai metadati CrossRef |
| corpo della nota | il server, appena qualcuno la apre e la corregge |
| riga di etichette in coda | la pipeline: è la proiezione dei tag |
"""

from __future__ import annotations

import http.cookiejar
import hashlib
import logging
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Il titolo della nota, prima riga utile del file.
_RE_TITOLO = re.compile(r"#\s+\S")

# La riga di citazione: tutta in corsivo, subito sotto il titolo.
_RE_CITAZIONE = re.compile(r"\*[^*].*\*\Z")

# Una riga di sole etichette: `#tag #altro-tag`. Un titolo markdown ha lo spazio dopo il
# cancelletto, quindi `# Titolo` non viene scambiato per una riga di etichette.
_RE_RIGA_ETICHETTE = re.compile(r"#\S+(?:[ \t]+#\S+)*\Z")


class PubblicazioneFallita(RuntimeError):
    """Il vault non ha accettato la nota, o non la restituisce come l'abbiamo mandata."""


@dataclass
class Pezzi:
    """Una nota scomposta nelle tre parti con proprietari diversi."""

    testa: str
    corpo: str
    etichette: str

    def unisci(self) -> str:
        parti = [p for p in (self.testa.strip(), self.corpo.strip()) if p]
        coda = f"\n\n{self.etichette}" if self.etichette else ""
        return "\n\n".join(parti) + coda + "\n"


def separa(testo: str) -> Pezzi:
    """Scompone una nota in testa, corpo ed etichette finali.

    La testa è il titolo più l'eventuale riga di citazione in corsivo. Una nota priva di una di
    queste parti non è un errore: il pezzo corrispondente resta vuoto e la ricomposizione lo omette.
    """
    righe = testo.splitlines()

    # La testa: il titolo e, se c'è, la riga di citazione in corsivo subito sotto.
    testa: list[str] = []
    if righe and _RE_TITOLO.match(righe[0]):
        testa.append(righe.pop(0))
        while righe and not righe[0].strip():
            righe.pop(0)
        if righe and _RE_CITAZIONE.fullmatch(righe[0].strip()):
            testa.append(righe.pop(0))
    while righe and not righe[0].strip():
        righe.pop(0)

    etichette = ""
    while righe and not righe[-1].strip():
        righe.pop()
    if righe and _RE_RIGA_ETICHETTE.fullmatch(righe[-1].strip()):
        etichette = righe.pop().strip()
    return Pezzi(testa="\n\n".join(testa), corpo="\n".join(righe), etichette=etichette)


def fondi(remota: str, locale: str) -> str:
    """La nota da rimandare sul server: corpo del server, testa ed etichette locali."""
    sul_server, qui = separa(remota), separa(locale)
    from src.figure import fondi_figure

    try:
        corpo = fondi_figure(sul_server.corpo, qui.corpo)
    except ValueError as exc:
        raise PubblicazioneFallita(str(exc)) from exc
    return Pezzi(testa=qui.testa, corpo=corpo, etichette=qui.etichette).unisci()


@dataclass
class Vault:
    """Il vault remoto, via l'API sul filesystem di SilverBullet.

    SilverBullet non ha un'API a risorse come flatnotes: espone direttamente lo spazio dei file
    sotto `/.fs/`, con `GET` e `PUT` sul percorso della pagina. L'autenticazione è a cookie —
    si posta il modulo di login a `/.auth` e si conserva la sessione.

    Creare e aggiornare sono la stessa operazione, un `PUT`. La distinzione fra le due la fa
    `pubblica`, guardando se la pagina c'era già.
    """

    base_url: str
    utente: str
    password: str
    timeout: float = 20.0
    _sessione: urllib.request.OpenerDirector | None = field(default=None, repr=False)

    @classmethod
    def da_ambiente(cls) -> "Vault":
        mancanti = [v for v in ("VAULT_URL", "VAULT_UTENTE", "VAULT_PASSWORD") if not os.getenv(v)]
        if mancanti:
            raise PubblicazioneFallita(
                f"{', '.join(mancanti)} non impostate: mettile in .env (vedi .env.example)"
            )
        return cls(
            base_url=os.environ["VAULT_URL"].rstrip("/"),
            utente=os.environ["VAULT_UTENTE"],
            password=os.environ["VAULT_PASSWORD"],
        )

    # -- trasporto ---------------------------------------------------------------------
    def sessione(self) -> urllib.request.OpenerDirector:
        """La sessione autenticata, aperta alla prima chiamata e riusata."""
        if self._sessione is None:
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
            )
            dati = urllib.parse.urlencode(
                {"username": self.utente, "password": self.password}
            ).encode()
            try:
                with opener.open(f"{self.base_url}/.auth", data=dati, timeout=self.timeout):
                    pass
            except urllib.error.HTTPError as exc:
                raise PubblicazioneFallita(
                    f"credenziali del vault rifiutate (HTTP {exc.code})"
                ) from exc
            except urllib.error.URLError as exc:
                raise PubblicazioneFallita(f"vault irraggiungibile: {exc.reason}") from exc
            self._sessione = opener
        return self._sessione

    def _chiama(self, percorso: str, corpo: bytes | None = None, metodo: str | None = None):
        dati = self._chiama_bytes(percorso, corpo, metodo, "text/markdown; charset=utf-8")
        return dati.decode("utf-8") if dati is not None else None

    def _chiama_bytes(self, percorso: str, corpo: bytes | None = None,
                      metodo: str | None = None, content_type: str = "application/octet-stream"):
        richiesta = urllib.request.Request(self.base_url + percorso, method=metodo, data=corpo)
        if corpo is not None:
            richiesta.add_header("Content-Type", content_type)
        try:
            with self.sessione().open(richiesta, timeout=self.timeout) as risposta:
                return risposta.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and (metodo or "GET") == "GET":
                return None
            if exc.code == 401:
                raise PubblicazioneFallita(
                    "sessione non autenticata: credenziali sbagliate o scadute"
                ) from exc
            raise PubblicazioneFallita(
                f"{metodo or 'GET'} {percorso}: HTTP {exc.code} {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise PubblicazioneFallita(f"vault irraggiungibile: {exc.reason}") from exc

    @staticmethod
    def _percorso(titolo: str) -> str:
        return f"/.fs/{urllib.parse.quote(titolo)}.md"

    # -- operazioni --------------------------------------------------------------------
    def leggi(self, titolo: str) -> str | None:
        """Il contenuto della nota sul server, o `None` se non c'è."""
        return self._chiama(self._percorso(titolo))

    def elenco(self) -> list[str]:
        """I nomi delle pagine presenti sul vault, senza estensione."""
        return [nome[:-3] for nome in self.elenco_file() if nome.endswith(".md")]

    def elenco_file(self) -> list[str]:
        """File dello spazio, inclusi gli allegati; i nomi mantengono l'estensione."""
        import json as _json

        grezzo = self._chiama("/.fs/")
        if not grezzo:
            return []
        return sorted(v["name"] for v in _json.loads(grezzo)
                      if v.get("name") and not v["name"].endswith("/"))

    @staticmethod
    def _percorso_file(nome: str) -> str:
        from src.figure import percorso_sicuro

        percorso_sicuro(Path("."), nome)
        return "/.fs/" + urllib.parse.quote(nome, safe="/")

    def leggi_file(self, nome: str) -> bytes | None:
        return self._chiama_bytes(self._percorso_file(nome))

    def scrivi_file(self, nome: str, dati: bytes) -> None:
        self._chiama_bytes(self._percorso_file(nome), dati, "PUT",
                           mimetypes.guess_type(nome)[0] or "application/octet-stream")

    def scrivi(self, titolo: str, contenuto: str) -> None:
        self._chiama(self._percorso(titolo), contenuto.encode("utf-8"), metodo="PUT")

    # Su SilverBullet creare e aggiornare sono lo stesso `PUT`: i due nomi restano perché
    # `pubblica` distingue i due casi, e perché rendono leggibile il registro delle chiamate.
    crea = scrivi
    aggiorna = scrivi

    def pubblica(self, titolo: str, contenuto: str, *, sovrascrivi: bool = False) -> str:
        """Manda la nota sul vault e restituisce che cosa è successo.

        Non solleva se la nota è già allineata: pubblicare due volte la stessa cosa è
        un'operazione legittima e deve costare zero.

        `sovrascrivi` rimpiazza la nota **per intero**, corpo compreso, e va usato solo quando
        cambia la struttura della nota — non i tag né i metadati, che la fusione gestisce da
        sola. In una migrazione di struttura la fusione non basta: conserva fedelmente il corpo
        del server, e il corpo del server è quello vecchio.
        """
        remota = self.leggi(titolo)
        if remota is None:
            self.crea(titolo, contenuto)
            esito = "creata"
        elif sovrascrivi:
            if contenuto == remota:
                return "invariata"
            self.aggiorna(titolo, contenuto)
            esito = "sovrascritta per intero"
        else:
            fusa = fondi(remota, contenuto)
            if fusa == remota:
                return "invariata"
            self.aggiorna(titolo, fusa)
            esito = "aggiornata (testa ed etichette)"
        self._verifica(titolo, esito, contenuto if remota is None or sovrascrivi else fusa)
        return esito

    def _verifica(self, titolo: str, esito: str, atteso: str) -> None:
        """Rilegge la nota dal server. È il passo che autorizza a cancellare la copia locale.

        Senza, una scrittura fallita a metà lascerebbe una nota monca sul server e nessuna
        copia qui: l'unico modo di perdere davvero una nota.
        """
        if self.leggi(titolo) != atteso:
            raise PubblicazioneFallita(
                f"«{titolo}» risulta {esito} ma il vault non la restituisce identica: "
                "la copia locale non va cancellata"
            )


def pubblica_file(
    vault: Vault, percorso: Path, *, cancella: bool = False, sovrascrivi: bool = False
) -> str:
    """Pubblica una nota e, solo dopo la verifica, cancella la copia locale."""
    percorso = Path(percorso)
    testo = percorso.read_text(encoding="utf-8")
    from src.figure import PENDENTI, percorso_sicuro

    if PENDENTI in testo:
        raise PubblicazioneFallita("figure da verificare: usa python -m src.figure sul JSON, "
                                  "poi approva o escludi i ritagli")
    # Prima valida tutti i file locali, poi carica gli allegati, infine la nota.
    # Percorsi con hash: una nuova versione non modifica le figure di una nota esistente.
    immagini = re.findall(r"!\[[^\]]*\]\((assets/[^)]+)\)", testo)
    allegati = []
    for nome in dict.fromkeys(immagini):
        try:
            dati = percorso_sicuro(percorso.parent, nome).read_bytes()
        except (OSError, ValueError) as exc:
            raise PubblicazioneFallita(f"allegato non disponibile: {nome}") from exc
        impronta = re.search(r"-([0-9a-f]{16})\.(?:png|jpe?g|webp)$", nome)
        if impronta and hashlib.sha256(dati).hexdigest()[:16] != impronta.group(1):
            raise PubblicazioneFallita(f"allegato modificato dopo la verifica: {nome}")
        allegati.append((nome, dati))
    for nome, dati in allegati:
        remoti = vault.leggi_file(nome)
        if remoti == dati:
            continue
        if remoti is not None:
            raise PubblicazioneFallita(f"allegato remoto diverso: {nome}; non sovrascritto")
        vault.scrivi_file(nome, dati)
        if vault.leggi_file(nome) != dati:
            raise PubblicazioneFallita(f"verifica dell'allegato fallita: {nome}")
    esito = vault.pubblica(
        percorso.stem, testo, sovrascrivi=sovrascrivi
    )
    if cancella:
        percorso.unlink()
        log.info("%s: %s, copia locale rimossa", percorso.name, esito)
    else:
        log.info("%s: %s", percorso.name, esito)
    return esito


def main(argv: list[str] | None = None) -> int:
    import argparse

    import dotenv

    parser = argparse.ArgumentParser(
        prog="python -m src.pubblica",
        description="Manda al vault le note presenti in locale e ne cancella la copia qui, "
        "dopo averne verificato l'arrivo. Serve dopo `src.tags --riallinea`, che rigenera i "
        "markdown senza pubblicarli, e per riprovare una pubblicazione fallita.",
    )
    parser.add_argument("note", nargs="*", type=Path, help="note da inviare (default: tutte)")
    parser.add_argument(
        "--tieni", action="store_true", help="non cancellare la copia locale dopo l'invio"
    )
    parser.add_argument(
        "--sovrascrivi",
        action="store_true",
        help="rimpiazza la nota per intero invece di aggiornarne testa ed etichette. "
        "Serve solo quando cambia la struttura della nota: cancella le correzioni "
        "fatte dall'interfaccia del vault",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dotenv.load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

    from src.ingest import Configurazione

    configurazione = Configurazione.carica()
    elenco = args.note or sorted(configurazione.note.glob("*.md"))
    if not elenco:
        print(f"nessuna nota da pubblicare in {configurazione.note}")
        return 0

    vault = Vault.da_ambiente()
    esiti: list[str] = []
    for percorso in elenco:
        try:
            esiti.append(
                pubblica_file(
                    vault, percorso, cancella=not args.tieni, sovrascrivi=args.sovrascrivi
                )
            )
        except PubblicazioneFallita as exc:
            log.error("%s: %s", percorso.name, exc)
            esiti.append("fallita")

    if any(e != "fallita" for e in esiti):
        from src.indice import pubblica_indice

        pubblica_indice(vault, configurazione.json)

    riepilogo = {e: esiti.count(e) for e in dict.fromkeys(esiti)}
    print("\n" + ", ".join(f"{n} {e}" for e, n in riepilogo.items()))
    return 1 if "fallita" in riepilogo else 0


if __name__ == "__main__":
    raise SystemExit(main())
