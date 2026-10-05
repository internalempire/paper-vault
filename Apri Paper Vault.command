#!/bin/zsh
# Avvio personale dal Finder; resta aperto durante i lavori.
cd -- "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  print 'Manca l’ambiente Python. Segui la sezione Installazione del README.'
  read '?Premi Invio per chiudere.'
  exit 1
fi
.venv/bin/python -m src.webapp --open
