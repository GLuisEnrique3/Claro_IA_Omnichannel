"""
ConversationStore — persistencia thread-safe de historial conversacional.

Almacena turnos por clave de canal (gchat::{space}::{user}, rest::{session_id}).
Cada turno son dos dicts consecutivos: {role: "user", ...} y {role: "assistant", ...}.
TTL: 24h (lazy cleanup en load). Sliding window: max 10 turnos por clave.
Escritura atómica via tmp file + os.replace().
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ruta del archivo de historial (core/ está un nivel adentro de la raíz)
_HISTORY_FILE = Path(__file__).parent.parent / "data" / "conversation_history.json"

# Lock a nivel de módulo para serializar operaciones read-modify-write
_HISTORY_LOCK = threading.Lock()

# Constantes
_MAX_TURNS = 10
_TTL_HOURS = 24
_MAX_CONTENT_CHARS = 200


class ConversationStore:
    """
    Encapsula la persistencia JSON thread-safe del historial conversacional.
    Métodos síncronos — los adapters los invocan con asyncio.to_thread().
    """

    def load(self, key: str) -> list[dict]:
        """
        Retorna los turnos vigentes para la clave dada.
        Aplica lazy TTL: descarta turnos con timestamp > 24h de antigüedad.
        Si la clave no existe o el archivo no existe, retorna [].
        """
        with _HISTORY_LOCK:
            data = self._leer_archivo()
            entries = data.get(key, [])

        if not entries:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=_TTL_HOURS)
        resultado = []
        # Iterar de a pares (user + assistant)
        i = 0
        while i + 1 < len(entries):
            turno_user = entries[i]
            turno_asst = entries[i + 1]
            ts_str = turno_user.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    # Retornar solo role+content, sin timestamp
                    resultado.append({"role": turno_user["role"], "content": turno_user["content"]})
                    resultado.append({"role": turno_asst["role"], "content": turno_asst["content"]})
            except (ValueError, KeyError):
                # Si el timestamp es inválido, descartar el turno
                pass
            i += 2

        return resultado

    def append(self, key: str, user_message: str, assistant_reply: str) -> None:
        """
        Agrega un nuevo turno (user + assistant) al historial de la clave dada.
        Trunca el contenido a 200 chars.
        Aplica sliding window: si supera 10 turnos, descarta el más antiguo.
        Escritura atómica via tmp file + os.replace().
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        turno_user = {
            "role": "user",
            "content": user_message[:_MAX_CONTENT_CHARS],
            "timestamp": now_iso,
        }
        turno_asst = {
            "role": "assistant",
            "content": assistant_reply[:_MAX_CONTENT_CHARS],
            "timestamp": now_iso,
        }

        with _HISTORY_LOCK:
            data = self._leer_archivo()
            entries = data.get(key, [])

            entries.append(turno_user)
            entries.append(turno_asst)

            # Sliding window: max 10 turnos = 20 dicts
            max_entries = _MAX_TURNS * 2
            while len(entries) > max_entries:
                # Descartar el par más antiguo (índices 0 y 1)
                entries = entries[2:]

            data[key] = entries
            self._guardar_archivo(data)

    # ── Helpers privados ───────────────────────────────────────────────────────

    def _leer_archivo(self) -> dict:
        """Lee el archivo JSON de historial. Retorna {} si no existe o está corrupto."""
        if not _HISTORY_FILE.exists():
            return {}
        try:
            with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _guardar_archivo(self, data: dict) -> None:
        """Escritura atómica: escribe en tmp y luego reemplaza con os.replace()."""
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = _HISTORY_FILE.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, _HISTORY_FILE)


# Singleton a nivel de módulo
conversation_store = ConversationStore()
