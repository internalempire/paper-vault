# Vocabolario controllato

Elenco dei tag ammessi. `ingest.py` lo legge a ogni corsa e lo passa nel prompt: il modello
riutilizza questi ogni volta che sono adeguati, e propone un tag nuovo solo se nessuno copre un
concetto centrale del documento. I tag proposti finiscono in `tags_nuovi`, escono nella nota con
l'asterisco e compaiono fra gli avvisi — vanno validati qui a mano, oppure lasciati cadere.

Senza questo file ogni nota inventa la propria tassonomia: le prime dieci ne hanno prodotti 68
distinti su 72 occorrenze, cioè quasi nessun riuso.

**Regole di forma**: minuscolo, parole separate da trattini, niente spazi. Sigle e acronimi
restano come si scrivono (`VExUS`, `ARDS`, `PEEP`, `ERCP`, `PERT`, `CTEPD`), e così i nomi
propri (`equazione-di-Starling`). Tutto il resto in italiano.

**Solo le righe di elenco sono tag.** Titoli, testo e tabelle di questo file vengono ignorati,
quindi si può documentare senza sporcare il vocabolario.

## Quadri clinici

- shock
- shock-settico
- shock-indifferenziato
- sepsi
- arresto-cardiaco
- insufficienza-d-organo
- ARDS
- edema-polmonare
- edema-polmonare-cardiogenico
- embolia-polmonare
- pancreatite-acuta
- pancreatite-necrotizzante
- scompenso-cardiaco
- scompenso-destro
- sindrome-cardiorenale
- insufficienza-renale-acuta
- insufficienza-renale-cronica

## Fisiologia ed emodinamica

- emodinamica
- ritorno-venoso
- congestione-venosa
- microcircolazione
- accoppiamento-ventricolo-arterioso
- interazione-cuore-polmone
- tolleranza-ai-fluidi
- equazione-di-Starling

## Monitoraggio e diagnostica

- ecocardiografia
- ecografia-polmonare
- ecografia-point-of-care
- VExUS
- sodio-urinario
- equilibrio-acido-base

## Terapia

- rianimazione
- decongestione
- vasopressori
- noradrenalina
- vasopressina
- diuretici-dell-ansa
- resistenza-diuretica
- anticoagulazione
- trombolisi
- trombectomia-meccanica
- terapia-renale-sostitutiva-continua
- nutrizione-enterale
- PEEP
- ventilazione-meccanica
- sedazione
- ERCP

## Metabolismo ed elettroliti

- iperkaliemia
- emergenze-elettrolitiche
- chetoacidosi
- chetoacidosi-diabetica
- chetoacidosi-alcolica
- chetoacidosi-euglicemica
- chetosi-da-digiuno
- intossicazione-da-salicilati

## Farmaci e classi

- SGLT2-inibitori
- RAAS-inibitori

## Popolazioni e contesti

- terapia-intensiva
- gravidanza
- obesita
- fragilita
- CTEPD
- PERT

## Accorpamenti applicati

Tag proposti dalle note, e in cosa sono confluiti. Se un accorpamento ti sembra sbagliato,
rimetti il tag originale nell'elenco qui sopra: le note già scritte non cambiano — i tag stanno
nel frontmatter — ma le prossime useranno la forma che scegli.

**Questa tabella è anche il registro delle decisioni prese.** `python -m src.tags` la legge: un
tag che compare qui non torna più nella rassegna, e la sua destinazione conta come usata anche
finché le note portano ancora la forma vecchia. Per respingere una proposta senza accorparla,
aggiungi una riga con `—` nella colonna di mezzo.

| Proposto | Confluito in | Perché |
|---|---|---|
| `cardiorenal-syndrome` | `sindrome-cardiorenale` | i tag sono in italiano |
| `diuretic-resistance` | `resistenza-diuretica` | idem |
| `heart-failure` | `scompenso-cardiaco` | idem |
| `right-heart-failure` | `scompenso-destro` | idem |
| `loop-diuretics` | `diuretici-dell-ansa` | idem |
| `chronic-kidney-disease` | `insufficienza-renale-cronica` | idem |
| `urinary-sodium` | `sodio-urinario` | idem |
| `decongestion` | `decongestione` | idem |
| `obesity`, `frailty` | `obesita`, `fragilita` | idem |
| `fluid-tolerance` | `tolleranza-ai-fluidi` | idem; è una frase, non un acronimo |
| `norepinefrina` | `noradrenalina` | forma italiana corrente |
| `embolia-polmonare-acuta` | `embolia-polmonare` | la forma cronica ha un tag suo (`CTEPD`) |
| `malattia-renale-cronica` | `insufficienza-renale-cronica` | due nomi per la stessa cosa |
| `fisiologia-cardiovascolare` | `emodinamica` | il primo non seleziona niente |
| `coerenza-emodinamica` | `emodinamica` + `microcircolazione` | concetto di un singolo paper |
| `ecocardiografia-in-shock`, `ecocardiografia-critica` | `ecocardiografia` + `shock` | la combinazione dice già tutto |
| `rianimazione-dello-shock` | `rianimazione` + `shock` | idem |
| `iperkaliemia-farmacologica` | `iperkaliemia` + `RAAS-inibitori` | idem |
| `interstizio-polmonare`, `clearance-dell-edema` | `edema-polmonare` | troppo specifici per selezionare |
| `CTEPH` | `CTEPD` | la seconda comprende la prima |

## Tag aggiunti senza che una nota li abbia proposti

Solo dove la famiglia era palesemente monca e l'asimmetria avrebbe prodotto churn al primo
paper: `sepsi` (c'era solo `shock-settico`), `embolia-polmonare` in forma generale,
`ventilazione-meccanica`, `sedazione` e `interazione-cuore-polmone` (raccomandazioni presenti
nella linea guida AHA/ACC ma non emerse come tag), `terapia-intensiva` (usato nei golden).
