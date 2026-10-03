from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache

from . import config

_CABECALHO = re.compile(r"^## Capítulo ([IVXLC]+): (.+)$", re.MULTILINE)
_ROMANOS = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


@dataclass(frozen=True)
class Capitulo:
    numero: int
    romano: str
    titulo: str
    texto: str


def _romano_para_int(romano: str) -> int:
    total = 0
    for atual, proximo in zip(romano, romano[1:] + " "):
        valor = _ROMANOS[atual]
        total += -valor if _ROMANOS.get(proximo, 0) > valor else valor
    return total


@cache
def capitulos() -> tuple[Capitulo, ...]:
    texto = (config.DADOS_DIR / "regulamento.md").read_text(encoding="utf-8")
    cabecalhos = list(_CABECALHO.finditer(texto))
    resultado = []
    for i, cab in enumerate(cabecalhos):
        fim = cabecalhos[i + 1].start() if i + 1 < len(cabecalhos) else len(texto)
        resultado.append(
            Capitulo(
                numero=_romano_para_int(cab.group(1)),
                romano=cab.group(1),
                titulo=cab.group(2).strip(),
                texto=texto[cab.start():fim].strip(),
            )
        )
    return tuple(resultado)


def indice() -> str:
    return "\n".join(f"{c.numero}. Capítulo {c.romano}: {c.titulo}" for c in capitulos())


def capitulo(numero: int) -> Capitulo | None:
    return next((c for c in capitulos() if c.numero == numero), None)
