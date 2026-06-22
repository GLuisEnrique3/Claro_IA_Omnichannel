import uuid
import json
import time
import threading
import datetime
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

_BQ_TABLE = "claroinsurance-dataplatform.claro_IA.model_tracking_poc"


class QueryLog:
    """Acumula métricas de una sola consulta del usuario."""

    def __init__(self, session_id: str, tipo_usuario: str, valor_identificado: str | None):
        self.session_id = session_id
        self.tipo_usuario = tipo_usuario
        self.valor_identificado = valor_identificado
        self.timestamp = datetime.datetime.utcnow().isoformat() + "Z"
        self.query_original: str = ""
        self.query_reescrita: str = ""
        self.query_normalizado: str = ""
        self.caso_catalogo: str | None = None
        self.caso_nombre: str | None = None
        self.caso_exitoso: bool = False
        self.confirmacion_mensaje: str | None = None
        self.confirmado: bool | None = None
        self.intentos_confirmacion: int = 0
        self.entidades: list[dict] = []
        self.tipo_ejecucion: str | None = None   # "sql", "rag", "multiple"
        self.sql_intentos: int = 0
        self.sql_exitoso: bool | None = None
        self.sql_error: str | None = None
        self.bq_filas: int | None = None
        self.bq_exitoso: bool | None = None
        self.bq_error: str | None = None
        self.sql_generada: str | None = None
        self.llm_respuesta: str | None = None
        self.rag_coleccion: str | None = None
        self.rag_documentos: int | None = None
        self.rag_carrier: str | None = None
        self.latencia_normalizacion_ms: int | None = None
        self.latencia_deteccion_ms: int | None = None
        self.latencia_entidades_ms: int | None = None
        self.latencia_sql_ms: int | None = None
        self.latencia_bq_ms: int | None = None
        self.latencia_rag_ms: int | None = None
        self.latencia_respuesta_ms: int | None = None
        self.latencia_total_ms: int | None = None
        self._t_inicio = time.perf_counter()

    def reiniciar_timer(self) -> None:
        """Reinicia el timer justo antes de la ejecución, para excluir el tiempo de espera del usuario."""
        self._t_inicio = time.perf_counter()

    def finalizar(self) -> None:
        self.latencia_total_ms = int((time.perf_counter() - self._t_inicio) * 1000)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "tipo_usuario": self.tipo_usuario,
            "valor_identificado": self.valor_identificado,
            "query_original": self.query_original,
            "query_reescrita": self.query_reescrita,
            "query_normalizado": self.query_normalizado,
            "caso_catalogo": self.caso_catalogo,
            "caso_nombre": self.caso_nombre,
            "caso_exitoso": self.caso_exitoso,
            "confirmacion_mensaje": self.confirmacion_mensaje,
            "confirmado": self.confirmado,
            "intentos_confirmacion": self.intentos_confirmacion,
            "entidades_json": json.dumps(self.entidades, ensure_ascii=False),
            "tipo_ejecucion": self.tipo_ejecucion,
            "sql_intentos": self.sql_intentos,
            "sql_exitoso": self.sql_exitoso,
            "sql_error": self.sql_error,
            "bq_filas": self.bq_filas,
            "bq_exitoso": self.bq_exitoso,
            "bq_error": self.bq_error,
            "sql_generada": self.sql_generada,
            "llm_respuesta": self.llm_respuesta,
            "rag_coleccion": self.rag_coleccion,
            "rag_documentos": self.rag_documentos,
            "rag_carrier": self.rag_carrier,
            "latencia_normalizacion_ms": self.latencia_normalizacion_ms,
            "latencia_deteccion_ms": self.latencia_deteccion_ms,
            "latencia_entidades_ms": self.latencia_entidades_ms,
            "latencia_sql_ms": self.latencia_sql_ms,
            "latencia_bq_ms": self.latencia_bq_ms,
            "latencia_rag_ms": self.latencia_rag_ms,
            "latencia_respuesta_ms": self.latencia_respuesta_ms,
            "latencia_total_ms": self.latencia_total_ms,
        }


class SessionLogger:

    def __init__(self):
        self.session_id = str(uuid.uuid4())
        self.tipo_usuario: str = ""
        self.valor_identificado: str | None = None
        self.current: QueryLog | None = None
        self._log_file = LOG_DIR / f"{datetime.date.today().isoformat()}.jsonl"

    def registrar_identificacion(
        self, tipo_usuario: str, valor_identificado: str | None, score: float | None
    ) -> None:
        self.tipo_usuario = tipo_usuario
        self.valor_identificado = valor_identificado
        self._append_jsonl({
            "session_id": self.session_id,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "evento": "identificacion",
            "tipo_usuario": tipo_usuario,
            "valor_identificado": valor_identificado,
            "score_identificacion": score,
        })

    def nueva_consulta(self) -> QueryLog:
        self.current = QueryLog(self.session_id, self.tipo_usuario, self.valor_identificado)
        return self.current

    def cerrar_consulta(self) -> None:
        if not self.current:
            return
        self.current.finalizar()
        self._append_jsonl(self.current.to_dict())
        q = self.current
        self.current = None
        threading.Thread(target=_flush_bq, args=(q,), daemon=True).start()

    def _append_jsonl(self, data: dict) -> None:
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass


def _flush_bq(q: QueryLog) -> None:
    try:
        from google.cloud import bigquery as bq
        from config import client
        job_config = bq.LoadJobConfig(
            write_disposition="WRITE_APPEND",
            source_format=bq.SourceFormat.NEWLINE_DELIMITED_JSON,
        )
        job = client.load_table_from_json([q.to_dict()], _BQ_TABLE, job_config=job_config)
        job.result()
    except Exception as exc:
        print(f"   ⚠  No se pudo registrar la consulta en BigQuery ({_BQ_TABLE}): {exc}")


_session: SessionLogger | None = None


def get_session() -> SessionLogger:
    global _session
    if _session is None:
        _session = SessionLogger()
    return _session


def nueva_sesion() -> SessionLogger:
    global _session
    _session = SessionLogger()
    return _session
