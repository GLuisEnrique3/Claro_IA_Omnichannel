"""
Cliente mínimo de la Chat REST API para publicar mensajes asíncronos.

Se usa cuando el procesamiento de un turno excede la ventana síncrona de 30 s
del webhook de Google Chat: el webhook responde un placeholder al instante y,
al terminar el trabajo pesado en background, este módulo publica el resultado
real en el space via spaces.messages.create.

Auth: service account con scope chat.bot. Ruta del JSON en CHAT_CREDENTIALS_JSON
(fallback a VERTEX_CREDENTIALS_JSON). OJO: el SA debe ser el que está
configurado como la app de Chat en la Chat API; si es distinto al de Vertex,
seteá CHAT_CREDENTIALS_JSON explícitamente.
"""
import logging
import os
import threading

import httpx
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

_log = logging.getLogger("chat_api")

_CHAT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
_CHAT_API_BASE = "https://chat.googleapis.com/v1"

_credentials = None
_CREDS_LOCK = threading.Lock()


def _get_token() -> str:
    """Credenciales SA con scope chat.bot, refrescadas si el token expiró."""
    global _credentials
    with _CREDS_LOCK:
        if _credentials is None:
            ruta = os.getenv("CHAT_CREDENTIALS_JSON") or os.getenv("VERTEX_CREDENTIALS_JSON")
            if not ruta:
                raise RuntimeError(
                    "Falta CHAT_CREDENTIALS_JSON (o VERTEX_CREDENTIALS_JSON) "
                    "para autenticar la Chat REST API."
                )
            _credentials = service_account.Credentials.from_service_account_file(
                ruta, scopes=[_CHAT_SCOPE]
            )
        if not _credentials.valid:
            _credentials.refresh(GoogleAuthRequest())
        return _credentials.token


def publicar_mensaje(space: str, thread_name: str | None, mensaje: dict) -> None:
    """
    Publica `mensaje` en `space` via spaces.messages.create.
    Si `thread_name` está, responde en ese mismo thread (cae a thread nuevo si
    el original ya no existe). `mensaje` es el mismo dict que arma _render_mensaje
    (text / cardsV2); el campo actionResponse, si viene, NO es válido acá y debe
    removerse antes de llamar.
    """
    if not space:
        _log.error("publicar_mensaje sin space; se descarta el mensaje.")
        return

    cuerpo = dict(mensaje)
    cuerpo.pop("actionResponse", None)  # solo válido en respuestas síncronas
    params = {}
    if thread_name:
        cuerpo["thread"] = {"name": thread_name}
        params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

    resp = httpx.post(
        f"{_CHAT_API_BASE}/{space}/messages",
        params=params,
        json=cuerpo,
        headers={"Authorization": f"Bearer {_get_token()}"},
        timeout=30,
    )
    if resp.status_code >= 400:
        _log.error(
            "Chat API %s al publicar en %s: %s", resp.status_code, space, resp.text
        )
        resp.raise_for_status()
