"""Validazione del JSON intermedio contro `schema/nota.schema.json`.

Il JSON è il punto di passaggio fra il modello e il renderer, ed è anche l'artefatto che
sopravvive: la Fase 5 ci ricostruirà le note di sintesi senza riaprire i PDF. Vale quindi la
pena rifiutarlo subito quando non è conforme, invece di scoprirlo mesi dopo.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema

PERCORSO_SCHEMA = Path(__file__).resolve().parents[1] / "schema" / "nota.schema.json"


class NotaNonValida(ValueError):
    """Il JSON prodotto dal modello non è conforme allo schema."""


@cache
def carica_schema() -> dict[str, Any]:
    return json.loads(PERCORSO_SCHEMA.read_text(encoding="utf-8"))


@cache
def _validatore() -> jsonschema.protocols.Validator:
    schema = carica_schema()
    classe = jsonschema.validators.validator_for(schema)
    classe.check_schema(schema)
    return classe(schema)


def errori(nota: dict[str, Any]) -> list[str]:
    """Gli errori di conformità, in ordine di posizione nel documento."""
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '(radice)'}: {e.message}"
        for e in sorted(_validatore().iter_errors(nota), key=lambda e: list(e.absolute_path))
    ]


def valida(nota: dict[str, Any]) -> dict[str, Any]:
    """Restituisce la nota se è conforme, altrimenti alza `NotaNonValida` con tutti gli errori."""
    trovati = errori(nota)
    if trovati:
        raise NotaNonValida(
            f"{len(trovati)} errori nel JSON estratto:\n  - " + "\n  - ".join(trovati)
        )
    return nota


def carica(percorso: str | Path) -> dict[str, Any]:
    return valida(json.loads(Path(percorso).read_text(encoding="utf-8")))
