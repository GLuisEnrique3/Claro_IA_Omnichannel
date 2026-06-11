"""
SessionStore — persistencia thread-safe del estado del flujo guiado por usuario.

Cada sesión se identifica por clave de canal (gchat::{space}::{user}) y guarda
el estado de la máquina de estados del flujo guiado (core/guided_flow.py):
identificación, catálogos permitidos, consulta pendiente de confirmación, etc.

Persistencia: JSON en data/guided_flow_sessions.json, escritura atómica via
tmp file + os.replace(). TTL: 24h de inactividad (lazy cleanup en load).
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SESSIONS_FILE = Path(__file__).parent.parent / "data" / "guided_flow_sessions.json"

_SESSIONS_LOCK = threading.Lock()

_TTL_HOURS = 24


class SessionStore:
    """Persistencia JSON thread-safe de sesiones del flujo guiado."""

    def load(self, key: str) -> dict | None:
        """
        Retorna la sesión para la clave dada, o None si no existe o expiró.
        Aplica lazy TTL: sesiones con más de 24h de inactividad se descartan.
        """
        with _SESSIONS_LOCK:
            data = self._leer_archivo()
        sesion = data.get(key)
        if not sesion:
            return None

        ts_str = sesion.get("actualizado_en", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

        cutoff = datetime.now(timezone.utc) - timedelta(hours=_TTL_HOURS)
        if ts < cutoff:
            return None
        return sesion

    def save(self, key: str, sesion: dict) -> None:
        """Guarda la sesión con timestamp de actividad. Escritura atómica."""
        sesion["actualizado_en"] = datetime.now(timezone.utc).isoformat()
        with _SESSIONS_LOCK:
            data = self._leer_archivo()
            data[key] = sesion
            self._guardar_archivo(data)

    def delete(self, key: str) -> None:
        """Elimina la sesión (p. ej. al escribir 'salir')."""
        with _SESSIONS_LOCK:
            data = self._leer_archivo()
            if key in data:
                del data[key]
                self._guardar_archivo(data)

    # ── Helpers privados ───────────────────────────────────────────────────────

    def _leer_archivo(self) -> dict:
        if not _SESSIONS_FILE.exists():
            return {}
        try:
            with open(_SESSIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _guardar_archivo(self, data: dict) -> None:
        _SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = _SESSIONS_FILE.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, _SESSIONS_FILE)


# Singleton a nivel de módulo
session_store = SessionStore()
