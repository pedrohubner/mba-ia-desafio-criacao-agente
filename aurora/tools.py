from __future__ import annotations

from typing import Any

from google.adk.tools import ToolContext

from . import condominio, regulamento


class SessaoSemApartamento(Exception):
    pass


def apartamento_da_sessao(tool_context: ToolContext) -> str:
    apartamento = tool_context.session.user_id
    if not condominio.apartamento_existe(apartamento):
        raise SessaoSemApartamento(f"Sessão sem apartamento válido: {apartamento!r}")
    return apartamento


def _erro(mensagem: str) -> dict[str, Any]:
    return {"status": "erro", "mensagem": mensagem}


def _area_e_data(area: str, data: str) -> tuple[condominio.Area, str] | dict[str, Any]:
    area_obj = condominio.resolver_area(area)
    if area_obj is None:
        nomes = ", ".join(f"{a.id} ({a.nome})" for a in condominio.listar_areas())
        return _erro(f"Área desconhecida: {area!r}. Áreas disponíveis: {nomes}.")
    try:
        return area_obj, condominio.validar_data(data)
    except ValueError:
        return _erro(f"Data inválida: {data!r}. Use o formato AAAA-MM-DD.")


def listar_areas() -> dict[str, Any]:
    """Lista as áreas comuns reserváveis, com a taxa de cada uma.

    Taxa maior que zero gera cobrança e exige confirmação do morador.
    """
    return {
        "areas": [
            {"id": a.id, "nome": a.nome, "taxa": a.taxa, "gera_cobranca": a.gera_cobranca}
            for a in condominio.listar_areas()
        ]
    }


def consultar_disponibilidade(area: str, data: str) -> dict[str, Any]:
    """Informa se uma área comum está livre ou ocupada em uma data.

    Args:
        area: id ou nome da área (ex.: "salao-de-festas", "churrasqueira", "quadra").
        data: data no formato AAAA-MM-DD.
    """
    validado = _area_e_data(area, data)
    if isinstance(validado, dict):
        return validado
    area_obj, data_iso = validado
    ocupada = condominio.data_ocupada(area_obj.id, data_iso)
    return {"area": area_obj.id, "data": data_iso, "disponivel": not ocupada}


def listar_minhas_reservas(tool_context: ToolContext) -> dict[str, Any]:
    """Lista as reservas ativas do apartamento do morador desta conversa."""
    apartamento = apartamento_da_sessao(tool_context)
    return {"reservas": condominio.reservas_do_apartamento(apartamento)}


def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict[str, Any]:
    """Reserva uma área comum para o apartamento do morador desta conversa.

    Áreas com taxa geram cobrança: nesse caso o sistema pede a confirmação do
    morador por um canal próprio e a reserva só é gravada se ele aprovar.

    Args:
        area: id ou nome da área (ex.: "salao-de-festas", "churrasqueira", "quadra").
        data: data da reserva no formato AAAA-MM-DD.
    """
    apartamento = apartamento_da_sessao(tool_context)
    validado = _area_e_data(area, data)
    if isinstance(validado, dict):
        return validado
    area_obj, data_iso = validado

    if area_obj.gera_cobranca:
        confirmacao = tool_context.tool_confirmation
        if confirmacao is None:
            if condominio.data_ocupada(area_obj.id, data_iso):
                return {"status": "indisponivel", "area": area_obj.id, "data": data_iso,
                        "mensagem": "A área já está reservada nessa data."}
            tool_context.request_confirmation(
                hint=(f"Confirmar reserva de {area_obj.nome} em {data_iso}"
                      f" com cobrança de R$ {area_obj.taxa:.2f}?"),
                payload={"area": area_obj.id, "nome_area": area_obj.nome,
                         "data": data_iso, "taxa": area_obj.taxa},
            )
            tool_context.actions.skip_summarization = True
            return {"status": "aguardando_confirmacao", "area": area_obj.id, "data": data_iso,
                    "mensagem": "Reserva pendente de confirmação do morador."}
        if not confirmacao.confirmed:
            return {"status": "nao_confirmada", "area": area_obj.id, "data": data_iso,
                    "mensagem": "O morador negou a confirmação. Nada foi reservado."}

    try:
        codigo = condominio.criar_reserva(apartamento, area_obj.id, data_iso)
    except condominio.DataOcupada:
        return {"status": "indisponivel", "area": area_obj.id, "data": data_iso,
                "mensagem": "A área já está reservada nessa data."}
    return {"status": "reservada", "codigo": codigo, "area": area_obj.id,
            "data": data_iso, "taxa": area_obj.taxa,
            "mensagem": (
                "Reserva concluída e gravada. A cobrança da taxa já foi confirmada pelo morador."
                if area_obj.gera_cobranca
                else "Reserva concluída e gravada. Área sem taxa, sem cobrança."
            )}


def cancelar_reserva(area: str, data: str, tool_context: ToolContext) -> dict[str, Any]:
    """Cancela uma reserva do próprio apartamento do morador desta conversa.

    Args:
        area: id ou nome da área reservada.
        data: data da reserva no formato AAAA-MM-DD.
    """
    apartamento = apartamento_da_sessao(tool_context)
    validado = _area_e_data(area, data)
    if isinstance(validado, dict):
        return validado
    area_obj, data_iso = validado
    codigo = condominio.cancelar_reserva(apartamento, area_obj.id, data_iso)
    if codigo is None:
        return {"status": "nao_encontrada", "area": area_obj.id, "data": data_iso,
                "mensagem": "Você não tem reserva ativa dessa área nessa data."}
    return {"status": "cancelada", "codigo": codigo, "area": area_obj.id, "data": data_iso}


def listar_meus_visitantes(tool_context: ToolContext) -> dict[str, Any]:
    """Lista os visitantes autorizados pelo apartamento do morador desta conversa."""
    apartamento = apartamento_da_sessao(tool_context)
    return {"visitantes": condominio.visitantes_do_apartamento(apartamento)}


def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict[str, Any]:
    """Autoriza a entrada de um visitante no prédio em uma data.

    Liberar acesso sempre exige a confirmação do morador por um canal próprio;
    a autorização só é gravada se ele aprovar.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.
    """
    apartamento = apartamento_da_sessao(tool_context)
    nome = " ".join(nome.split())
    if not nome:
        return _erro("Informe o nome do visitante.")
    try:
        data_iso = condominio.validar_data(data)
    except ValueError:
        return _erro(f"Data inválida: {data!r}. Use o formato AAAA-MM-DD.")

    confirmacao = tool_context.tool_confirmation
    if confirmacao is None:
        tool_context.request_confirmation(
            hint=f"Confirmar a entrada de {nome} em {data_iso}?",
            payload={"nome": nome, "data": data_iso},
        )
        tool_context.actions.skip_summarization = True
        return {"status": "aguardando_confirmacao", "nome": nome, "data": data_iso,
                "mensagem": "Autorização pendente de confirmação do morador."}
    if not confirmacao.confirmed:
        return {"status": "nao_confirmada", "nome": nome, "data": data_iso,
                "mensagem": "O morador negou a confirmação. Nada foi autorizado."}

    condominio.autorizar_visitante(apartamento, nome, data_iso)
    return {"status": "autorizado", "nome": nome, "data": data_iso,
            "mensagem": "Entrada confirmada pelo morador, autorizada e gravada. Nada mais a fazer."}


def consultar_capitulo(numero: int) -> dict[str, Any]:
    """Devolve o texto de UM capítulo do regulamento interno.

    Args:
        numero: número do capítulo (1 a 14), conforme o índice.
    """
    cap = regulamento.capitulo(numero)
    if cap is None:
        return _erro(f"Capítulo {numero} não existe. Consulte o índice.")
    return {"capitulo": f"{cap.romano}: {cap.titulo}", "texto": cap.texto}
