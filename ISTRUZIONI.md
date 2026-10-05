# Istruzioni operative

> La guida completa e aggiornata è [README.md](README.md). Le impostazioni personali riportate qui descrivono l’installazione esistente.

Come si usa la pipeline, giorno per giorno. Il *perché* delle scelte sta in
[HANDOVER.md](HANDOVER.md); qui c'è solo cosa digitare.

Tutti i comandi si lanciano da `/Users/nicola/code/paper-pipeline`.

---

## Nuovo ingresso: URL del full-text

Dalla cartella del progetto, con l'ambiente virtuale attivo:

```bash
python -m src.ingest "https://editore.example/articolo/full" --dry-run
python -m src.ingest "https://editore.example/articolo/full" --pubblica
```

Usare la pagina con l'articolo completo. Le regole di sintesi sono le stesse dei PDF. Le figure citate nei passaggi utilizzati vengono scaricate dall'editore, senza ritaglio, e richiedono approvazione nell'anteprima come i PDF. Se un download fallisce, `python -m src.figure /percorso/nota.json --riprova` lo ripete senza chiamate al modello. Pagine protette, abstract e contenuti che richiedono JavaScript possono richiedere il PDF. Gli HTML locali rimangono utilizzabili per il testo; per le figure automatiche usare l'URL.

## Il giro normale

### 1. Scarica il PDF dove vuoi

Non c'è una cartella obbligata. **Il documento resta dove lo metti**: la pipeline non lo sposta
e non ne fa copie. Una sola copia del PDF, nella posizione che hai deciso tu.

### 2. Guarda prima, che è gratis

```bash
.venv/bin/python -m src.ingest ~/Documents/Papers/nuovo.pdf --dry-run
```

Estrae il testo, cerca il DOI, interroga CrossRef e si ferma **prima** di spendere. In dieci
secondi vedi pagine, token stimati, tabelle riconosciute, autori e rivista.

La parte che conta sono gli **avvisi** in fondo. Vanno letti, non scorsi:

| Avviso | Cosa significa |
|---|---|
| `metadati da completare a mano` | CrossRef e PubMed non conoscono il DOI. La nota uscirà senza autori e citazione |
| `il titolo del DOI «…» non compare nella prima pagina` | Il DOI potrebbe essere di un altro articolo citato in copertina. **Verifica sul PDF** |
| `il PDF usa font a codifica propria` | Lettere greche e prefissi di unità possono uscire sbagliati (`µm` letto come `mm`). Ricontrolla le unità nella nota |
| `numeri di pagina non ricostruiti` | Innocuo: i rimandi non compaiono comunque nelle note |
| `~N token stimati, oltre la soglia` | Il documento non entra in una chiamata sola. Non è ancora successo |

Non saltare questo passo. Ogni editore nuovo ha portato la sua stranezza sulla prima pagina, e
il dry-run è dove si vedono senza pagare.

### 3. Elabora e pubblica

```bash
.venv/bin/python -m src.ingest ~/Documents/Papers/nuovo.pdf --pubblica
```

In ordine: classifica, estrae, salva il JSON, rende il markdown, lo manda sul vault, **rilegge
per conferma**, cancella la copia locale, rigenera la pagina `index`.

Se il vault non risponde, la nota **resta in locale** e compare un avviso invece di un errore:
è già costata una chiamata e non va persa. Si riprova poi con `python -m src.pubblica`.

### 4. Leggi il riepilogo

Costo della corsa e, in fondo, i **tag nuovi** che il modello ha proposto. Non devi farci niente
subito: un tag proposto funziona già nella nota, semplicemente non fa ancora parte del
vocabolario.

### 5. La nota è sul vault

**https://nicmaf-vault.duckdns.org** — in cima all'indice. Da lì la leggi e la correggi, anche
dal telefono. Il vault è l'unica sede delle note: quello che correggi lì è la versione buona.

---

## Figure richiamate nei passaggi della sintesi operativa

Le nuove estrazioni selezionano le figure esplicitamente citate nei passaggi originali
usati nella nota. Non basta che la figura riguardi lo stesso argomento, e non serve che il
richiamo «Figura 1» sia ripetuto nel riassunto italiano. Nel JSON restano il numero della
figura, la citazione testuale del passaggio e la sezione della nota a cui appartiene.

Per i PDF la pipeline propone ritagli PNG dell'originale, includendo testo e grafica
vettoriale. Le didascalie restano nella lingua della fonte. Le proposte **vanno controllate**:
ritagli, pannelli e didascalie dipendono dall'impaginazione dell'editore. Finché ci sono figure
pendenti, `--pubblica` conserva la nota in locale e segnala il controllo necessario; non si
spende una nuova chiamata per completarlo.

1. Dopo `src.ingest`, apri il file HTML indicato negli avvisi, sotto `note/.anteprime/`.
   Per rigenerare o ritrovare l'anteprima:

   ```bash
   .venv/bin/python -m src.figure "$HOME/Documents/Papers/note/.json/<slug>.json"
   ```

2. Controlla che la figura sia completa, con tutti i pannelli, le frecce e le etichette,
   e che corrisponda al passaggio riportato nell'anteprima. Approva i numeri corretti;
   escludi esplicitamente quelli da omettere:

   ```bash
   .venv/bin/python -m src.figure "$HOME/Documents/Papers/note/.json/<slug>.json" --approva 1 3
   .venv/bin/python -m src.figure "$HOME/Documents/Papers/note/.json/<slug>.json" --escludi 2
   ```

   I comandi aggiornano il JSON e rigenerano il Markdown locale. Non pubblicano e non
   chiamano il modello. La selezione delle figure resta automatica; questa verifica riguarda
   la fedeltà del recupero dal PDF e l'associazione con il testo.

3. Quando tutte le figure sono approvate o escluse, pubblica la nota:

   ```bash
   .venv/bin/python -m src.pubblica "$HOME/Documents/Papers/note/<slug>.md"
   ```

   Prima vengono caricati e riletti gli allegati, poi la nota. I file sono confrontati
   integralmente; una verifica fallita conserva il Markdown sul Mac. Le immagini restano
   anche in locale, in `note/assets/<slug>/`, per rigenerare e ripubblicare senza estrarre
   nuovamente il PDF. Il backup `src.scarica` ora comprende anche gli allegati.

**Ritaglio da correggere.** È possibile indicare un rettangolo manuale in punti PDF
(origine in alto a sinistra), con pagina fisica del PDF numerata da 1:

```bash
.venv/bin/python -m src.figure "$HOME/Documents/Papers/note/.json/<slug>.json" \
  --ritaglio 1 2 48 350 550 575
```

L'esempio riguarda la figura 1 sulla seconda pagina; le coordinate vanno scelte per quel
PDF. Il ritaglio torna «da verificare»: riaprire l'anteprima prima di approvarlo. Le figure
su più pagine non vengono assemblate automaticamente. Un richiamo non verificato nel testo
non si può sanare scegliendo soltanto un rettangolo: va corretta la selezione nel JSON o la
figura va esclusa.

**Note già pubblicate.** L'aggiunta di nuove figure conserva il testo remoto e gli eventuali
blocchi figura già presenti. Se la sezione di destinazione è stata rinominata, la pubblicazione
si ferma: occorre riallineare il titolo in `figure[].sezione_nota` e rigenerare. Non usare
`--sovrascrivi` per aggirare il controllo, perché cancellerebbe le correzioni del corpo.
I blocchi figura esistenti non vengono aggiornati o rimossi automaticamente.

**JSON precedenti alla feature.** Continuano a funzionare e non vengono rielaborati da soli.
Non contengono la selezione semantica: per aggiungerla si può compilare `nota.figure_richieste`
con citazioni reali e sezioni esistenti, poi rilanciare ingest senza `--forza`. In alternativa
`--forza` ripete l'estrazione con il nuovo prompt e comporta nuove chiamate LLM.

**Limiti.** Nei PDF servono didascalie testuali riconoscibili; figure su più pagine,
scansioni e didascalie ambigue vengono segnalate. Gli URL full-text supportano il
recupero diretto degli originali PNG, JPEG e WebP; gli HTML locali continuano a
usare l'estrazione testuale senza catalogo immagini. Per siti non riconosciuti usare
il PDF. Per percorsi personalizzati passa `--note /cartella/note` a `src.figure`;
gli assets devono stare accanto ai Markdown.

---

## Ogni tanto: la manutenzione dei tag

Il modello riusa il vocabolario di `tags.md` e propone un tag nuovo solo quando nessuno copre un
concetto centrale. Quelle proposte vanno decise ogni tanto, tutte insieme.

```bash
.venv/bin/python -m src.tags
```

Mostra i tag proposti e non ancora in vocabolario, con quante volte e da quali note, e i tag
esistenti che gli somigliano. Un tag proposto **tre volte** da paper diversi è una categoria
vera; uno proposto una volta è quasi sempre il vocabolario di quell'autore.

Poi si modifica `tags.md` **a mano**. Tre mosse:

```markdown
## Quadri clinici
- delirium                                          ← promuovi: aggiungi la riga

| `sedazione-leggera` | `sedazione` | il grado è un attributo |   ← accorpa: riga in tabella
| `ipotermia-terapeutica` | — | un solo paper |                    ← respingi: `—` al centro
```

La tabella in fondo a `tags.md` è anche il **registro delle decisioni**: un tag che compare lì
non torna più nella rassegna.

Infine si riallinea e si ripubblica:

```bash
.venv/bin/python -m src.tags --riallinea   # riscrive i tag nei JSON e rigenera i markdown
.venv/bin/python -m src.pubblica           # li manda sul vault e li cancella da qui
```

Il riallineamento non chiama mai il modello. La ripubblicazione fa la **fusione chirurgica**:
prende dal vault il corpo della nota — con le tue correzioni — e ci rimette testa ed etichette
aggiornate.

---

## Ogni tanto: la copia di sicurezza

Le note vivono solo sul vault. Un comando le riporta tutte giù, gratis, senza toccare il server.

```bash
.venv/bin/python -m src.scarica
```

Scrive in `~/Documents/Papers/note/.backup/` e stampa quante ne ha aggiornate e quante erano
già identiche. Non cancella mai niente: se una nota è sparita dal vault ma la copia locale ce
l'ha ancora, te la segnala come «orfana» e la lascia lì. Sono i tuoi file, in markdown, leggibili
senza nessun programma.

Vale la pena lanciarlo prima di ogni intervento sul server.

---

## Obsidian sul desktop

La cartella del vault è esposta anche in **WebDAV**, così Obsidian desktop può aprirla come se
fosse una cartella locale. Stesso dominio, stessa password di SilverBullet, sotto `/dav`.

**Montarla** (una volta sola; il Mac poi la ricorda):

1. Finder → menu *Vai* → *Connessione al server…* (⌘K)
2. indirizzo: `https://nicmaf-vault.duckdns.org/dav`
3. utente e password: le stesse con cui entri nel vault

Compare come disco in `/Volumes/dav`. In Obsidian: *Apri cartella come vault* → scegli quel
disco.

**Tre cose da sapere prima di usarlo davvero.**

*Non c'è nessuna sincronizzazione.* Obsidian legge e scrive direttamente sul server: non tiene
una copia sul Mac. Se la rete cade mentre stai scrivendo, il salvataggio fallisce. Per lavorare
in aereo serve altro.

*Non c'è risoluzione dei conflitti.* Se apri la stessa nota su SilverBullet dal telefono e su
Obsidian dal Mac, vince l'ultimo che salva e l'altra versione sparisce senza avviso. Usa un
programma alla volta per nota.

*Obsidian scrive la sua configurazione dentro il vault*, in una cartella `.obsidian/`.
SilverBullet la ignora — indicizza solo i `.md` — quindi i due convivono, ma la cartella c'è.

Se il disco resta appeso, in Finder si espelle e si rimonta; oppure da terminale:

```bash
umount /Volumes/dav
```

---

## Quando qualcosa va storto

**Una pubblicazione è fallita.** La nota è rimasta in `~/Documents/Papers/note/`. Riprova:

```bash
.venv/bin/python -m src.pubblica
```

**Hai cambiato la password del vault.** Vanno allineati due punti e poi ricreato il container:

```bash
ssh -i ~/.ssh/oracle-paper-vault.key ubuntu@144.24.237.77
#   modifica SB_USER in /home/ubuntu/paper-vault/.env  (formato utente:password)
cd /home/ubuntu/paper-vault && docker compose up -d --force-recreate
```

Poi aggiorna `VAULT_PASSWORD` nel `.env` locale. **`docker restart` non basta**: Docker legge
`env_file` alla creazione del container, non a ogni avvio. Quel comando ricrea anche il
WebDAV, che usa la stessa credenziale: dopo, in Finder, il disco va rimontato.

**Vuoi rifare una nota da zero**, perché hai cambiato il prompt o un template:

```bash
.venv/bin/python -m src.ingest <percorso-del-pdf> --forza --pubblica
```

Senza `--forza` riconosce il DOI già elaborato e si limita a rigenerare dal JSON, senza spendere.

**Hai cambiato la struttura della nota** (template, non solo tag). La fusione chirurgica non
basta, perché conserva il corpo del server, che è quello vecchio:

```bash
.venv/bin/python -m src.pubblica --sovrascrivi
```

Attenzione: rimpiazza le note per intero e **cancella le correzioni fatte dall'app**.

**Vuoi solo rigenerare il markdown da un JSON**, senza chiamate:

```bash
.venv/bin/python -m src.render ~/Documents/Papers/note/.json/<slug>.json \
  --out ~/Documents/Papers/note/<slug>.md
```

---

## Dove sta cosa

| | dove |
|---|---|
| PDF | dove li hai messi tu. Mai spostati, mai copiati |
| JSON delle note | `~/Documents/Papers/note/.json/` — sorgente per rigenerare, e memoria di cosa è già stato fatto |
| Markdown | **solo sul vault**. In locale esistono solo fra la generazione e la pubblicazione |
| Vocabolario dei tag | `tags.md` |
| Chiavi API e credenziali | `.env`, non versionato |
| Configurazione | `config.toml`, non versionato |
| Note sul server | `/home/ubuntu/paper-vault/note/` |
| Copia di sicurezza | `~/Documents/Papers/note/.backup/`, con `src.scarica` |
| WebDAV per Obsidian | `https://nicmaf-vault.duckdns.org/dav`, montato in `/Volumes/dav` |
| Credenziali del vault | `/home/ubuntu/paper-vault/.env`, chiave `SB_USER` |

**I JSON non si cancellano.** Senza, ogni PDF ridato in pasto costerebbe una chiamata nuova, e
la Fase 5 — le note di sintesi multi-paper — non avrebbe da cosa partire.

---

## Costi

Circa venti-trenta centesimi a paper, con due chiamate: una breve di classificazione e quella di
estrazione. Il documento più grosso elaborato finora, una linea guida da 75 pagine, è costato
116.000 token in ingresso.

**Non ottimizzare sul prezzo.** A duecento paper l'anno la differenza fra il modello migliore e
il peggiore è sotto i venticinque dollari; l'unica variabile che conta è l'accuratezza nella
trascrizione di dosaggi e soglie.

I comandi che **non** costano niente: `--dry-run`, `src.tags`, `src.tags --riallinea`,
`src.scarica`, `src.render`, `src.indice`, `src.pubblica`.
