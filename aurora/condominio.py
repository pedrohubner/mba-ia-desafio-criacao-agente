from __future__ import annotations

import json
import secrets
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS apartamentos (
    numero  TEXT PRIMARY KEY,
    morador TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS areas (
    id   TEXT PRIMARY KEY,
    nome TEXT NOT NULL,
    taxa REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS codigos_emitidos (
    codigo TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS reservas (
    codigo      TEXT PRIMARY KEY REFERENCES codigos_emitidos(codigo),
    apartamento TEXT NOT NULL,
    area        TEXT NOT NULL REFERENCES areas(id),
    data        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ativa' CHECK (status IN ('ativa', 'cancelada'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_reserva_ativa
    ON reservas(area, data) WHERE status = 'ativa';
CREATE TABLE IF NOT EXISTS visitantes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL,
    nome        TEXT NOT NULL,
    data        TEXT NOT NULL,
    UNIQUE (apartamento, nome, data)
);
CREATE TABLE IF NOT EXISTS sessoes (
    session_id  TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL
);
"""


class DataOcupada(Exception):
    pass


@dataclass(frozen=True)
class Area:
    id: str
    nome: str
    taxa: float

    @property
    def gera_cobranca(self) -> bool:
        return self.taxa > 0


@contextmanager
def conectar() -> Iterator[sqlite3.Connection]:
    config.VAR_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.CONDOMINIO_DB, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transacao() -> Iterator[sqlite3.Connection]:
    with conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def _ler_json(nome: str) -> list[dict]:
    return json.loads((config.DADOS_DIR / nome).read_text(encoding="utf-8"))


def inicializar() -> None:
    with conectar() as conn:
        conn.executescript(SCHEMA)
        vazio = conn.execute("SELECT COUNT(*) FROM apartamentos").fetchone()[0] == 0
    if vazio:
        restaurar()


def restaurar() -> None:
    with conectar() as conn:
        conn.executescript(SCHEMA)
    with transacao() as conn:
        for tabela in ("visitantes", "reservas", "areas", "apartamentos", "sessoes"):
            conn.execute(f"DELETE FROM {tabela}")
        conn.executemany(
            "INSERT INTO apartamentos (numero, morador) VALUES (:numero, :morador)",
            _ler_json("apartamentos.json"),
        )
        conn.executemany(
            "INSERT INTO areas (id, nome, taxa) VALUES (:id, :nome, :taxa)",
            _ler_json("areas.json"),
        )
        reservas = _ler_json("reservas.json")
        conn.executemany(
            "INSERT OR IGNORE INTO codigos_emitidos (codigo) VALUES (:codigo)",
            reservas,
        )
        conn.executemany(
            "INSERT INTO reservas (codigo, apartamento, area, data)"
            " VALUES (:codigo, :apartamento, :area, :data)",
            reservas,
        )
        conn.executemany(
            "INSERT INTO visitantes (apartamento, nome, data)"
            " VALUES (:apartamento, :nome, :data)",
            _ler_json("visitantes.json"),
        )


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return "-".join(sem_acento.lower().replace("_", " ").replace("-", " ").split())


def validar_data(texto: str) -> str:
    return date.fromisoformat(texto.strip()).isoformat()


def apartamento_existe(numero: str) -> bool:
    with conectar() as conn:
        return conn.execute(
            "SELECT 1 FROM apartamentos WHERE numero = ?", (numero,)
        ).fetchone() is not None


def morador(numero: str) -> str | None:
    with conectar() as conn:
        row = conn.execute(
            "SELECT morador FROM apartamentos WHERE numero = ?", (numero,)
        ).fetchone()
    return row["morador"] if row else None


def listar_areas() -> list[Area]:
    with conectar() as conn:
        rows = conn.execute("SELECT id, nome, taxa FROM areas ORDER BY nome").fetchall()
    return [Area(r["id"], r["nome"], r["taxa"]) for r in rows]


def resolver_area(texto: str) -> Area | None:
    alvo = _normalizar(texto)
    areas = listar_areas()
    for area in areas:
        if alvo in (area.id, _normalizar(area.nome)):
            return area
    candidatas = [a for a in areas if alvo and (alvo in a.id or alvo in _normalizar(a.nome))]
    return candidatas[0] if len(candidatas) == 1 else None


def data_ocupada(area_id: str, data: str) -> bool:
    with conectar() as conn:
        return conn.execute(
            "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'",
            (area_id, data),
        ).fetchone() is not None


def reservas_do_apartamento(apartamento: str) -> list[dict]:
    with conectar() as conn:
        rows = conn.execute(
            "SELECT codigo, area, data FROM reservas"
            " WHERE apartamento = ? AND status = 'ativa' ORDER BY data, area",
            (apartamento,),
        ).fetchall()
    return [dict(r) for r in rows]


def _novo_codigo(conn: sqlite3.Connection) -> str:
    while True:
        codigo = f"RSV-{secrets.randbelow(900_000) + 100_000}"
        try:
            conn.execute("INSERT INTO codigos_emitidos (codigo) VALUES (?)", (codigo,))
            return codigo
        except sqlite3.IntegrityError:
            continue


def criar_reserva(apartamento: str, area_id: str, data: str) -> str:
    try:
        with transacao() as conn:
            codigo = _novo_codigo(conn)
            conn.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
                (codigo, apartamento, area_id, data),
            )
    except sqlite3.IntegrityError as exc:
        if "reservas.area" in str(exc) or "ux_reserva_ativa" in str(exc):
            raise DataOcupada from exc
        raise
    return codigo


def cancelar_reserva(apartamento: str, area_id: str, data: str) -> str | None:
    with transacao() as conn:
        row = conn.execute(
            "SELECT codigo FROM reservas WHERE apartamento = ? AND area = ? AND data = ?"
            " AND status = 'ativa'",
            (apartamento, area_id, data),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE reservas SET status = 'cancelada' WHERE codigo = ?", (row["codigo"],)
        )
        return row["codigo"]


def visitantes_do_apartamento(apartamento: str) -> list[dict]:
    with conectar() as conn:
        rows = conn.execute(
            "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, nome",
            (apartamento,),
        ).fetchall()
    return [dict(r) for r in rows]


def autorizar_visitante(apartamento: str, nome: str, data: str) -> bool:
    with transacao() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
            (apartamento, nome, data),
        )
        return cur.rowcount == 1


def registrar_sessao(session_id: str, apartamento: str) -> None:
    with transacao() as conn:
        conn.execute(
            "INSERT INTO sessoes (session_id, apartamento) VALUES (?, ?)",
            (session_id, apartamento),
        )


def apartamento_da_sessao(session_id: str) -> str | None:
    with conectar() as conn:
        row = conn.execute(
            "SELECT apartamento FROM sessoes WHERE session_id = ?", (session_id,)
        ).fetchone()
    return row["apartamento"] if row else None
