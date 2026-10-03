from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService, Session
from google.genai import types
from pydantic import BaseModel

from . import condominio, config
from .agent import app as adk_app

logger = logging.getLogger("aurora.api")

CONFIRMACAO = "adk_request_confirmation"


class NovaSessao(BaseModel):
    apartamento: str


class NovaMensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


class Estado:
    runner: Runner
    sessoes: DatabaseSessionService
    travas: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


estado = Estado()


@asynccontextmanager
async def lifespan(_: FastAPI):
    condominio.inicializar()
    config.VAR_DIR.mkdir(parents=True, exist_ok=True)
    estado.sessoes = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{config.SESSOES_DB}")
    estado.runner = Runner(app=adk_app, session_service=estado.sessoes)
    yield
    await estado.runner.close()


api = FastAPI(title="Residencial Aurora", lifespan=lifespan)


async def _carregar_sessao(session_id: str) -> Session:
    apartamento = condominio.apartamento_da_sessao(session_id)
    sessao = None
    if apartamento is not None:
        sessao = await estado.sessoes.get_session(
            app_name=config.APP_NAME, user_id=apartamento, session_id=session_id
        )
    if sessao is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    return sessao


def confirmacoes_pendentes(sessao: Session) -> list[dict[str, Any]]:
    pedidos: dict[str, dict[str, Any]] = {}
    respondidos: set[str] = set()
    for evento in sessao.events:
        for chamada in evento.get_function_calls():
            if chamada.name == CONFIRMACAO and chamada.id:
                pedidos[chamada.id] = dict(chamada.args or {})
        for resposta in evento.get_function_responses():
            if resposta.name == CONFIRMACAO and resposta.id:
                respondidos.add(resposta.id)

    pendentes = []
    for id_, args in pedidos.items():
        if id_ in respondidos:
            continue
        original = args.get("originalFunctionCall") or {}
        pedido = args.get("toolConfirmation") or {}
        pendentes.append({
            "id": id_,
            "acao": original.get("name", ""),
            "detalhes": pedido.get("payload") or original.get("args") or {},
        })
    return pendentes


def _texto(evento: Event) -> list[str]:
    if evento.author == "user" or not evento.content or not evento.content.parts:
        return []
    return [p.text for p in evento.content.parts if p.text and not p.thought]


async def _executar(sessao: Session, mensagem: types.Content) -> dict[str, Any]:
    textos: list[str] = []
    try:
        async for evento in estado.runner.run_async(
            user_id=sessao.user_id, session_id=sessao.id, new_message=mensagem
        ):
            textos.extend(_texto(evento))
    except Exception as exc:
        logger.exception("Falha ao executar o agente")
        raise HTTPException(status_code=502, detail=f"Falha ao executar o assistente: {exc}")

    atualizada = await _carregar_sessao(sessao.id)
    return {
        "resposta": "\n".join(t.strip() for t in textos if t.strip()),
        "confirmacoes_pendentes": confirmacoes_pendentes(atualizada),
    }


@api.post("/sessoes", status_code=201)
async def criar_sessao(corpo: NovaSessao) -> dict[str, str]:
    apartamento = corpo.apartamento.strip()
    morador = condominio.morador(apartamento)
    if morador is None:
        raise HTTPException(status_code=422, detail="Apartamento inexistente.")
    sessao = await estado.sessoes.create_session(
        app_name=config.APP_NAME,
        user_id=apartamento,
        state={"apartamento": apartamento},
    )
    condominio.registrar_sessao(sessao.id, apartamento)
    return {"session_id": sessao.id}


@api.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: NovaMensagem) -> dict[str, Any]:
    async with estado.travas[session_id]:
        sessao = await _carregar_sessao(session_id)
        mensagem = types.Content(role="user", parts=[types.Part(text=corpo.texto)])
        return await _executar(sessao, mensagem)


@api.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, corpo: RespostaConfirmacao) -> dict[str, Any]:
    async with estado.travas[session_id]:
        sessao = await _carregar_sessao(session_id)
        if corpo.id not in {p["id"] for p in confirmacoes_pendentes(sessao)}:
            raise HTTPException(
                status_code=409,
                detail="Não existe confirmação pendente com esse id nesta sessão.",
            )
        resposta = types.Part(
            function_response=types.FunctionResponse(
                name=CONFIRMACAO,
                id=corpo.id,
                response={"confirmed": corpo.confirmado},
            )
        )
        return await _executar(sessao, types.Content(role="user", parts=[resposta]))


@api.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str) -> list[dict[str, Any]]:
    sessao = await _carregar_sessao(session_id)
    return [e.model_dump(mode="json", by_alias=True, exclude_none=True) for e in sessao.events]


@api.get("/apartamentos/{numero}/reservas")
def reservas_do_apartamento(numero: str) -> list[dict[str, Any]]:
    return condominio.reservas_do_apartamento(numero)


@api.get("/apartamentos/{numero}/visitantes")
def visitantes_do_apartamento(numero: str) -> list[dict[str, Any]]:
    return condominio.visitantes_do_apartamento(numero)
