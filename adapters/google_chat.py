"""
Adapter Google Chat — webhook que expone el flujo guiado del CLI (main.py)
replicado por core/guided_flow.py.

Eventos soportados:
    ADDED_TO_SPACE → inicia sesión: header + menú de identificación
    MESSAGE        → texto del usuario → GuidedFlow.step()
    CARD_CLICKED   → click en botón → mismo step() con el valor del botón

Sesiones por usuario: clave gchat::{space}::{user} en data/guided_flow_sessions.json.
Historial conversacional: sliding window de 10 turnos por sesión en
data/conversation_history.json (core/conversation_store.py).

Seguridad: verificación del JWT que firma Google Chat
(chat@system.gserviceaccount.com) contra GOOGLE_CHAT_AUDIENCE.
Para pruebas locales con curl: GOOGLE_CHAT_SKIP_AUTH=1.
"""
import asyncio
import logging
import os
import threading
import traceback

from fastapi import APIRouter, HTTPException, Request

import google.auth.transport.requests
import google.oauth2.id_token

from core.guided_flow import FlowResult, step
from core.conversation_store import conversation_store
from core.session_store import session_store
from adapters.chat_render import render_chat, texto_a_html_card
from adapters.chat_api import publicar_mensaje

router = APIRouter()

_log = logging.getLogger("google_chat")

# Tareas en vuelo: se mantiene la referencia para que asyncio no las recolecte
# (GC) antes de terminar — patrón estándar para create_task fire-and-forget.
_TAREAS: set = set()

# Sesiones con un turno pesado en proceso: mientras esté acá, los mensajes
# nuevos de esa sesión se rechazan con un aviso de espera. El flujo es una
# máquina de estados secuencial; no se admite un segundo turno en paralelo.
_EN_PROCESO: set = set()

# Límite de caracteres para mensajes de texto simple de Google Chat
_MAX_TEXTO_SIMPLE = 4000

# Locks por sesión: serializa mensajes concurrentes del mismo usuario
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_de(session_key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        if session_key not in _SESSION_LOCKS:
            _SESSION_LOCKS[session_key] = threading.Lock()
        return _SESSION_LOCKS[session_key]


# ── Verificación JWT de Google ─────────────────────────────────────────────────

def _verificar_jwt_google(authorization: str) -> dict:
    """
    Verifica el JWT enviado por Google Chat.
    Google Chat firma con chat@system.gserviceaccount.com (certs propios),
    NO con los certs OAuth2 genéricos — por eso se pasa certs_url explícito.
    """
    if os.getenv("GOOGLE_CHAT_SKIP_AUTH", "") == "1":
        return {}

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="No autorizado")

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="No autorizado")

    audience = os.getenv("GOOGLE_CHAT_AUDIENCE", "")

    _CHAT_CERTS_URL = (
        "https://www.googleapis.com/service_accounts/v1/metadata/x509/"
        "chat@system.gserviceaccount.com"
    )

    try:
        transport = google.auth.transport.requests.Request()
        return google.oauth2.id_token.verify_token(
            token, transport, audience=audience, certs_url=_CHAT_CERTS_URL
        )
    except Exception:
        raise HTTPException(status_code=401, detail="No autorizado")


# ── Render: FlowResult → mensaje de Google Chat ────────────────────────────────

def _render_mensaje(result: FlowResult, echo: str | None = None) -> dict:
    """
    Convierte un FlowResult en JSON de mensaje de Google Chat.
    El contenido se formatea para Chat via adapters/chat_render.py (negritas,
    sin decoración de terminal, debug oculto salvo FLOW_DEBUG=1).
    Texto ≤ 4000 chars → mensaje de texto simple; más largo → card con
    textParagraph (límite de texto simple de Chat: 4096 chars).
    Los botones sugeridos se agregan como card con buttonList.
    """
    texto, cards_extra = render_chat(result)
    if echo:
        texto = f"_{echo}_\n\n{texto}" if texto else f"_{echo}_"

    mensaje: dict = {}
    cards: list[dict] = []

    if len(texto) <= _MAX_TEXTO_SIMPLE:
        if texto:
            mensaje["text"] = texto
    else:
        # Texto largo (p. ej. instrucciones) → card con párrafos.
        # Las cards usan HTML simple, no el formato *negrita* de mensajes.
        widgets = []
        for chunk in _trocear(texto, _MAX_TEXTO_SIMPLE):
            widgets.append({"textParagraph": {"text": texto_a_html_card(chunk)}})
        cards.append({
            "cardId": "contenido",
            "card": {"sections": [{"widgets": widgets}]},
        })

    cards.extend(cards_extra)

    if result.botones:
        botones_widgets = {
            "buttonList": {
                "buttons": [
                    {
                        "text": label,
                        "onClick": {
                            "action": {
                                "function": "responder",
                                "parameters": [{"key": "valor", "value": value}],
                            }
                        },
                    }
                    for label, value in result.botones
                ]
            }
        }
        cards.append({
            "cardId": "acciones",
            "card": {"sections": [{"widgets": [botones_widgets]}]},
        })

    if cards:
        mensaje["cardsV2"] = cards
    return mensaje


def _trocear(texto: str, maximo: int) -> list[str]:
    """Parte el texto en bloques de hasta `maximo` chars cortando por línea."""
    bloques, actual = [], ""
    for linea in texto.split("\n"):
        if len(actual) + len(linea) + 1 > maximo and actual:
            bloques.append(actual)
            actual = linea
        else:
            actual = f"{actual}\n{linea}" if actual else linea
    if actual:
        bloques.append(actual)
    return bloques


# ── Procesamiento de un turno ──────────────────────────────────────────────────

def _procesar_turno(session_key: str, texto: str) -> FlowResult:
    """Ejecuta GuidedFlow.step() serializado por sesión (corre en thread)."""
    with _lock_de(session_key):
        resultado = step(session_key, texto)
        # Historial conversacional del ticket: sliding window de 10 turnos
        # (user + assistant) por sesión. TTL 24h. Se guarda lo que el usuario
        # vio en Chat, no el render CLI.
        texto_chat, _ = render_chat(resultado)
        conversation_store.append(session_key, texto or "(inicio)", texto_chat)
        return resultado


def _mensaje_error() -> dict:
    """Mensaje genérico cuando el turno falla con una excepción capturable."""
    return {
        "text": (
            "⚠️ Ocurrió un error procesando tu solicitud. "
            "Por favor intenta de nuevo en unos momentos."
        )
    }


async def _turno_seguro(session_key: str, texto: str) -> FlowResult | None:
    """
    Ejecuta _procesar_turno en un thread, capturando y logueando cualquier
    excepción. Retorna None si hubo error (el caller responde con _mensaje_error).

    NOTA: esto captura excepciones de Python (auth de Vertex, errores de
    BigQuery, bugs). NO captura un OOM kill: ese es un SIGKILL del kernel que
    termina el proceso sin pasar por este except — se resuelve con más memoria.
    """
    try:
        return await asyncio.to_thread(_procesar_turno, session_key, texto)
    except Exception:
        _log.error(
            "Error procesando turno (session=%s):\n%s",
            session_key,
            traceback.format_exc(),
        )
        return None


# ── Procesamiento asíncrono (turnos que exceden los 30 s de Chat) ───────────────

def _placeholder() -> dict:
    """Respuesta síncrona inmediata mientras el trabajo pesado corre en background."""
    return {"text": "⏳ Procesando tu consulta, dame unos segundos..."}


def _mensaje_espera() -> dict:
    """Respuesta cuando llega un mensaje y ya hay un turno pesado en proceso."""
    return {"text": "⏳ Aún estoy procesando tu consulta anterior, dame un momento..."}


def _es_turno_pesado(session_key: str, valor: str) -> bool:
    """
    True si el turno PUEDE ejecutar la consulta pendiente (Gemini Pro + BigQuery
    / RAG), lo que excede la ventana síncrona de 30 s de Google Chat.

    Con el motor de dos agentes la confirmación es en lenguaje libre: no se sabe
    si el usuario confirma o reformula hasta llamar al Agente 2. Por eso, estando
    en CONFIRM con un caso ejecutable pendiente, se trata el turno como pesado
    (lado seguro contra el timeout). Espía el estado sin mutarlo. Sirve igual
    para CARD_CLICKED (valor del botón) que para MESSAGE (el usuario escribe).
    """
    session = session_store.load(session_key)
    if not session or session.get("state") != "CONFIRM":
        return False
    pendiente = session.get("pendiente") or {}
    return pendiente.get("use_case_entry") is not None


def _agendar_async(space: str, session_key: str,
                   texto: str, echo: str | None = None) -> None:
    """Marca la sesión como en proceso y lanza el trabajo en background."""
    _EN_PROCESO.add(session_key)
    tarea = asyncio.create_task(
        _procesar_y_publicar(space, session_key, texto, echo)
    )
    _TAREAS.add(tarea)
    tarea.add_done_callback(_TAREAS.discard)


async def _procesar_y_publicar(space: str, session_key: str,
                               texto: str, echo: str | None) -> None:
    """Procesa el turno pesado y publica el resultado en el flujo principal."""
    try:
        resultado = await _turno_seguro(session_key, texto)
        if resultado is None:
            mensaje = _mensaje_error()
        else:
            mensaje = _render_mensaje(resultado, echo=echo)
        # thread=None → publica en el stream principal, no en un hilo anidado.
        await asyncio.to_thread(publicar_mensaje, space, None, mensaje)
    except Exception:
        _log.error(
            "No se pudo publicar la respuesta async en %s:\n%s",
            space,
            traceback.format_exc(),
        )
    finally:
        # Libera la sesión pase lo que pase, para no dejarla bloqueada.
        _EN_PROCESO.discard(session_key)


# ── Webhook principal ──────────────────────────────────────────────────────────

@router.post("/webhook", summary="Webhook de Google Chat")
async def webhook(request: Request):
    """Recibe eventos de Google Chat, verifica el JWT y enruta al flujo guiado."""
    auth_header = request.headers.get("Authorization", "")
    _verificar_jwt_google(auth_header)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload invalido")

    event_type = body.get("type", "")
    user_id = body.get("user", {}).get("name", "")
    space_name = body.get("space", {}).get("name", "")
    session_key = f"gchat::{space_name or 'unknown'}::{user_id or 'unknown'}"

    if event_type == "ADDED_TO_SPACE":
        # Arranque igual que el CLI: header + identificación
        resultado = await _turno_seguro(session_key, "")
        if resultado is None:
            return _mensaje_error()
        return _render_mensaje(resultado)

    if event_type == "CARD_CLICKED":
        # Guard: si ya hay un turno pesado en proceso, obligar a esperar.
        if session_key in _EN_PROCESO:
            respuesta = _mensaje_espera()
            respuesta["actionResponse"] = {"type": "NEW_MESSAGE"}
            return respuesta
        action = body.get("action", {}) or {}
        parametros = action.get("parameters") or body.get("common", {}).get("parameters") or []
        if isinstance(parametros, dict):
            valor = parametros.get("valor", "")
        else:
            valor = next((p.get("value", "") for p in parametros if p.get("key") == "valor"), "")
        echo = f"Selección: {valor}"
        # Turno pesado (confirmar ejecución): procesar en background y publicar
        # async via Chat REST API, para no chocar con el límite de 30 s del webhook.
        if _es_turno_pesado(session_key, valor):
            _agendar_async(space_name, session_key, valor, echo=echo)
            respuesta = _placeholder()
            respuesta["actionResponse"] = {"type": "NEW_MESSAGE"}
            return respuesta
        resultado = await _turno_seguro(session_key, valor)
        if resultado is None:
            respuesta = _mensaje_error()
        else:
            respuesta = _render_mensaje(resultado, echo=echo)
        # NEW_MESSAGE: la respuesta se publica como mensaje nuevo y la
        # conversación queda visible como transcript (igual que el CLI).
        respuesta["actionResponse"] = {"type": "NEW_MESSAGE"}
        return respuesta

    if event_type != "MESSAGE":
        return {}

    # argumentText excluye la mención al bot en espacios grupales
    mensaje = body.get("message", {})
    texto = (mensaje.get("argumentText") or mensaje.get("text") or "").strip()
    if not texto:
        return {}

    # Guard: si ya hay un turno pesado en proceso, obligar a esperar.
    if session_key in _EN_PROCESO:
        return _mensaje_espera()

    # El usuario puede confirmar escribiendo "S" en vez de tocar el botón: mismo
    # turno pesado → async.
    if _es_turno_pesado(session_key, texto):
        _agendar_async(space_name, session_key, texto)
        return _placeholder()

    resultado = await _turno_seguro(session_key, texto)
    if resultado is None:
        return _mensaje_error()
    return _render_mensaje(resultado)
