# HANDOVER — Pipeline PDF paper → note markdown consultabili

> Archivio storico: per architettura e procedure attuali leggere [README.md](README.md). Dal 5 ottobre 2026, su richiesta esplicita dell’utente, il codice viene pubblicato nel repository **privato** `internalempire/paper-vault`; credenziali e dati restano esclusi.

Documento di passaggio di consegne. Da leggere per intero prima di scrivere codice.

---

## 1. Obiettivo del progetto

Costruire una pipeline che trasformi automaticamente i PDF di letteratura medica in note markdown strutturate, consultabili da desktop e da mobile, ospitate su un NAS domestico.

**Flusso target**:

```
PDF in cartella locale
  → estrazione testo
  → classificazione del tipo di documento
  → estrazione strutturata via LLM (API)
  → JSON intermedio
  → rendering markdown con template
  → cartella note locale
  → invio al VPS
  → app self-hosted che legge e modifica markdown su file (web + mobile, dietro autenticazione)
```

**Revisione dell'intento (agosto 2026).** Il NAS non è stato acquistato e l'architettura in
casa è rimandata. Nel frattempo il vault consultabile da remoto vive su un **VPS Oracle
Cloud**: la pipeline gira sul portatile e viene lanciata a mano, i markdown vengono spinti sul
server, e lì un'applicazione li espone e li rende modificabili. È un cambio rispetto alla
scelta originaria di tenere tutto in casa — vedi §3 — preso perché il valore delle note è nel
poterle consultare in reparto, e senza NAS non c'era altro modo.

Le note **non contengono dati di pazienti**: sono sintesi di letteratura pubblicata. La posta
in gioco nell'esporle non è clinica.

Quando il NAS arriverà, la stessa applicazione si sposta lì e cambia soltanto il bersaglio
dell'invio: è il motivo per cui la scelta va fatta su qualcosa che gira in Docker o come
binario, non su un servizio gestito.

**Utente e uso reale**: medico anestesista-rianimatore. Le note servono per la consultazione durante l'attività clinica, in reparto e in sala operatoria. Il criterio di qualità non è la completezza accademica: è che a distanza di mesi la nota permetta di **agire senza riaprire il PDF** e di **verificare qualunque dato in pochi secondi** tornando alla fonte.

**Corpus previsto**: prevalentemente narrative review e linee guida. Non trial. L'informazione ricercata è pratica — meccanismi fisiopatologici, iter diagnostici, indicazioni terapeutiche, dosaggi.

---

## 2. Stato attuale (24 agosto 2026)

**Fase 1 completa.** La pipeline gira da capo a fondo e ha prodotto **10 note da 10 paper reali**,
di 10 editori diversi. 193 test automatici. Fase 0 (template) resta la base: i tre golden in
`tests/golden/` sono il corpus di regressione.

**Fase 2 in corso.** Il VPS è preparato, il candidato per il vault è quasi deciso.

### Come è fatta la pipeline

| Modulo | Cosa fa |
|---|---|
| `src/extract.py` | Testo da PDF e HTML: ordine di lettura a due colonne, tabelle, **tabelle stampate di traverso ricostruite dalle coordinate**, OCR, numeri di pagina stampati, TOC, stima token |
| `src/metadata.py` | DOI dalla prima pagina, CrossRef con ripiego su PubMed, citazione Vancouver, slug |
| `src/llm.py` | Parti comuni: prompt dal file, costi, giro di correzione se il JSON non è conforme |
| `src/llm_anthropic.py`, `src/llm_openai.py` | Le due implementazioni. Si sceglie con `fornitore` in `config.toml` |
| `src/schema.py` + `schema/nota.schema.json` | Validazione del JSON intermedio |
| `src/render.py` + `templates/` | JSON → markdown via Jinja, e rigenerazione da un JSON salvato |
| `src/tags.py` | Vocabolario controllato e rassegna delle proposte |
| `src/ingest.py` | Entrypoint: `python -m src.ingest <file> [--dry-run] [--forza]` |

**Fornitore in uso: OpenAI** (`gpt-5.6-luna` estrazione, `gpt-5.5` classificazione), chiave in `.env`.
L'implementazione Anthropic è scritta e testata ma non è mai stata eseguita.

### Cosa ha retto e cosa no, in dieci documenti

Sul **contenuto** non c'è stato un errore attribuibile alla pipeline. Verifiche puntuali:
24 coppie numero-unità su 24 confermate nel PDF (Palmer), 16 celle su 16 di una tabella ruotata,
13 soglie su 13 (congestione venosa). Due incoerenze **delle fonti** marcate invece che risolte —
la più grave: patiromer `8,4 mg` nella figura contro `8,4-25,2 g` nella tabella, un fattore mille
su un chelante del potassio.

Tutti i difetti trovati erano di **ingresso**, e ognuno è emerso da un editore nuovo:

| Difetto | Editore | Stato |
|---|---|---|
| DOI spezzato su due righe nella barra laterale | Elsevier | ricucito |
| Trattini tipografici (`U+2011`) e caratteri di controllo nel DOI e nei numeri di pagina | BMJ | normalizzati |
| Entità HTML nei metadati CrossRef (`&amp;`) | Springer | sciolte |
| DOI della **testata** scambiato per quello dell'articolo | MDPI | si sceglie il più specifico |
| Font a codifica propria: `µm` estratto come `mm` | Wolters Kluwer | **non correggibile**: si rileva e si avvisa, e il modello corregge l'unità dichiarandolo |
| Tabelle senza bordi non riconosciute | Elsevier, Springer | il modello le ricostruisce dal testo, purché l'ordine di lettura sia giusto |
| `Søreide` → `s-reide` nello slug | Oxford | lettere non scomponibili tradotte |

### Modifiche al prompt fatte in Fase 1

Il prompt è passato da `v3` (uscita markdown) a `v4` (uscita JSON), cambiando **solo il formato**.
Poi sei correzioni, tutte nate da difetti visti su note vere: la lingua dei valori nei dosaggi,
la forma dei tag, la legenda del grading resa obbligatoria, il divieto di parafrasare in prosa le
raccomandazioni graduate, il criterio di ammissione delle tabelle autonome, e la **correzione delle
unità implausibili di ordini di grandezza** (§8).

Una correzione **non ha funzionato**: irrigidire il criterio sulle tabelle autonome non ha impedito
al modello di promuoverle tutte in fondo alla nota. Due tentativi falliti; il terzo probabilmente
fallirebbe. Se il problema dà fastidio, la strada è il renderer, non il prompt.

---

## 3. Ambiente e vincoli

- **Host attuale del vault**: **VPS Oracle Cloud, architettura ARM64**. Coolify è stato disinstallato: si riparte da Docker sulla macchina nuda. L'immagine dell'applicazione **deve esistere per `linux/arm64`**: è il primo filtro nella scelta, prima di qualunque valutazione di funzionalità. Rete da verificare su due strati indipendenti: security list del cloud **e** iptables sull'istanza, che di default lascia passare solo SSH.
- **Portatile**: dove stanno i PDF e dove gira la pipeline, lanciata a mano.
- **NAS**: TerraMaster F4-425, **non acquistato**. Resta l'approdo previsto: sistema TOS con supporto Docker, gestione via SSH. Quando arriverà, l'applicazione del vault si sposta lì e cambia solo il bersaglio dell'invio.
- **Rete di casa**: il NAS **non potrà essere collegato via ethernet al router** (TIM Hub+); servirà un bridge WiFi esterno. Implicazione da ricordare al momento del trasloco: banda e latenza verso il NAS non garantite, quindi niente architetture che assumano I/O di rete veloce e continuo verso la cartella note.
- **Accesso remoto**: valutato Tailscale per il solo accesso personale, oppure endpoint pubblico HTTPS con autenticazione. Decisione in §10.4.
- **Competenze**: Python noto, PyMuPDF già usato in un altro progetto. Bot Telegram già funzionante e riutilizzabile per le notifiche.

---

## 4. Decisioni prese — non rimetterle in discussione

Ognuna è già stata discussa e motivata. Se il codice suggerisce di cambiarle, segnalarlo esplicitamente prima di procedere.

| Decisione | Motivo |
|---|---|
| **Note come file `.md` su disco**, non in un database | La pipeline scrive e l'app di note-taking legge. Con file su disco l'output della pipeline **è** la nota: zero integrazione da mantenere. Con Joplin o Trilium servirebbe parlare con un'API |
| **Frontend: un'app che legge e scrive markdown su file**, non un database | Requisito confermato. SilverBullet era la scelta di partenza ma **non è più data per acquisita**: l'esigenza è «semplice e pratico, niente versioning», e SilverBullet porta con sé una sintassi e un modello propri. La scelta va rifatta all'inizio della Fase 2 (§10.6) |
| **Il server è l'unica sede delle note** | I markdown vengono inviati appena generati e **cancellati dal portatile**. Si modificano solo dall'interfaccia dell'applicazione, mai in locale: niente sincronizzazione bidirezionale, niente conflitti, una sola copia autorevole |
| **I JSON restano sul portatile** | Sono la sorgente per rigenerare senza richiamare l'API (criterio 5) e la base della Fase 5. Servono anche all'idempotenza: è il DOI nei JSON locali a dire se un PDF è già stato elaborato. Non sono «la nota», quindi non seguono la sua sorte |
| **Accesso pubblico HTTPS con autenticazione** | Niente VPN: in reparto l'attrito di attivarla ogni volta porterebbe a non consultare le note, che è il fallimento del progetto |
| **L'LLM produce JSON, non markdown** | Struttura identica su tutte le note; il layout si può rigenerare senza richiamare l'API; i JSON sono riutilizzabili dal progetto di knowledge mapping |
| **I metadati bibliografici NON li estrae l'LLM** | DOI via regex dalla prima pagina → interrogazione CrossRef/PubMed. Autori, anno, rivista sono i dati su cui un modello occasionalmente inventa |
| **Polling ogni 60 s, non inotify** | I PDF possono arrivare via SMB o da export automatico. Controllo di stabilità (stessa dimensione e mtime per due cicli) prima di processare, altrimenti si manda all'API un file scritto a metà |
| **Idempotenza via SHA-256 in SQLite** | Protegge dal riavvio del container che riprocessa tutto e dal re-drop dello stesso file |
| **Vocabolario tag controllato passato nel prompt** | Senza, dopo trenta paper si hanno `sepsi`, `sepsis`, `shock-settico` e `settico` come tag distinti |
| **Modello frontier per l'estrazione** | Il costo è irrilevante (vedi §9); l'unica variabile che conta è l'accuratezza nella trascrizione di dosaggi e soglie |
| **Nessun export di backup in markdown** | Serviva solo se lo storage fosse stato un database. Con file nativi è ridondante |

---

## 5. Roadmap per fasi

- **Fase 0 — Template della nota.** ✅ Completata.
- **Fase 1 — Script CLI one-shot.** ⬅️ **Prossimo passo.** `python ingest.py paper.pdf` produce il `.md`. Gira sul portatile. Nessun NAS, nessun Docker, nessun watcher.
- **Fase 2 — Vault consultabile da remoto.** ⬅️ **Prossimo passo.** Applicazione sul VPS che legge e modifica i markdown come file, dietro autenticazione, usabile da desktop e da telefono. Comando di invio dal portatile al server. A questo punto il valore è già in larga parte consegnato, anche con ingestion manuale.
- **Fase 3 — Automazione.** Containerizzazione, loop di polling, SQLite, cartella `failed/`, notifica Telegram.
- **Fase 4 — Tassonomia.** `tags.md` con vocabolario controllato letto dal filesystem e passato nel prompt; pagine indice dinamiche; manutenzione manuale dei tag dall'interfaccia.
- **Fase 5 — To Do.** Note di sintesi multi-paper: un job che prende tutti i JSON con un dato tag e genera una nota unica, rigenerabile. Nessuna riprocessazione dei PDF. È il motivo principale per cui i JSON vanno conservati.

---

## 6. Specifica funzionale — Fase 1

### Interfaccia

```
python ingest.py <path.pdf> [--out DIR] [--json-out DIR] [--model MODEL] [--dry-run]
```

Output: un file `.md` nella cartella note e un `.json` nella cartella intermedi.

### Passi

**6.1 — Estrazione testo.** PyMuPDF. Verificare la presenza di uno strato di testo: se il PDF è scansionato o il testo estratto è sotto una soglia minima di caratteri per pagina, passare da `ocrmypdf` prima di procedere. Usare `get_toc()` per recuperare i bookmark quando presenti: servono in caso di map-reduce.

**6.2 — Gestione documenti lunghi.** Una linea guida da 120 pagine sta attorno ai 150k token e entra in una singola chiamata. **Non implementare il map-reduce subito**: gestire il caso singolo, e prevedere il taglio per sezioni via TOC solo se emerge un documento oltre i limiti. Registrare comunque il conteggio token stimato nei log, per capire quando serve.

**6.3 — Metadati.** Regex per il DOI sulla prima pagina → CrossRef API. Fallback: PubMed. Se entrambi falliscono, lasciare i campi vuoti e segnalarlo, **non** far compilare all'LLM.

**6.4 — Classificazione.** Chiamata breve ed economica che restituisce `narrative review | linea guida | altro`. La logica di classificazione è già scritta nel Passo 1 del prompt.

**6.5 — Estrazione.** Chiamata principale al modello frontier con `prompt-estrazione-v3.md` come system prompt, il vocabolario tag e il testo del documento. **Modifica necessaria rispetto alla v3**: il prompt attualmente chiede markdown in uscita. Per la Fase 1 va convertito a JSON. **Cambiare solo il formato di uscita, non le istruzioni di contenuto**: quelle sono state validate su tre documenti.

**6.6 — Rendering.** Template Jinja2, uno per tipo di documento, che riproducono esattamente la struttura dei golden output.

**6.7 — Output.** Slug: `<anno>-<primo-autore>-<parole-chiave-titolo>.md`. Spostare il PDF elaborato in `library/` e inserire il link relativo nel frontmatter.

### Schema JSON intermedio (bozza da raffinare)

```jsonc
{
  "meta": {
    "title_it": "...",
    "tipo": "narrative review | linea guida | altro",
    "autori": "...", "rivista": "...", "anno": 2026, "doi": "...",
    "societa": null,                    // solo linee guida
    "tags": ["..."], "tags_nuovi": ["..."],
    "pdf": "library/....pdf",
    "criterio_riferimenti": "pagina | sezione",
    "data_nota": "2026-08-20"
  },
  "argomento": "...",                   // review
  "in_breve": ["..."],                  // review, 3-5 voci
  "ambito": "...",                      // linea guida
  "novita": ["..."],                    // linea guida
  "grading_legenda": { "...": "..." },  // linea guida
  "dosaggi": [
    { "ambito": "...", "parametro": "...", "valore": "...",
      "rif": "p. e1007", "ambiguo": false }
  ],
  "sezioni": [ { "titolo": "...", "contenuto_md": "..." } ],
  "raccomandazioni": [
    { "tema": "...", "testo": "...", "cor": "1", "loe": "B-R",
      "razionale": "...", "rif": "p. e1002", "negativa": false }
  ],
  "trappole": [ { "errore": "...", "approccio": "...", "rif": "..." } ],
  "aree_incertezza": ["..."],
  "tabelle": [ { "titolo": "...", "markdown": "...", "rif": "..." } ],
  "bibliografia": { "citazione": "...", "letture_essenziali": ["..."] }
}
```

**Nota di design importante**: la sezione "Cosa non fare" **non è un campo separato**. È una vista generata a render-time filtrando `raccomandazioni[].negativa == true`. Le raccomandazioni negative compaiono sia al loro posto tematico sia nel riepilogo finale — la duplicazione è voluta e vive nel template, non nei dati.

### Criteri di accettazione della Fase 1

1. Sui tre PDF del corpus di test, l'output è **equivalente in contenuto** ai golden output. Differenze di formulazione accettabili; una soglia numerica mancante o sbagliata è un fallimento.
2. Nessun numero di pagina inventato sul documento senza paginazione.
3. Il DOI e i metadati vengono da CrossRef, non dal modello.
4. Rilanciare lo script sullo stesso PDF non produce chiamate API duplicate (una volta introdotto lo stato).
5. Il JSON intermedio è sufficiente a rigenerare il markdown senza richiamare l'API.

---

## 7. Corpus di test

| Documento | Formato | Perché è nel corpus |
|---|---|---|
| Palmer & Clegg, *Pathophysiology of Ketoacidosis: Core Curriculum 2026*, AJKD 2026;88(2):301-314 | PDF 14 pp, nativo digitale, due colonne | Caso base narrative review. Contiene casi clinici e domande a risposta multipla **da scartare**, una tabella sinottica **da riprodurre**, e un unico blocco di dosaggi |
| Creager et al., *2026 AHA/ACC Guideline for Acute Pulmonary Embolism*, Circulation 2026;153:e977-e1051 | PDF 75 pp | Stress test. Oltre 90 raccomandazioni formali, sistema di grading proprio, numerose raccomandazioni negative, molte tabelle |
| Aletras et al., *Diuretic resistance in cardiorenal syndrome*, Front Cardiovasc Med 2026;12:1731305 | HTML open access | **Nessuna paginazione.** Verifica il fallback ai numeri di sezione. Contiene inoltre un'incoerenza interna (timing del sodio urinario: 1-2 h nel testo, 2-6 h nella tabella) che il marcatore di ambiguità deve intercettare |

---

## 8. Trappole già incontrate

- **PDF = immagini.** Nell'interfaccia chat un PDF viene renderizzato una pagina per immagine e satura rapidamente i limiti. Irrilevante per la pipeline, ma è il motivo per cui il lavoro si sposta su Claude Code: lo script lavora su testo estratto e il vincolo sparisce.
- **Documenti senza numeri di pagina.** Frequente nell'open access. Il prompt v3 impone il fallback a `(§3.4)` più una riga dichiarativa in testa alla nota. Verificare che il renderer la emetta.
- **Incoerenze interne alla fonte.** Il marcatore `[formulazione ambigua nella fonte]` esiste per questo e ha già intercettato un caso reale. Non "risolvere" l'ambiguità scegliendo un valore.
- **Tabelle a colonna singola.** La regola sulla compressione tabellare ha un limite opposto già scritto nel prompt: ciò che nell'originale è già un elenco sintetico resta un elenco.
- **Estrazione tabelle con PyMuPDF.** Le tabelle a più colonne vengono linearizzate male. Da verificare sui tre documenti di test; se il degrado è significativo, valutare `pdfplumber` per le sole tabelle.
- **Il rischio clinico è la trascrizione dei numeri.** Dosaggi e soglie sono l'unica parte in cui un errore ha conseguenze reali. Da qui la regola dei riferimenti su ogni valore numerico e la scelta di un modello frontier.

---

## 9. Costi

Circa 15k token di input e 1.500 di output per un paper da 10 pagine. Per documento: da meno di mezzo centesimo con i modelli economici a poco più di venti centesimi con i più costosi. **A 200 paper l'anno la differenza tra la scelta migliore e la peggiore è inferiore a 25 dollari.** Non ottimizzare sul prezzo. Batch API e prompt caching sono disponibili e adatti (la pipeline non ha vincoli di latenza) ma **non vanno implementati nella Fase 1**: complessità che non ripaga a questi volumi.

---

## 10. Questioni aperte

1. **Dove conservare i JSON.** Accanto alla nota o in una cartella separata? La cartella separata tiene pulito il vault di SilverBullet; accanto alla nota semplifica il rigenerare.
2. **Come alimentare la watch folder.** Deposito manuale, oppure collegamento all'export automatico di Zotero — che eliminerebbe del tutto il gesto manuale. Da decidere in Fase 3.
3. **Scelta del modello.** Frontier per l'estrazione, economico per la classificazione. Il modello specifico non è stato fissato.
4. **Endpoint pubblico o VPN.** Tailscale è più semplice e sicuro ma richiede la VPN sempre attiva; un endpoint HTTPS pubblico con autenticazione è più pratico in reparto. Da decidere all'inizio della Fase 2, non dopo: cambia come si configura il server.
6. **Quale applicazione per il vault.** Vincoli: markdown su file, nessun database, modificabile da web e da telefono, autenticazione integrata, niente versioning. Da scegliere all'inizio della Fase 2.
7. **Come inviare.** Se l'applicazione scelta espone un'API HTTP, l'invio è una chiamata e la conferma di ricezione è la risposta. Altrimenti `scp` di un file singolo. In entrambi i casi la nota locale si cancella **solo dopo** aver verificato che il server ce l'ha.
8. **Backup del vault.** Con le note solo sul server, il VPS diventa l'unica copia. I JSON locali permettono di rigenerare le note, ma **non le modifiche fatte dall'interfaccia**. Serve una copia periodica del vault verso casa, o si accetta di poter perdere le correzioni manuali.
9. **Riferimento al PDF.** Resta il percorso locale del portatile: da remoto è un promemoria, non un collegamento. Accettato per questa fase; cambierà col NAS.
5. **Conversione del prompt a output JSON.** Da fare all'inizio della Fase 1 (vedi §6.5). Attenzione a non toccare le istruzioni di contenuto.
6. **Gestione delle immagini e delle figure.** Il prompt chiede di descrivere testualmente le figure con informazione non ridondante. Non è stato deciso se estrarre anche le immagini dal PDF e allegarle alla nota.

---

## 11. Struttura repo proposta

```
paper-pipeline/
├── HANDOVER.md                  ← questo file
├── prompts/
│   ├── estrazione-v3.md         ← prompt corrente
│   ├── estrazione-v2.md
│   └── estrazione-v1.md
├── templates/
│   ├── review.md.j2
│   └── linea-guida.md.j2
├── src/
│   ├── ingest.py                ← entrypoint Fase 1
│   ├── extract.py               ← PyMuPDF, OCR fallback, TOC
│   ├── metadata.py              ← DOI regex + CrossRef/PubMed
│   ├── llm.py                   ← chiamate API, classificazione + estrazione
│   ├── render.py                ← JSON → markdown via Jinja
│   └── state.py                 ← SQLite hash (da Fase 3)
├── tests/
│   ├── corpus/                  ← i tre PDF/HTML di riferimento
│   └── golden/                  ← le tre note di riferimento
├── tags.md                      ← vocabolario controllato (da Fase 4)
└── config.example.toml
```

---

## 12. Fase 2 — stato del server e prossimi passi

### Il VPS, come l'abbiamo trovato e come l'abbiamo lasciato

`ssh -i <chiave> ubuntu@144.24.237.77` — Ubuntu 24.04, **aarch64**, 2 vCPU, 11 GB RAM, 91 GB liberi.
Docker 29.6.2 con Compose v5.3.1, utente già nel gruppo `docker`.

**Le regole di rete sono già a posto**: 22, 80 e 443 aperti sia nella security list Oracle sia in
`iptables`. Era il primo intoppo che mi aspettavo e non c'è.

Girano altri progetti — `shazam-server`, `newsfetcher`, `mdskill` — che non vanno toccati.

**Modifica fatta a Caddy** (backup in `/home/ubuntu/backup-caddy-20260824-141826/`):

- creata la rete Docker **`proxy`**, esterna, che non appartiene a nessun progetto;
- Caddy vi è attaccato **in aggiunta** a `shazam-server_default`. Attenzione: dichiarando le reti
  su un servizio, questo non entra più in automatico in quella del progetto — `default` va elencata
  esplicitamente, o Caddy perde `api` e il sito va in 502;
- il Caddyfile ora fa `import /etc/caddy/sites/*.caddy`, e `/home/ubuntu/caddy-sites/` è montata in
  sola lettura: ogni progetto tiene lì il proprio blocco, senza scriverlo nel compose altrui.

Verificato prima e dopo: il sito esistente risponde identico (HTTP 404 dall'API, TLS valido), zero
errori nel log. Fermo di quattro secondi.

**Dominio del vault**: `nicmaf-vault.duckdns.org`, già puntato a 144.24.237.77. Nessun blocco Caddy
ancora scritto: il vault non è esposto.

### Il vault, in produzione dal 24 agosto 2026

**https://nicmaf-vault.duckdns.org** — flatnotes, dietro autenticazione, con le dieci note.

- `/home/ubuntu/paper-vault/` — progetto compose **a sé**, agganciato alla rete esterna `proxy`.
  Non dipende da nessun altro progetto e si sposterà sul NAS portandosi dietro `./note`.
- `/home/ubuntu/paper-vault/note/` — i markdown, uno per file. I file appartengono a `opc:opc`,
  l'utente del container: per toccarli da shell serve `sudo`.
- `/home/ubuntu/paper-vault/.env` (permessi 600) — utente e password generati sul server. Le
  stesse credenziali stanno nel `.env` locale come `VAULT_UTENTE` e `VAULT_PASSWORD`.
  Per rileggerle: `grep VAULT_ .env`.
- `/home/ubuntu/caddy-sites/paper-vault.caddy` — il blocco Caddy. Il certificato è arrivato da
  solo al primo accesso.

Verificato: dieci note identiche byte per byte fra locale e server, ripubblicazione che non fa
niente (`invariata`), `/api/notes` senza credenziali risponde 401, e il sito preesistente
`nicmaf-shazam.duckdns.org` è rimasto identico per tutta l'operazione.

**L'applicazione è SilverBullet** (`ghcr.io/silverbulletmd/silverbullet`), dal 24 agosto.
Immagine `linux/arm64` nativa, nessun database, e il risalvataggio restituisce il file
**identico byte per byte**.

Prima c'era flatnotes, rimossa: interfaccia mobile scarsa e ricerca incapace di trovare
porzioni di parola. La cartella `note/` non porta traccia del passaggio — indice e cartella
allegati rimossi — e questa è la prova che il vincolo «i file restano i file» ha pagato:
cambiare applicazione è costato un `docker compose up` e la riscrittura dell'adattatore, non
una migrazione di dati.

**L'API è diversa da quella di flatnotes**: SilverBullet espone lo spazio dei file sotto
`/.fs/{pagina}.md` con `GET` e `PUT`, e l'autenticazione è a cookie — si posta il modulo a
`/.auth` e si conserva la sessione. Creare e aggiornare sono lo stesso `PUT`. Le credenziali
stanno in `SB_USER=utente:password`, unica chiave rimasta in `/home/ubuntu/paper-vault/.env`.

La riga di etichette `#tag` in coda alle note resta: serviva a flatnotes, e SilverBullet la
riconosce ugualmente.

### Il ciclo di lavoro

```bash
python -m src.ingest ~/Documents/Papers/nuovo.pdf --dry-run   # gratis, verifica ingresso
python -m src.ingest ~/Documents/Papers/nuovo.pdf --pubblica  # elabora, invia, cancella qui
python -m src.tags                                            # rassegna dei tag proposti
#   … si modifica tags.md a mano: promuovi, accorpa, o respingi con `—`
python -m src.tags --riallinea                                # riscrive i tag, gratis
```

`--pubblica` cancella il markdown locale **solo dopo** aver riletto la nota dal server. Se il
vault non risponde, la nota resta qui e diventa un avviso, non un errore: è già costata una
chiamata.

**Il riallineamento dei tag non ripubblica da solo.** Dopo `--riallinea` le note locali sono
cambiate ma quelle sul server no: vanno rimandate su con `src.pubblica`, che fa la **fusione
chirurgica** — legge la nota dal server, ne tiene il corpo, e sostituisce solo frontmatter ed
etichette. Le correzioni fatte dall'app sopravvivono.

### La prova dei candidati (per memoria)

Tre applicazioni tirate su in locale sul VPS (porte 8091, 8092, 8093, **non esposte**), ciascuna con
una copia delle dieci note vere in `/home/ubuntu/prova-vault/`. Tutte e tre hanno immagine
`linux/arm64` nativa. **Nessuna ha modificato i markdown al solo avvio.**

| | risalvataggio fedele | tag dal frontmatter | cosa lascia nella cartella |
|---|---|---|---|
| **flatnotes** `dullage/flatnotes` | **sì, byte per byte** | **no** | indice di ricerca in `.flatnotes/` (1,2 MB per 10 note), ricostruibile |
| filebrowser `filebrowser/filebrowser` | da provare | non applicabile (è un gestore di file) | niente |
| silverbullet `ghcr.io/silverbulletmd/silverbullet` | da provare | da provare | `.silverbullet.auth.json` |

**flatnotes riconosce i tag solo come `#cancelletto` nel corpo della nota.** Provate tutte e tre le
forme: elenco YAML, inline YAML fra parentesi quadre (la nostra), e cancelletto nel testo. Solo
l'ultima viene indicizzata — da cui la riga di etichette che il renderer aggiunge in coda.

### La forma della nota pubblicata

Il frontmatter YAML **non c'è più nelle note**: flatnotes non lo interpreta e il markdown lo
rendeva come linea orizzontale più blocco in grassetto in cima a ogni pagina. Al suo posto una
riga di citazione in corsivo sotto il titolo. I metadati completi restano nel JSON, che è la
fonte per rigenerare.

Non sono più nella nota: percorso del PDF, tipo di documento, data della nota, e l'asterisco che
distingueva i tag proposti dai validati. Tutto nel JSON; l'asterisco è sorvegliato da
`python -m src.tags`.

`src/pubblica.py` possiede quindi **testa** (titolo e citazione) ed **etichette**; il corpo è del
server. Attenzione: quando cambia la *struttura* della nota la fusione chirurgica non basta —
conserva fedelmente il corpo del server, che è quello vecchio. Serve `sovrascrivi=True`, che
rimpiazza la nota per intero. È successo alla migrazione del 24 agosto: la prima ripubblicazione
ha lasciato il vecchio frontmatter incastrato sotto la testa nuova.

### Che cosa resta da fare

1. **Le tabelle larghe su schermo di telefono.** Provate il 24 agosto: la nota è consultabile,
   ma la larghezza delle tabelle sporca l'esperienza. Accettato per ora. Se un giorno dà troppo
   fastidio, la strada è il renderer — meno colonne, o le tabelle dosaggi rese come elenco su
   schermo stretto — e costa una rigenerazione dai JSON, senza chiamate.
2. **Le dieci note locali** sono ancora in `note/`: sono state pubblicate senza cancellarle, per
   poter confrontare. Si possono rimuovere quando la prova dal telefono è andata.
3. **Il backup del vault** (§10.8): il server è ora l'unica sede delle note, e i JSON locali
   rigenerano il testo originale ma non le correzioni fatte dall'app.

### Cose da non dimenticare

- **Il vault sul VPS sarà l'unica copia delle note.** I JSON locali rigenerano il testo originale ma
  **non le correzioni fatte dall'interfaccia**. Vedi §10.8.
- Le istanze del piano gratuito Oracle vengono reclamate se restano inattive.
- Le dieci note esistenti portano ancora i tag di quando sono state scritte, non quelli di `tags.md`.
  Riallinearle costa dieci chiamate, oppure si modificano i tag nei dieci JSON e si rigenera il solo
  markdown, che è gratis. La tabella degli accorpamenti in `tags.md` contiene tutte le corrispondenze.
- La cartella delle note sta in **`~/Documents/Papers/note/`**, accanto ai PDF, e non più dentro
  il progetto: i dati dell'utente e il codice non si mescolano. In locale contiene solo i JSON —
  i markdown ci passano il tempo fra la generazione e la pubblicazione. La precedente scelta di non pubblicare il codice è stata superata dalla richiesta del 5 ottobre 2026: repository privato, senza dati e credenziali.


## 13. Figure selezionate dalla sintesi operativa (ottobre 2026)

**Criterio concordato con l'utente:** includere le figure citate nei segmenti originali
usati nel riassunto operativo finale, anche se il richiamo non è ripetuto nella parafrasi.
Non selezionare per semplice affinità tematica e non estrarre indiscriminatamente ogni immagine.

- Il modello restituisce `nota.figure_richieste[]` con `numero`, `passaggio_fonte`
  testuale e `sezione_nota`. Schema e prompt sono retrocompatibili (campo facoltativo).
- `src.figure` verifica citazione, richiamo e destinazione; rende in PNG una regione
  della pagina originale, preservando vettori, scritte e pannelli. Non genera né traduce
  contenuto visivo. Le proposte devono essere approvate o escluse dall'anteprima locale.
- L'involucro JSON conserva `figure[]`: selezione, didascalia originale, pagina fisica PDF,
  bounding box in punti, percorso relativo, SHA-256, DPI e stato. Le immagini persistono
  anche sul Mac sotto `note/assets/<slug>/`; questo estende la vecchia regola «solo JSON».
- Il renderer colloca ogni immagine presso la sezione selezionata. Un marcatore impedisce
  la pubblicazione di note con figure ancora da verificare; l'estrazione LLM già pagata
  resta salvata e un ingest successivo la riutilizza.
- Il publisher carica e verifica byte per byte le immagini prima della nota e confronta
  anche il Markdown riletto. I nomi immagine contengono un hash; non sovrascrive allegati
  remoti diversi. La fusione conserva il corpo e i blocchi figura remoti, aggiunge solo i
  blocchi mancanti e si ferma se la sezione è assente/ambigua. Non cancella vecchi allegati.
- `src.scarica` salva Markdown e allegati binari, mantenendo gli orfani e omettendo file
  nascosti/sessioni. Le scritture del backup sostituiscono atomicamente la copia precedente.
- Limiti della prima versione (superati per gli URL dal §14): recupero immagini da PDF; scansioni e impaginazioni
  ambigue richiedono intervento. Figure multipagina non assemblate automaticamente.
- Validazione: fixture sintetici con modello/vault simulati e ispezione visuale sul corpus.
  Nessuna nuova chiamata LLM reale e nessuna modifica alle note di produzione durante lo sviluppo.


## 14. Ingresso full-text web e repository privato (5 ottobre 2026)

`src.ingest` e `src.extract` accettano URL HTTP(S). `src/web_article.py` acquisisce il corpo, i metadati DOI dichiarati e il catalogo immagini; il resto della pipeline e il criterio delle figure sono condivisi con i PDF. Il JSON conserva provenienza, snapshot testuale e catalogo per riprendere senza LLM. Gli originali PNG/JPEG/WebP selezionati vengono scaricati e verificati, poi passano dalla stessa revisione. `src.figure --riprova` ripete i recuperi falliti. Consultare il README per limiti, comandi, backup e infrastruttura.
