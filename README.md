# Residencial Aurora: assistente virtual com Google ADK

Assistente do aplicativo dos moradores do Residencial Aurora, construído com
**Google ADK 2.11.0** e exposto por uma API **FastAPI** em `http://localhost:8000`.
Pelo chat, o morador reserva o salão de festas, a churrasqueira e a quadra,
cancela as próprias reservas, autoriza visitantes e tira dúvidas sobre o
regulamento.

A filosofia do projeto: **o modelo decide o caminho, o código decide o que é
permitido.** Nenhuma regra crítica mora só no prompt. Todas estão em código e
continuam valendo não importa o que o morador escreva.

```
aurora/
├── agent.py        # agentes (principal + 3 especialistas) e o App do ADK
├── tools.py        # tools dos especialistas (única porta de acesso aos dados)
├── condominio.py   # armazenamento SQLite do condomínio e regras de dados
├── regulamento.py  # leitura do regulamento por capítulo
├── api.py          # API FastAPI (contrato do desafio)
├── restaurar.py    # comando de restauração dos dados
└── config.py       # caminhos, nomes e modelos
dados/              # estado inicial (intocado)
var/                # estado vivo, criado em tempo de execução (fora do Git)
```

## Arquitetura

```
                    morador
                       │  POST /sessoes/{id}/mensagens · /confirmacoes
                       ▼
               ┌───────────────┐
               │   concierge   │  agente principal (sem o regulamento nas instruções)
               └───────┬───────┘
     transferência     │ transferência        ferramenta (AgentTool)
   ┌───────────────────┼────────────────────────────┐
   ▼                   ▼                            ▼
┌──────────┐     ┌────────────┐              ┌─────────────┐
│ reservas │◄───►│ visitantes │              │ regulamento │ sessão própria,
└────┬─────┘     └─────┬──────┘              └──────┬──────┘ descartável
     │ tools           │ tools                      │ consultar_capitulo
     ▼                 ▼                            ▼
  var/condominio.db (SQLite)                 dados/regulamento.md (1 capítulo por vez)
```

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| `concierge` (principal) | Conversa com o morador, responde cumprimentos e distribui cada pedido ao especialista certo. Não tem tools de dados. | Recebe toda mensagem nova (raiz do `App`). | Um ponto de entrada só, com instruções curtas e baratas. Ele não recebe o regulamento. |
| `reservas` | Lista as áreas, consulta a disponibilidade, lista, cria e cancela reservas do morador. | **Transferência** (`sub_agents` do principal). | `reservar_area` pede confirmação. A resposta da confirmação precisa voltar ao agente que fez a chamada original, **na sessão persistida**. Como sub-agente, ele roda na própria sessão do morador, e o Runner do ADK encaminha a resposta a ele. |
| `visitantes` | Lista e autoriza visitantes do morador. | **Transferência** (`sub_agents` do principal). | Mesmo motivo: `autorizar_visitante` sempre pede confirmação. |
| `regulamento` | Responde dúvidas lendo um capítulo do regulamento por vez. | **Ferramenta** (`AgentTool`) do principal. | O `AgentTool` roda o especialista numa sessão em memória separada. Os capítulos lidos ficam nessa sessão descartável e só a resposta final volta ao histórico do morador. É isso que mantém o regulamento fora da janela de contexto das mensagens seguintes. |

Decisões que valem destacar:

- **Por que não usar `AgentTool` para reservas e visitantes?** O `AgentTool`
  executa o sub-agente em um `InMemorySessionService` próprio. Um pedido de
  confirmação feito lá dentro não fica na sessão persistida e não pode ser
  retomado pela rota de confirmações, nem depois de um reinício.
- **Retomada da confirmação:** o `App` usa
  `ResumabilityConfig(is_resumable=True)` ([aurora/agent.py](aurora/agent.py)).
  Assim o Runner roteia a resposta de uma confirmação pelo id da chamada
  original, e não pela última mensagem. Foi testado com sessão persistida em
  SQLite, inclusive com a confirmação criada antes de reiniciar a API e
  aprovada depois do reinício.
- **Modelos:** `gemini-3.5-flash-lite` por padrão em todos os agentes, porque a
  cota gratuita do AI Studio para os modelos *flash* não-lite é muito baixa
  (20 requisições por dia na conta usada nos testes). O fluxo do avaliador faz
  algumas dezenas de chamadas. Dá para trocar o modelo pelas variáveis
  `AURORA_MODELO_PRINCIPAL` e `AURORA_MODELO_ESPECIALISTAS`. As chamadas usam
  retry automático para 429, 500, 503 e 504, com espera exponencial de até 60 s.
  Assim o limite por minuto do plano gratuito (15 requisições por minuto no
  `flash-lite`) atrasa a resposta, mas não derruba a requisição.
- **Armazenamento:** SQLite, sem serviço externo. `var/condominio.db` guarda
  apartamentos, áreas, reservas, visitantes e o dono de cada sessão.
  `var/sessoes.db` guarda sessões e eventos (`DatabaseSessionService` do ADK).

## Garantias

### Garantia 1: cobrança ou acesso só com confirmação

**Onde:**
- [aurora/tools.py](aurora/tools.py): `reservar_area` (bloco
  `if area_obj.gera_cobranca:`) e `autorizar_visitante` (bloco
  `confirmacao = tool_context.tool_confirmation`).
- [aurora/api.py](aurora/api.py): `confirmacoes_pendentes` e `responder_confirmacao`.

Em `aurora/tools.py`, função `reservar_area`:

```python
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
```

Em `aurora/api.py`, função `responder_confirmacao`:

```python
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
```

**Por que não depende do modelo:**
- A decisão de pedir confirmação está na tool, e não no prompt. Toda área com
  `taxa > 0` e toda autorização de visitante passam por `request_confirmation`.
  A quadra (taxa 0) grava direto.
- O único caminho que entrega `tool_confirmation` à tool é uma `FunctionResponse`
  `adk_request_confirmation`. Ela só é montada pela rota
  `POST /sessoes/{id}/confirmacoes`, e o texto do chat nunca vira uma.
  Escrever "já estou confirmando aqui" não muda nada.
- Ao retomar, o ADK reexecuta a chamada **original gravada na sessão**, com os
  mesmos argumentos. O modelo não consegue trocar área, data ou nome entre o
  pedido e a aprovação.
- As pendências são calculadas pelo histórico persistido: um pedido continua
  pendente enquanto não houver uma resposta com o mesmo id. Por isso:
  - um id inexistente recebe `409`;
  - um id de outra sessão recebe `409`;
  - um id já respondido recebe `409`, e a ação não executa de novo.

  Um lock por sessão serializa mensagens e confirmações da mesma sessão.

### Garantia 2: cada sessão pertence a um apartamento

**Onde:**
- [aurora/api.py](aurora/api.py): `criar_sessao` (`user_id=apartamento`).
- [aurora/tools.py](aurora/tools.py): `apartamento_da_sessao`.
- [aurora/condominio.py](aurora/condominio.py): `reservas_do_apartamento`,
  `cancelar_reserva`, `visitantes_do_apartamento` e `data_ocupada`.

Em `aurora/tools.py`:

```python
def apartamento_da_sessao(tool_context: ToolContext) -> str:
    apartamento = tool_context.session.user_id
    if not condominio.apartamento_existe(apartamento):
        raise SessaoSemApartamento(f"Sessão sem apartamento válido: {apartamento!r}")
    return apartamento
```

**Por que não depende do modelo:**
- O apartamento vira o `user_id` da sessão do ADK uma única vez, em `POST /sessoes`.
  Nenhum evento altera o `user_id` depois disso.
- **Nenhuma tool tem parâmetro de apartamento.** Todas leem
  `apartamento_da_sessao`, então o modelo não tem como escolher outro
  apartamento, mesmo que o morador diga ser do 302.
- As consultas filtram por apartamento no próprio SQL. No cancelamento
  (`WHERE apartamento = ? AND area = ? AND data = ?`), uma reserva de outro
  apartamento é indistinguível de uma reserva inexistente. A tool responde
  "você não tem reserva ativa dessa área nessa data" e nenhum código alheio
  chega à conversa.
- `consultar_disponibilidade`, e `reservar_area` quando a data está ocupada,
  devolvem só "livre/ocupada". Elas nunca dizem o código nem o apartamento da
  reserva existente.

### Garantia 3: nada se perde no reinício

**Onde:**
- [aurora/api.py](aurora/api.py): `lifespan` (`DatabaseSessionService(db_url="sqlite+aiosqlite:///var/sessoes.db")`).
- [aurora/condominio.py](aurora/condominio.py): `inicializar`.

**Por que não depende do modelo:**
- Sessões e eventos ficam no SQLite do `DatabaseSessionService`. Reservas,
  cancelamentos, visitantes e o dono de cada sessão ficam em `var/condominio.db`.
- Ao subir, `inicializar` só cria o schema, e só carrega `dados/` se o banco
  estiver vazio. Um reinício não restaura nada.
- A mesma sessão continua com todos os eventos, e as confirmações pendentes
  podem ser aprovadas depois do reinício.
- **Códigos nunca repetem.** Todo código gerado entra em `codigos_emitidos`
  (chave primária, [aurora/condominio.py](aurora/condominio.py): `_novo_codigo`).
  Reservas canceladas ficam com `status='cancelada'`, sem ser apagadas. Nem o
  comando de restauração limpa `codigos_emitidos`.

### Garantia 4: o regulamento é consultado, não carregado

**Onde:**
- [aurora/agent.py](aurora/agent.py): `regulamento_agent` e
  `tools=[AgentTool(agent=regulamento_agent)]` no `root_agent`.
- [aurora/regulamento.py](aurora/regulamento.py): `capitulo` e `indice`.
- [aurora/tools.py](aurora/tools.py): `consultar_capitulo`.

**Por que não depende do modelo:**
- A instrução do `concierge` não contém o regulamento.
- O especialista `regulamento` recebe só o **índice de títulos** e lê **um
  capítulo por vez** com `consultar_capitulo(numero)`. Não existe tool que
  devolva o texto inteiro.
- Ele roda como `AgentTool`, numa sessão em memória separada e descartada. Na
  sessão do morador entram só a pergunta (`functionCall` de `regulamento`) e a
  resposta final (`functionResponse`).
- Mesmo que o especialista leia um capítulo errado, esse texto não chega aos
  eventos da sessão nem às chamadas seguintes do agente principal.

### Garantia 5: dois moradores, uma reserva

**Onde:**
- [aurora/condominio.py](aurora/condominio.py): índice `ux_reserva_ativa` no
  `SCHEMA` e função `criar_reserva`.
- [aurora/tools.py](aurora/tools.py): `reservar_area` (`except condominio.DataOcupada`).

Em `aurora/condominio.py`, no `SCHEMA`:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS ux_reserva_ativa
    ON reservas(area, data) WHERE status = 'ativa';
```

Em `aurora/condominio.py`, função `criar_reserva`:

```python
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
```

**Por que não depende do modelo:**
- A exclusividade é validada **pelo SQLite no instante do INSERT**, por um
  índice único parcial que só considera reservas ativas. Não existe janela
  entre conferir e gravar.
- Se duas aprovações simultâneas tentarem gravar a mesma área e data:
  - a primeira grava;
  - a segunda recebe `IntegrityError`, que vira `DataOcupada`;
  - a tool responde `{"status": "indisponivel"}`, e a API devolve `200` com uma
    resposta normal.
- A conferência de agenda feita antes de pedir a confirmação é só uma gentileza
  ao morador. A garantia é o índice.
- Uma reserva cancelada sai do índice, e a data volta a ficar livre.

## Como rodar

### Pré-requisitos

- [uv](https://docs.astral.sh/uv/). O `uv sync` instala o Python 3.12+ se faltar.
- Chave do **Google AI Studio** (https://aistudio.google.com/apikey).
- Nenhum serviço externo: o armazenamento é SQLite, em `var/`.

### Variáveis do `.env`

```bash
cp .env.example .env
```

| Variável | Obrigatória | Descrição |
|---|---|---|
| `GOOGLE_API_KEY` | sim | Chave do Google AI Studio. |
| `GOOGLE_GENAI_USE_VERTEXAI` | sim | `FALSE` (usa o AI Studio, não o Vertex). |
| `AURORA_MODELO_PRINCIPAL` | não | Modelo do agente principal. Padrão: `gemini-3.5-flash-lite`. |
| `AURORA_MODELO_ESPECIALISTAS` | não | Modelo dos especialistas. Padrão: `gemini-3.5-flash-lite`. |

### Comandos

Instalar as dependências:

```bash
uv sync
```

Restaurar os dados iniciais. Isso volta reservas e visitantes ao estado de
`dados/` e apaga as sessões gravadas; rode com a API parada:

```bash
uv run python -m aurora.restaurar
```

Subir a API em http://localhost:8000:

```bash
uv run python -m aurora
```

Pare a API com Ctrl+C e suba de novo com o mesmo comando: conversas, reservas e
visitantes continuam lá.

### Exemplo rápido

```bash
curl -s -X POST localhost:8000/sessoes -H 'content-type: application/json' -d '{"apartamento":"101"}'
```

```bash
curl -s -X POST localhost:8000/sessoes/<session_id>/mensagens -H 'content-type: application/json' -d '{"texto":"Reserve o salão de festas para 2030-04-20."}'
```

```bash
curl -s -X POST localhost:8000/sessoes/<session_id>/confirmacoes -H 'content-type: application/json' -d '{"id":"<id da confirmação>","confirmado":true}'
```

```bash
curl -s localhost:8000/apartamentos/101/reservas
```
