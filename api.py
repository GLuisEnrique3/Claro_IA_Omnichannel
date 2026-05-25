"""
api.py — FastAPI con el mismo flujo conversacional que main.py, adaptado para Google Chat.

Flujo replicado:
  1. Identificación de usuario (Agencia / NPN / Management)
  2. Consulta libre en lenguaje natural
  3. Confirmación de entidades detectadas (S / N / omitir)
  4. Ejecución y resultado
  5. ¿Otra consulta?

Correr:
  uvicorn api:app --host 0.0.0.0 --port 8080 --reload
"""

import os
import re
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from pydantic import BaseModel

import main as _m
from config import llm_model

# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Claro Insurance AI", version="2.0.0")
_POOL = ThreadPoolExecutor(max_workers=8)

# ── Constantes de comandos (igual que main.py) ─────────────────────────────────

_CMDS_SALIR        = {"salir", "exit", "q", "quit"}
_CMDS_VOLVER       = {"volver", "back"}
_CMDS_NUEVA_SESION = {"nueva sesión", "nueva sesion"}
_CMDS_INSTRUCCIONES= {"instrucciones", "help", "ayuda", "guia", "guía"}
_CMDS_ESCALAR      = {"escalar", "escalar a un humano"}

# ── Estado de sesión por usuario ───────────────────────────────────────────────

@dataclass
class UserState:
    # Paso actual del flujo
    step: str = "MENU_TIPO"          # MENU_TIPO | INGRESO_ID | CONSULTA | CONFIRMACION | CONTINUAR

    # Identificación
    tipo_key: Optional[str] = None
    tipo_nombre: Optional[str] = None
    filtro_fijo_key: Optional[str] = None
    filtro_fijo_sql: Optional[str] = None
    valor_identificado: Optional[str] = None
    catalogos: list = field(default_factory=list)

    # Query en proceso (durante CONFIRMACION)
    pending_query: Optional[str] = None
    pending_caso: Optional[dict] = None
    pending_pregunta: Optional[dict] = None
    pending_entidades: Optional[dict] = None
    pending_confirmar_s: bool = False   # True si el tipo permite S/Enter distintos
    sender_id: str = ""                 # ID del remitente, guardado en state

# sessions[sender_id] → UserState
_sessions: dict[str, UserState] = {}

def _get_state(sender_id: str) -> UserState:
    if sender_id not in _sessions:
        _sessions[sender_id] = UserState(sender_id=sender_id)
    return _sessions[sender_id]

def _reset(sender_id: str) -> UserState:
    _sessions[sender_id] = UserState(sender_id=sender_id)
    return _sessions[sender_id]

# ── Textos fijos ───────────────────────────────────────────────────────────────

def _texto_menu_tipo() -> str:
    return (
        "🔐 *IDENTIFICACIÓN DE USUARIO*\n\n"
        "Selecciona tu tipo de usuario:\n"
        "  *1* — Representante de Agencia\n"
        "  *2* — Agente NPN\n"
        "  *3* — Management"
    )

def _texto_instrucciones() -> str:
    lines = []
    for cat_key, cat_data in _m.USE_CASES["options"].items():
        label = _m.CATALOG_LABELS.get(cat_key, cat_key)
        lines.append(f"\n*{label}*")
        for p in cat_data.get("preguntas", []):
            lines.append(f"  · {p['texto']}")
    temas = "\n".join(lines)
    return (
        "ℹ️ *INSTRUCCIONES DE USO*\n\n"
        "Escribe tu consulta en lenguaje natural. El sistema detecta automáticamente "
        "el tema y los filtros mencionados en tu consulta.\n\n"
        "*TEMAS DISPONIBLES:*"
        f"{temas}\n\n"
        "*FILTROS RECONOCIDOS:* Carrier, Estado, Línea de Negocio, Agencia, NPN, "
        "Ejecutivo, Etapa, Sub-etapa, Fecha de Pago, Número de Póliza.\n\n"
        "*COMANDOS:*\n"
        "  `instrucciones` — muestra esta pantalla\n"
        "  `volver` — vuelve al paso anterior\n"
        "  `salir` — cierra la sesión"
    )

# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}

# ── REST genérico ──────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str
    message: str

@app.post("/chat")
async def chat(req: ChatRequest):
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(_POOL, _handle_message, req.session_id, req.message)
    return {"response": response}

# ── Google Chat Webhook ────────────────────────────────────────────────────────

@app.post("/google-chat-webhook")
@app.post("/webhook/google-chat")
async def google_chat_webhook(request: Request):
    body = await request.json()
    event_type = body.get("type", "")

    if event_type == "ADDED_TO_SPACE":
        state = _get_state("__added__")  # no importa el ID aquí
        return {"text": _texto_menu_tipo()}

    if event_type != "MESSAGE":
        return {}

    # Extraer sender_id y texto del mensaje
    sender_id = body.get("sender", {}).get("name", "unknown")
    message   = body.get("message", {})
    text      = message.get("argumentText") or message.get("text", "")
    text      = re.sub(r"<users/\d+>|@\S+", "", text).strip()

    if not text:
        return {"text": "Por favor escribe tu consulta."}

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(_POOL, _handle_message, sender_id, text)
    return {"text": response}

# ── Máquina de estados ─────────────────────────────────────────────────────────

def _handle_message(sender_id: str, text: str) -> str:
    """Punto de entrada de cada mensaje. Delega al handler del paso actual."""
    state = _get_state(sender_id)
    cmd   = text.strip().lower()

    # Comandos globales — funcionan en cualquier paso
    if cmd in _CMDS_SALIR:
        _reset(sender_id)
        return "👋 Sesión finalizada. ¡Hasta pronto!\n\n" + _texto_menu_tipo()

    if cmd in _CMDS_NUEVA_SESION:
        _reset(sender_id)
        return "🔄 Nueva sesión iniciada.\n\n" + _texto_menu_tipo()

    if cmd in _CMDS_INSTRUCCIONES:
        return _texto_instrucciones()

    if cmd in _CMDS_ESCALAR:
        return (
            "📞 Escalando a un agente humano...\n"
            "Por favor espera, alguien del equipo se pondrá en contacto contigo.\n\n"
            "_(Esta funcionalidad está en proceso de implementación.)_"
        )

    if cmd in _CMDS_VOLVER:
        return _cmd_volver(state)

    # Despachar al handler del paso actual
    if state.step == "MENU_TIPO":
        return _step_menu_tipo(state, text)

    if state.step == "INGRESO_ID":
        return _step_ingreso_id(state, text)

    if state.step == "CONSULTA":
        return _step_consulta(state, text)

    if state.step == "CONFIRMACION":
        return _step_confirmacion(state, text)

    if state.step == "CONTINUAR":
        return _step_continuar(state, text)

    # Fallback
    _reset(sender_id)
    return _texto_menu_tipo()


def _cmd_volver(state: UserState) -> str:
    if state.step in ("CONSULTA", "CONFIRMACION", "CONTINUAR"):
        state.step        = "CONSULTA"
        state.pending_query    = None
        state.pending_caso     = None
        state.pending_pregunta = None
        state.pending_entidades= None
        return "↩️ Volviendo a consulta.\n\n¿Qué deseas consultar?"
    # Si está en pasos previos, reiniciar
    _reset(state.sender_id)
    return "↩️ Volviendo al inicio.\n\n" + _texto_menu_tipo()


# ── PASO 1: Selección de tipo de usuario ──────────────────────────────────────

def _step_menu_tipo(state: UserState, text: str) -> str:
    opcion = text.strip()
    if opcion not in _m.TIPOS_USUARIO:
        return f"⚠️ Opción no válida. Selecciona 1, 2 o 3.\n\n{_texto_menu_tipo()}"

    tipo = _m.TIPOS_USUARIO[opcion]
    state.tipo_key    = opcion
    state.tipo_nombre = tipo["nombre"]
    state.catalogos   = tipo["catalogos"]

    if tipo["filtro_key"] is None:
        # Management → sin identificación, directo a consulta
        state.filtro_fijo_key = None
        state.filtro_fijo_sql = None
        state.valor_identificado = None
        state.step = "CONSULTA"
        return (
            f"✅ Bienvenido, *{tipo['nombre']}*\n\n"
            "¿Qué deseas consultar hoy? Puedes escribir tu consulta en lenguaje natural."
        )

    # Agencia o NPN → pedir identificador
    state.filtro_fijo_key = tipo["filtro_key"]
    state.step = "INGRESO_ID"
    prompt = tipo["prompt_id"].replace(": ", "")
    return f"✅ Seleccionado: *{tipo['nombre']}*\n\n{prompt}:"


# ── PASO 2: Ingreso de ID (Agencia / NPN) ─────────────────────────────────────

def _step_ingreso_id(state: UserState, text: str) -> str:
    tipo    = _m.TIPOS_USUARIO[state.tipo_key]
    filtro_key = tipo["filtro_key"]
    candidatos = _m.FILTROS_VALIDOS.get(filtro_key, [])
    embs_pre   = _m.FILTROS_EMBEDDINGS.get(filtro_key, {}).get("embeddings")

    if not candidatos:
        return "⚠️ Sin registros en el sistema para este tipo de usuario. Contacta al administrador."

    match, score = _m._buscar_semantico(text, candidatos, tipo["umbral"], embs_pre)

    if not match:
        return (
            f"⚠️ No encontré coincidencia para *{text}* "
            f"(similitud: {score:.2f}, umbral: {tipo['umbral']:.2f}).\n"
            "Verifica el dato e intenta nuevamente."
        )

    state.valor_identificado = match
    valor_seguro = match.replace("'", "''")
    state.filtro_fijo_sql = tipo["sql_filtro"].replace("{valor}", valor_seguro)
    state.step = "CONSULTA"

    return (
        f"✅ Bienvenido. El sistema te identificó como *{match}* "
        f"(confianza: {score:.2f})\n\n"
        "¿Qué deseas consultar hoy? Puedes escribir tu consulta en lenguaje natural."
    )


# ── PASO 3: Consulta libre ─────────────────────────────────────────────────────

def _step_consulta(state: UserState, text: str) -> str:
    # Normalizar con LLM
    normalized = _m.transformar_consulta_con_llm(text)

    # Detectar caso de uso
    caso_entry, score = _m.detectar_caso_de_uso(normalized, state.catalogos)

    if caso_entry is None:
        return (
            f"🔍 No pude identificar un flujo para: _{text}_\n"
            f"(Intención detectada: _{normalized}_ — similitud: {score:.2f})\n\n"
            "Por favor reformula tu consulta con más detalle."
        )

    pregunta      = caso_entry["pregunta"]
    nombre_caso   = caso_entry["nombre"]
    tipo_pregunta = pregunta.get("tipo", "sql")
    catalogo_key  = caso_entry["catalogo"]

    # Extraer entidades del query original
    entidades_previas: dict = {}
    if tipo_pregunta == "sql":
        entidades_previas = _m.extraer_entidades(
            text, pregunta.get("parametros", []), state.filtro_fijo_key
        )
    elif tipo_pregunta == "multiple":
        # Extraer entidades desde la primera sub-consulta SQL que tenga parámetros
        catalogo = _m.USE_CASES["options"].get(catalogo_key, {})
        preguntas_map = {p["id"]: p for p in catalogo.get("preguntas", [])}
        sub_pqs = [preguntas_map[i] for i in pregunta.get("invoca", []) if i in preguntas_map]
        params_ref = next(
            (sp["parametros"] for sp in sub_pqs if sp.get("tipo") == "sql" and sp.get("parametros")),
            []
        )
        if params_ref:
            entidades_previas = _m.extraer_entidades(text, params_ref, state.filtro_fijo_key)

    # Guardar estado pendiente
    state.pending_query     = text
    state.pending_caso      = caso_entry
    state.pending_pregunta  = pregunta
    state.pending_entidades = entidades_previas

    # Construir mensaje de confirmación
    msg = f'🎯 Flujo identificado: *{nombre_caso}* (confianza: {score:.2f})\n'

    if tipo_pregunta == "multiple":
        catalogo = _m.USE_CASES["options"].get(catalogo_key, {})
        preguntas_map = {p["id"]: p for p in catalogo.get("preguntas", [])}
        invoca_nombres = [
            preguntas_map[i]["texto"]
            for i in pregunta.get("invoca", [])
            if i in preguntas_map
        ]
        msg += f"\nSe ejecutarán {len(invoca_nombres)} consultas en paralelo:\n"
        for n in invoca_nombres:
            msg += f"  • {n}\n"

    if entidades_previas:
        msg += "\nEntidades detectadas:\n"
        for _, (valor, sc_e, label, _) in entidades_previas.items():
            msg += f"  • *{label}:* {valor}  (confianza: {sc_e:.2f})\n"
    else:
        msg += "\nSin filtros adicionales detectados.\n"

    msg += (
        "\n¿Confirmar?\n"
        "  *S* — ejecutar con estos filtros\n"
        "  *N* — reformular la consulta\n"
        "  *omitir* — ejecutar sin filtros adicionales"
    )

    state.step = "CONFIRMACION"
    return msg


# ── PASO 4: Confirmación de entidades ─────────────────────────────────────────

def _step_confirmacion(state: UserState, text: str) -> str:
    resp = text.strip().lower()

    if resp in {"n", "no", "reformular"}:
        state.step = "CONSULTA"
        state.pending_query     = None
        state.pending_caso      = None
        state.pending_pregunta  = None
        state.pending_entidades = None
        return "↩️ Por favor reformula tu consulta:"

    # S → con entidades | omitir / cualquier otra cosa → sin entidades
    confirmo_s = resp in {"s", "si", "sí", "confirmar", "confirmo"}

    pregunta      = state.pending_pregunta
    tipo_pregunta = pregunta.get("tipo", "sql")
    user_query    = state.pending_query
    entidades     = state.pending_entidades if confirmo_s else {}
    caso_entry    = state.pending_caso
    catalogo_key  = caso_entry["catalogo"]

    respuesta = _ejecutar(tipo_pregunta, pregunta, catalogo_key, state, entidades, user_query)

    state.step = "CONTINUAR"
    return f"{respuesta}\n\n─────────────────────\n¿Deseas realizar otra consulta?\n  *S* — sí   *N* — cerrar sesión"


# ── PASO 5: ¿Otra consulta? ────────────────────────────────────────────────────

def _step_continuar(state: UserState, text: str) -> str:
    resp = text.strip().lower()
    if resp in {"s", "si", "sí", "otra", "continuar"}:
        state.step              = "CONSULTA"
        state.pending_query     = None
        state.pending_caso      = None
        state.pending_pregunta  = None
        state.pending_entidades = None
        return "¿Qué deseas consultar ahora?"
    # N u otro
    _reset(state.sender_id)
    return "👋 Gracias por usar el Sistema IA de Claro Insurance. ¡Hasta pronto!\n\n" + _texto_menu_tipo()


# ── Ejecución del caso detectado ──────────────────────────────────────────────

def _ejecutar(
    tipo: str,
    pregunta: dict,
    catalogo_key: str,
    state: UserState,
    entidades: dict,
    user_query: str,
) -> str:
    if tipo == "rag":
        return _ejecutar_rag(pregunta, user_query)
    if tipo == "multiple":
        return _ejecutar_multiple(pregunta, catalogo_key, state, entidades, user_query)
    # sql
    texto_usuario = user_query if entidades else ""
    return _m.ejecutar_consulta(
        pregunta,
        state.filtro_fijo_sql,
        entidades,
        texto_usuario,
    )


def _ejecutar_rag(pregunta: dict, user_query: str) -> str:
    result = _m._ejecutar_rag_silenciosa(pregunta, user_query)
    if result.get("error"):
        return f"❌ No pude acceder a los documentos: {result['error']}"
    docs = result.get("documentos", [])
    if not docs:
        return "No encontré documentos relacionados con tu consulta."

    contexto = "\n\n---\n\n".join(docs)
    entity_resolution = pregunta.get("entity_resolution", "")
    entity_resolution_bloque = (
        f"\n\nFORMATO ESPECÍFICO DE RESPUESTA PARA ESTE CASO DE USO:\n{entity_resolution}"
        if entity_resolution else ""
    )
    prompt = (
        f"Eres un asistente especializado de Claro Insurance.\n"
        f"El usuario preguntó: \"{user_query}\"\n\n"
        f"Basándote ÚNICAMENTE en los siguientes fragmentos de documentos:\n\n{contexto}\n\n"
        f"Responde directamente la pregunta en español de forma clara y concisa. "
        f"Si la información no está en los documentos, indícalo. "
        f"NO uses cierres formales como 'Atentamente' ni firmas. "
        f"Al final agrega estas dos líneas exactas, en este orden: "
        f"'Le invitamos a realizar otra pregunta para seguir explorando.' "
        f"'Recuerde que si su consulta no fue efectiva, puede escribir \"escalar a un humano\"'."
        f"{entity_resolution_bloque}"
    )
    try:
        return llm_model.generate_content(prompt).text.strip()
    except Exception as exc:
        return f"Error al generar respuesta: {exc}"


def _ejecutar_multiple(
    pregunta_multiple: dict,
    catalogo_key: str,
    state: UserState,
    entidades: dict,
    user_query: str,
) -> str:
    from concurrent.futures import as_completed, ThreadPoolExecutor

    invoca_ids = pregunta_multiple.get("invoca", [])
    if not invoca_ids:
        return "Este flujo no tiene sub-consultas configuradas."

    catalogo = _m.USE_CASES["options"].get(catalogo_key, {})
    preguntas_map = {p["id"]: p for p in catalogo.get("preguntas", [])}
    sub_preguntas = [preguntas_map[i] for i in invoca_ids if i in preguntas_map]

    if not sub_preguntas:
        return "No se encontraron las sub-consultas referenciadas."

    resultados: dict = {}
    with ThreadPoolExecutor(max_workers=len(sub_preguntas)) as pool:
        futures: dict = {}
        for sp in sub_preguntas:
            tipo_sp = sp.get("tipo", "sql")
            if tipo_sp == "sql":
                fut = pool.submit(
                    _m._ejecutar_consulta_silenciosa,
                    sp, state.filtro_fijo_sql, entidades, user_query,
                )
            elif tipo_sp == "rag":
                fut = pool.submit(_m._ejecutar_rag_silenciosa, sp, user_query)
            else:
                continue
            futures[fut] = sp

        for fut in as_completed(futures):
            sp = futures[fut]
            try:
                res = fut.result()
                res["descripciones"] = sp.get("descripciones", {})
                resultados[sp["texto"]] = res
            except Exception as exc:
                resultados[sp["texto"]] = {
                    "tipo": sp.get("tipo", "unknown"),
                    "nombre": sp["texto"],
                    "error": str(exc),
                    "descripciones": sp.get("descripciones", {}),
                }

    return _m._sintetizar_respuestas_multiples(
        user_query,
        resultados,
        entity_resolution=pregunta_multiple.get("entity_resolution", ""),
    )
