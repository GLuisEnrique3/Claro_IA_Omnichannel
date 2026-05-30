import re
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed

from main import (
    USE_CASES,
    _ejecutar_rag_silenciosa,
    _ejecutar_consulta_silenciosa,
    _sintetizar_respuestas_multiples,
    _construir_sql_con_llm,
    _formatear_filas,
    _llm_call,
    _MAX_REINTENTOS_SQL,
    extraer_entidades,
)
from config import llm_model, client

_SQL_FORBIDDEN = re.compile(
    r'^\s*(DELETE|INSERT|UPDATE|DROP|TRUNCATE|CREATE|ALTER|MERGE|CALL)\b',
    re.IGNORECASE | re.MULTILINE,
)
_MAX_FILAS_PROMPT = 80


def sql_response(
    pregunta: dict,
    sql_filtro: str | None,
    entidades: dict,
    texto_usuario: str,
    query_log=None,
    debug_out: dict | None = None,
) -> tuple[str, str | None]:
    """Ejecuta el flujo SQL completo y retorna (respuesta_llm, sql_generado)."""
    error_previo: str | None = None

    for intento in range(1, _MAX_REINTENTOS_SQL + 1):
        if query_log:
            query_log.sql_intentos = intento

        _t_sql = _time.perf_counter()
        try:
            sql_final = _construir_sql_con_llm(pregunta, sql_filtro, entidades, texto_usuario, error_previo)
        except Exception as exc:
            error_previo = str(exc)
            if query_log:
                query_log.sql_exitoso = False
                query_log.sql_error = str(exc)
                query_log.latencia_sql_ms = int((_time.perf_counter() - _t_sql) * 1000)
            if intento == _MAX_REINTENTOS_SQL:
                return "❌ No disponible. No fue posible construir la consulta con el modelo de IA.", None
            continue

        if query_log:
            query_log.latencia_sql_ms = int((_time.perf_counter() - _t_sql) * 1000)

        if debug_out is not None:
            debug_out["sql"] = sql_final

        if _SQL_FORBIDDEN.search(sql_final):
            return "❌ Operación no permitida. Solo se aceptan consultas de lectura (SELECT).", sql_final

        _t_bq = _time.perf_counter()
        try:
            rows = list(client.query(sql_final).result())
        except Exception as exc:
            error_previo = str(exc)
            if query_log:
                query_log.bq_exitoso = False
                query_log.bq_error = str(exc)
                query_log.latencia_bq_ms = int((_time.perf_counter() - _t_bq) * 1000)
            if intento == _MAX_REINTENTOS_SQL:
                return "❌ No disponible. La consulta no pudo ejecutarse correctamente.", sql_final
            continue

        if query_log:
            query_log.latencia_bq_ms = int((_time.perf_counter() - _t_bq) * 1000)
            query_log.bq_exitoso = True
            query_log.bq_filas = len(rows)
            query_log.sql_exitoso = True

        if not rows:
            return "No se encontraron resultados para su consulta.", sql_final

        filas_truncadas = len(rows) > _MAX_FILAS_PROMPT
        tabla = _formatear_filas(rows[:_MAX_FILAS_PROMPT])
        aviso_truncado = (
            f"\n[NOTA: Se muestran las primeras {_MAX_FILAS_PROMPT} filas de {len(rows)} totales. "
            "Indica al usuario que puede agregar filtros para acotar los resultados.]\n"
            if filas_truncadas else ""
        )

        solicitud_display = texto_usuario if texto_usuario else pregunta["texto"]
        entity_resolution = pregunta.get("entity_resolution", "")
        entity_resolution_bloque = (
            f"\nFORMATO ESPECÍFICO DE RESPUESTA PARA ESTE CASO DE USO:\n{entity_resolution}\n"
            if entity_resolution else ""
        )
        prompt = (
            f"Eres un asistente especializado de Claro Insurance.\n"
            f"El usuario solicitó: \"{solicitud_display}\".\n\n"
            f"Datos obtenidos de la base de datos:\n{tabla}{aviso_truncado}\n\n"
            f"INSTRUCCIONES DE RESPUESTA:\n"
            f"- Saluda brevemente e informa el resultado de forma directa y profesional.\n"
            f"- Si los datos tienen UNA sola fila con totales o conteos globales: preséntala como un número resumen.\n"
            f"- Si los datos tienen MÚLTIPLES filas agrupadas (por carrier, agencia, estado, etc.): "
            f"muestra CADA fila con su categoría y valor, en formato de lista o tabla simple. "
            f"No colapses el desglose en un solo total.\n"
            f"- Si los datos son registros individuales (IDs, contratos, oportunidades): "
            f"NO los enumeres uno por uno. Solo indica cuántos se encontraron "
            f"y ofrece al usuario explorar detalles específicos si lo desea.\n"
            f"- Al final agrega estas dos líneas exactas, en este orden:\n"
            f"  'Te invito a realizar otra pregunta para seguir explorando.'\n"
            f"  'Recuerde que si su consulta no fue efectiva, puede escribir \"escalar a un humano\"'.\n"
            f"- NO uses cierres formales como 'Atentamente' ni firmas de ningún tipo.\n"
            f"- Responde en español de forma clara y concisa.\n"
            f"{entity_resolution_bloque}"
        )

        _t_resp = _time.perf_counter()
        try:
            response = _llm_call(llm_model, prompt)
            if query_log:
                query_log.latencia_respuesta_ms = int((_time.perf_counter() - _t_resp) * 1000)
            return response.text.strip(), sql_final
        except Exception as exc:
            return f"❌ Error al generar la respuesta: {exc}", sql_final

    return "❌ No disponible. No fue posible completar la consulta.", None


def rag_response(
    pregunta_config: dict,
    user_query: str,
    query_log=None,
    debug_out: dict | None = None,
) -> str:
    _t = _time.perf_counter()

    result = _ejecutar_rag_silenciosa(pregunta_config, user_query)

    if query_log:
        query_log.rag_coleccion = pregunta_config.get("coleccion_chroma", "documentos_normativos")
        query_log.latencia_rag_ms = int((_time.perf_counter() - _t) * 1000)

    if result.get("error"):
        return f"❌ Error al consultar documentos: {result['error']}"

    docs = result.get("documentos", [])

    if query_log:
        query_log.rag_documentos = len(docs)

    if debug_out is not None:
        debug_out["chunks"] = docs

    if not docs:
        return "No se encontraron documentos relacionados con tu consulta."

    contexto = "\n\n---\n\n".join(docs)
    entity_resolution = pregunta_config.get("entity_resolution", "")
    entity_resolution_bloque = (
        f"\n\nFORMATO ESPECÍFICO DE RESPUESTA PARA ESTE CASO DE USO:\n{entity_resolution}"
        if entity_resolution else ""
    )
    prompt = (
        f"Eres un asistente especializado de Claro Insurance.\n"
        f"El usuario preguntó: \"{user_query}\"\n\n"
        f"Basándote ÚNICAMENTE en los siguientes fragmentos de documentos:\n\n"
        f"{contexto}\n\n"
        f"Responde directamente la pregunta en español de forma clara y concisa. "
        f"Si la información no está en los documentos, indícalo. "
        f"NO uses cierres formales como 'Atentamente' ni firmas. "
        f"Al final agrega estas dos líneas exactas, en este orden: "
        f"'Le invitamos a realizar otra pregunta para seguir explorando.' "
        f"'Recuerde que si su consulta no fue efectiva, puede escribir \"escalar a un humano\"'."
        f"{entity_resolution_bloque}"
    )
    _t_resp = _time.perf_counter()
    try:
        response = _llm_call(llm_model, prompt)
        if query_log:
            query_log.latencia_respuesta_ms = int((_time.perf_counter() - _t_resp) * 1000)
        return response.text.strip()
    except Exception as exc:
        return f"❌ Error al generar respuesta: {exc}"


def multiple_response(
    pregunta: dict,
    catalogo_key: str,
    sql_filtro: str | None,
    filtro_fijo_key: str | None,
    user_query: str,
    entidades_previas: dict | None = None,
    debug_out: dict | None = None,
) -> str:
    invoca_ids = pregunta.get("invoca", [])
    if not invoca_ids:
        return "⚠ Este flujo no tiene sub-consultas configuradas."

    catalogo = USE_CASES["options"].get(catalogo_key, {})
    preguntas_map = {p["id"]: p for p in catalogo.get("preguntas", [])}
    sub_preguntas = [preguntas_map[i] for i in invoca_ids if i in preguntas_map]

    if not sub_preguntas:
        return "⚠ No se encontraron las sub-consultas referenciadas."

    entidades_combinadas: dict = entidades_previas or {}
    if not entidades_combinadas:
        params_all = list(dict.fromkeys(
            p for sp in sub_preguntas if sp.get("tipo") == "sql"
            for p in sp.get("parametros", [])
        ))
        if params_all:
            entidades_combinadas = extraer_entidades(user_query, params_all, filtro_fijo_key)

    def _entidades_para(sp: dict) -> dict:
        sp_params = set(sp.get("parametros", []))
        return {p: v for p, v in entidades_combinadas.items() if p in sp_params}

    resultados: dict = {}
    with ThreadPoolExecutor(max_workers=len(sub_preguntas)) as executor:
        futures: dict = {}
        for sp in sub_preguntas:
            tipo_sp = sp.get("tipo")
            if tipo_sp == "sql":
                fut = executor.submit(
                    _ejecutar_consulta_silenciosa, sp, sql_filtro, _entidades_para(sp), user_query
                )
            elif tipo_sp == "rag":
                fut = executor.submit(_ejecutar_rag_silenciosa, sp, user_query)
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

    if debug_out is not None:
        debug_out["sqls"] = {
            nombre: res["sql"]
            for nombre, res in resultados.items()
            if res.get("tipo") == "sql" and res.get("sql")
        }

    return _sintetizar_respuestas_multiples(
        user_query,
        resultados,
        entity_resolution=pregunta.get("entity_resolution", ""),
    )
