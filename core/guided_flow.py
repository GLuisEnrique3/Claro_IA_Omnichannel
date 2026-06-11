"""
GuidedFlow — máquina de estados que replica el flujo guiado del CLI (main.py)
para canales conversacionales request/response (Google Chat).

main.py NO se modifica: este módulo importa sus funciones (detección de caso
de uso, extracción de entidades, ejecución SQL/RAG/multiple, etc.) y replica
únicamente la orquestación basada en input()/print() como máquina de estados.
Los textos de los prompts son copias EXACTAS de los prints del CLI; la salida
de las funciones que imprimen directamente (ejecutar_consulta, ejecutar_rag,
ejecutar_multiple, _mostrar_instrucciones, _header) se captura tal cual via
core/captura_stdout.py para garantizar paridad sin duplicar textos.

Estados:
    IDENT_TIPO   → espera opción 1/2/3 (tipo de usuario)
    IDENT_VALOR  → espera nombre de agencia / NPN
    QUERY        → espera consulta en lenguaje natural
    REFORMULAR   → espera consulta reformulada (faltan filtros requeridos)
    CONFIRM      → espera S / N / Enter(omitir) para ejecutar
    RETRY        → espera S/N (¿Desea intentar de nuevo?)
    OTRA         → espera S/N (¿Desea realizar otra consulta?)

Divergencias deliberadas (mínimas) respecto al CLI:
    - "Enter" no existe en chat: el botón "Omitir filtros" envía el sentinel
      __OMITIR__ y también se acepta la palabra "omitir" escrita.
    - "volver" fuera de la zona de confirmación crashea el CLI (VoverError no
      capturado); en chat se mapea al comportamiento amigable más cercano.
"""
import time

from dataclasses import dataclass, field

import main as cli
from config.logger import SessionLogger
from core.captura_stdout import capturar, instalar_proxy
from core.session_store import session_store

# El proxy debe estar instalado antes de cualquier _capturado(); idempotente.
instalar_proxy()

_SEP = "━" * 55

# Sentinel del botón "Omitir filtros" (equivalente a Enter vacío en el CLI)
OMITIR_SENTINEL = "__OMITIR__"

# Ventana del historial usado por reescribir_consulta. El CLI conserva 4
# entradas pero solo consume la última ([-1:]); aquí se conservan 10 turnos
# según el ticket — el comportamiento del rewriter es idéntico.
_MAX_HISTORIAL = 10

# SessionLogger por sesión de chat (en memoria — se recrea tras un reinicio)
_loggers: dict[str, SessionLogger] = {}


@dataclass
class FlowResult:
    """Resultado de un paso del flujo: líneas a mostrar + botones sugeridos."""
    lineas: list[str] = field(default_factory=list)
    botones: list[tuple[str, str]] | None = None
    terminado: bool = False

    @property
    def texto(self) -> str:
        return "\n".join(self.lineas)


# ── Helpers de render (textos copiados EXACTAMENTE del CLI) ───────────────────

def _capturado(func, *args, **kwargs):
    """Ejecuta una función de main.py capturando sus prints. Retorna (salida, retorno)."""
    with capturar() as buf:
        retorno = func(*args, **kwargs)
    return buf.getvalue().removesuffix("\n"), retorno


def _lineas_header() -> list[str]:
    salida, _ = _capturado(cli._header)
    return [salida]


def _lineas_menu_identificacion() -> list[str]:
    lineas = ["🔐 IDENTIFICACIÓN DE USUARIO", _SEP, "Seleccione su tipo de usuario:"]
    for k, v in cli.TIPOS_USUARIO.items():
        lineas.append(f"  {k}. {v['nombre']}")
    lineas += ["", "Opción: "]
    return lineas


def _botones_identificacion() -> list[tuple[str, str]]:
    return [(f"{k}. {v['nombre']}", k) for k, v in cli.TIPOS_USUARIO.items()]


def _lineas_menu_consulta() -> list[str]:
    return [_SEP, "", "¿Qué desea consultar hoy?", "", "  Su consulta: "]


_PROMPT_CONFIRMAR = "  ¿Desea Confirmar? (S = confirmar / N = reformular filtros / Enter = omitir filtros): "
_PROMPT_RETRY = "  ¿Desea intentar de nuevo? (S/N): "
_PROMPT_OTRA = "¿Desea realizar otra consulta? (S/N): "

_BOTONES_SN = [("S", "S"), ("N", "N")]
_BOTONES_CONFIRMAR = [
    ("✅ Confirmar (S)", "S"),
    ("✏️ Reformular filtros (N)", "N"),
    ("⏭ Omitir filtros (Enter)", OMITIR_SENTINEL),
]


def _prompt_actual(session: dict) -> tuple[list[str], list[tuple[str, str]] | None]:
    """Prompt + botones del estado actual (para re-preguntar tras un keyword global)."""
    estado = session.get("state")
    if estado == "IDENT_TIPO":
        return ["Opción: "], _botones_identificacion()
    if estado == "IDENT_VALOR":
        tipo = cli.TIPOS_USUARIO[session["tipo_key"]]
        return [tipo["prompt_id"]], None
    if estado in ("QUERY", "REFORMULAR"):
        return ["  Su consulta: "], None
    if estado == "CONFIRM":
        return [_PROMPT_CONFIRMAR], _BOTONES_CONFIRMAR
    if estado == "RETRY":
        return [_PROMPT_RETRY], _BOTONES_SN
    if estado == "OTRA":
        return [_PROMPT_OTRA], _BOTONES_SN
    return [], None


def _get_logger(session_key: str, nuevo: bool = False) -> SessionLogger:
    if nuevo or session_key not in _loggers:
        _loggers[session_key] = SessionLogger()
    return _loggers[session_key]


def _entidades_a_json(entidades: dict) -> dict:
    return {p: list(v) for p, v in entidades.items()}


def _entidades_de_json(entidades: dict) -> dict:
    return {p: tuple(v) for p, v in (entidades or {}).items()}


# ── Entrada principal ──────────────────────────────────────────────────────────

def iniciar_sesion(session_key: str) -> FlowResult:
    """Crea una sesión nueva y retorna header + menú de identificación (como main())."""
    _get_logger(session_key, nuevo=True)
    session = {"state": "IDENT_TIPO"}
    session_store.save(session_key, session)
    lineas = _lineas_header() + _lineas_menu_identificacion()
    return FlowResult(lineas=lineas, botones=_botones_identificacion())


def step(session_key: str, texto: str) -> FlowResult:
    """
    Procesa un mensaje del usuario contra la sesión dada.
    Si no hay sesión (nueva o expirada), inicia el flujo desde la identificación.
    """
    texto = (texto or "").strip()
    session = session_store.load(session_key)

    if session is None:
        return iniciar_sesion(session_key)

    resultado = _procesar(session_key, session, texto)

    if resultado.terminado:
        session_store.delete(session_key)
        _loggers.pop(session_key, None)
    else:
        session_store.save(session_key, session)
    return resultado


def _procesar(session_key: str, session: dict, texto: str) -> FlowResult:
    lower = texto.lower()

    # ── Keywords globales — réplica de _input() en main.py ────────────────────
    if lower in cli._PALABRAS_SALIR:
        return FlowResult(lineas=["", "Sesión finalizada. ¡Hasta pronto!"], terminado=True)

    if lower in cli._PALABRAS_MENU:
        session.clear()
        session["state"] = "IDENT_TIPO"
        lineas = ["", "  Volviendo a la identificación de usuario...", ""]
        lineas += _lineas_menu_identificacion()
        return FlowResult(lineas=lineas, botones=_botones_identificacion())

    if lower in cli._PALABRAS_VOLVER:
        return _manejar_volver(session_key, session)

    if lower in cli._PALABRAS_INSTRUCCIONES:
        salida, _ = _capturado(cli._mostrar_instrucciones)
        prompt, botones = _prompt_actual(session)
        return FlowResult(lineas=[salida] + prompt, botones=botones)

    if lower in cli._PALABRAS_ESCALAR:
        prompt, botones = _prompt_actual(session)
        return FlowResult(
            lineas=["", "  Funcionalidad todavía en proceso.", ""] + prompt,
            botones=botones,
        )

    if lower in cli._PALABRAS_SALUDO:
        saludo = [
            "",
            "  ¡Hola! Soy tu asistente de Claro Insurance.",
            "  Puedo ayudarte con consultas sobre contratos, comisiones y documentos normativos.",
            '  Escribe "instrucciones" para ver todo lo que puedes consultar.',
            "  ¡Quedo atento!",
            "",
        ]
        prompt, botones = _prompt_actual(session)
        return FlowResult(lineas=saludo + prompt, botones=botones)

    # ── Dispatch por estado ────────────────────────────────────────────────────
    estado = session.get("state", "IDENT_TIPO")
    if estado == "IDENT_TIPO":
        return _handle_ident_tipo(session_key, session, texto)
    if estado == "IDENT_VALOR":
        return _handle_ident_valor(session_key, session, texto)
    if estado == "QUERY":
        return _handle_query(session_key, session, texto)
    if estado == "REFORMULAR":
        return _handle_reformular(session_key, session, texto)
    if estado == "CONFIRM":
        return _handle_confirm(session_key, session, texto)
    if estado == "RETRY":
        return _handle_retry(session_key, session, texto)
    if estado == "OTRA":
        return _handle_otra(session_key, session, texto)

    # Estado desconocido — reiniciar de forma segura
    session.clear()
    session["state"] = "IDENT_TIPO"
    return FlowResult(
        lineas=_lineas_header() + _lineas_menu_identificacion(),
        botones=_botones_identificacion(),
    )


def _manejar_volver(session_key: str, session: dict) -> FlowResult:
    """
    'volver' en el CLI lanza VoverError: dentro de la zona de confirmación se
    captura ("Volviendo al menú principal..."); en el resto de prompts el CLI
    crashea — aquí se mapea al comportamiento amigable más cercano.
    """
    estado = session.get("state")
    if estado in ("CONFIRM", "REFORMULAR"):
        _get_logger(session_key).cerrar_consulta()
        session.pop("pendiente", None)
        session["state"] = "QUERY"
        lineas = ["", "  Volviendo al menú principal...", ""] + _lineas_menu_consulta()
        return FlowResult(lineas=lineas)

    if estado in ("IDENT_TIPO", "IDENT_VALOR"):
        session.clear()
        session["state"] = "IDENT_TIPO"
        lineas = ["", "  Volviendo a la identificación de usuario...", ""]
        lineas += _lineas_menu_identificacion()
        return FlowResult(lineas=lineas, botones=_botones_identificacion())

    session["state"] = "QUERY"
    lineas = ["", "  Volviendo al menú principal...", ""] + _lineas_menu_consulta()
    return FlowResult(lineas=lineas)


# ── Identificación ─────────────────────────────────────────────────────────────

def _handle_ident_tipo(session_key: str, session: dict, texto: str) -> FlowResult:
    opcion = texto.strip()
    if opcion not in cli.TIPOS_USUARIO:
        lineas = ["⚠  Opción no válida. Intente nuevamente.", ""]
        lineas += _lineas_menu_identificacion()
        return FlowResult(lineas=lineas, botones=_botones_identificacion())

    tipo = cli.TIPOS_USUARIO[opcion]
    session["tipo_key"] = opcion

    if tipo["filtro_key"] is None:
        return _completar_identificacion(session_key, session, opcion, tipo["nombre"], None, None)

    candidatos = cli.FILTROS_VALIDOS.get(tipo["filtro_key"], [])
    if not candidatos:
        lineas = [f"⚠  Sin registros en filtro '{tipo['filtro_key']}'. Contacte al administrador.", ""]
        lineas += _lineas_menu_identificacion()
        return FlowResult(lineas=lineas, botones=_botones_identificacion())

    session["state"] = "IDENT_VALOR"
    return FlowResult(lineas=[tipo["prompt_id"]])


def _handle_ident_valor(session_key: str, session: dict, texto: str) -> FlowResult:
    tipo_key = session["tipo_key"]
    tipo = cli.TIPOS_USUARIO[tipo_key]
    valor_input = texto.strip()

    if not valor_input:
        return FlowResult(lineas=["⚠  El campo no puede estar vacío.", "", tipo["prompt_id"]])

    candidatos = cli.FILTROS_VALIDOS.get(tipo["filtro_key"], [])
    embs_pre = cli.FILTROS_EMBEDDINGS.get(tipo["filtro_key"], {}).get("embeddings")

    match, score = cli._buscar_semantico(valor_input, candidatos, tipo["umbral"], embs_pre)

    if not match:
        lineas = [
            f"⚠  Sin coincidencia para '{valor_input}' "
            f"(mejor similitud: {score:.2f}, umbral: {tipo['umbral']:.2f}).\n"
            "   Verifique e intente nuevamente.",
            "",
            tipo["prompt_id"],
        ]
        return FlowResult(lineas=lineas)

    return _completar_identificacion(session_key, session, tipo_key, tipo["nombre"], match, score)


def _completar_identificacion(
    session_key: str,
    session: dict,
    tipo_key: str,
    nombre_tipo: str,
    valor_id: str | None,
    score: float | None,
) -> FlowResult:
    """Réplica del cuerpo de main(): filtro SQL, agencia, bienvenida y arranque del ciclo."""
    logger = _get_logger(session_key)
    logger.registrar_identificacion(nombre_tipo, valor_id, score)

    tipo = cli.TIPOS_USUARIO[tipo_key]
    sql_filtro = None
    if tipo["sql_filtro"] and valor_id:
        valor_seguro = valor_id.replace("'", "''")
        sql_filtro = tipo["sql_filtro"].replace("{valor}", valor_seguro)

    # Resolver agencia del usuario (para filtros de RAG por agencia)
    agency_name = None
    if tipo_key == "1":
        agency_name = valor_id
    elif tipo_key == "2" and valor_id:
        valor_seguro = valor_id.replace("'", "''")
        rows = list(cli.client.query(f"""
            SELECT a.Name_Agencies
            FROM `claroinsurance-dataplatform.salesforce_claro.contact` c
            LEFT JOIN `claroinsurance-dataplatform.claro_bi.dim_account_2` a ON a.Id = c.AccountId
            WHERE c.NPN__c = '{valor_seguro}'
            LIMIT 1
        """).result())
        agency_name = rows[0]["Name_Agencies"] if rows else None

    lineas = [""]
    if valor_id and score is not None:
        if tipo_key == "2" and agency_name:
            lineas.append(
                f'✅ Bienvenido, el sistema ha reconocido tu ingreso "NPN:{valor_id}", '
                f'"Agencia:{agency_name}", nivel de coincidencia {score:.2f}'
            )
        else:
            lineas.append(
                f'✅ Bienvenido, el sistema ha reconocido tu ingreso "{valor_id}", '
                f"nivel de coincidencia {score:.2f}"
            )
    else:
        lineas.append(f"✅ Bienvenido, {nombre_tipo}")

    _perms = cli._CATALOG_PERMS.get(tipo_key)
    catalogos = [c for c in tipo["catalogos"] if c in _perms] if _perms else tipo["catalogos"]

    session.update({
        "state": "QUERY",
        "tipo_key": tipo_key,
        "nombre_tipo": nombre_tipo,
        "valor_id": valor_id,
        "score": score,
        "sql_filtro": sql_filtro,
        "filtro_fijo_key": tipo["filtro_key"],
        "agency_name": agency_name,
        "catalogos": catalogos,
        "historial_reciente": [],
    })

    lineas += _lineas_menu_consulta()
    return FlowResult(lineas=lineas)


# ── Ciclo de consultas (réplica de ciclo_consultas) ────────────────────────────

def _handle_query(session_key: str, session: dict, texto: str) -> FlowResult:
    if not texto:
        return FlowResult(lineas=["  Por favor ingrese una consulta."] + _lineas_menu_consulta())
    return _pipeline_consulta(session_key, session, texto)


def _handle_reformular(session_key: str, session: dict, texto: str) -> FlowResult:
    # Réplica del bloque filtros_requeridos: la nueva consulta reinicia el
    # pipeline completo (_next_query en el CLI). Vacío → VoverError.
    if not texto:
        return _manejar_volver(session_key, session)
    _get_logger(session_key).cerrar_consulta()
    session["state"] = "QUERY"
    return _pipeline_consulta(session_key, session, texto)


def _pipeline_consulta(session_key: str, session: dict, user_query: str) -> FlowResult:
    """Réplica exacta de una iteración de ciclo_consultas hasta la confirmación."""
    logger = _get_logger(session_key)
    q = logger.nueva_consulta()
    q.query_original = user_query

    catalogos = session["catalogos"]
    filtro_fijo_key = session["filtro_fijo_key"]
    historial = [
        {"role": "user", "content": c} for c in session.get("historial_reciente", [])
    ]

    lineas = ["", "  Analizando su consulta..."]

    # Solo pasa la última entrada al rewriter (ver comentario en ciclo_consultas)
    if len(user_query.split()) > 7:
        user_query_efectiva = user_query
    else:
        user_query_efectiva = cli.reescribir_consulta(historial[-1:], user_query)
    if user_query_efectiva != user_query:
        lineas.append(f"  #--DEBUG Consulta reescrita: {user_query_efectiva}")

    _t = time.perf_counter()
    normalized = cli.transformar_consulta_con_llm(user_query_efectiva)
    q.latencia_normalizacion_ms = int((time.perf_counter() - _t) * 1000)
    q.query_normalizado = normalized

    lineas.append(f"  #--DEBUG Pregunta Formateada: {normalized}")

    _t = time.perf_counter()
    use_case_entry, score = cli.detectar_caso_de_uso(normalized, catalogos)
    q.latencia_deteccion_ms = int((time.perf_counter() - _t) * 1000)
    q.caso_score = score
    q.caso_exitoso = use_case_entry is not None

    if use_case_entry is None:
        lineas += [
            "",
            "  No fue posible identificar un flujo asociado a su solicitud. Puede reformular su mensaje para intentar nuevamente el proceso automatizado, o escalar su caso a un agente humano para obtener asistencia personalizada.",
            "",
            "  Por favor reformule su consulta con más detalle.",
        ]
        q.reiniciar_timer()  # excluir el tiempo de espera del usuario al decidir si reintenta
        session["state"] = "RETRY"
        lineas.append(_PROMPT_RETRY)
        return FlowResult(lineas=lineas, botones=_BOTONES_SN)

    nombre_caso = use_case_entry["nombre"]
    pregunta = use_case_entry["pregunta"]
    q.caso_nombre = nombre_caso
    q.caso_catalogo = use_case_entry.get("catalogo")

    historial_reciente = session.get("historial_reciente", [])
    historial_reciente.append(user_query_efectiva)
    session["historial_reciente"] = historial_reciente[-_MAX_HISTORIAL:]

    # Pre-detectar entidades (solo para SQL individual) — usa la query reescrita
    tipo_pregunta = pregunta.get("tipo")
    entidades_previas = {}
    if tipo_pregunta == "sql":
        _t = time.perf_counter()
        entidades_previas = cli.extraer_entidades(
            user_query_efectiva, pregunta.get("parametros", []), filtro_fijo_key
        )
        q.latencia_entidades_ms = int((time.perf_counter() - _t) * 1000)
        q.entidades = [
            {"param": p, "label": v[2], "valor": v[0], "score": v[1]}
            for p, v in entidades_previas.items()
        ]

    lineas += [
        "",
        f'  Se ha identificado el flujo: "{nombre_caso}" - (Nivel de coincidencia: {score:.2f})',
    ]

    if tipo_pregunta == "multiple":
        catalogo_actual = cli.USE_CASES["options"].get(use_case_entry["catalogo"], {})
        preguntas_map_actual = {p["id"]: p for p in catalogo_actual.get("preguntas", [])}
        invoca_nombres = [
            preguntas_map_actual[i]["texto"]
            for i in pregunta.get("invoca", [])
            if i in preguntas_map_actual
        ]
        lineas.append(f"  Se ejecutarán {len(invoca_nombres)} consultas en paralelo:")
        for n in invoca_nombres:
            lineas.append(f"      • {n}")
        # Extraer entidades por sub-caso y mostrar agrupadas (usa la query ORIGINAL)
        sub_pqs = [preguntas_map_actual[i] for i in pregunta.get("invoca", []) if i in preguntas_map_actual]
        entidades_por_subcaso: dict = {}
        for sp in sub_pqs:
            if sp.get("tipo") == "sql" and sp.get("parametros"):
                entidades_por_subcaso[sp["texto"]] = cli.extraer_entidades(
                    user_query, sp["parametros"], filtro_fijo_key
                )
        lineas.append("")
        for nombre_sp, ents in entidades_por_subcaso.items():
            lineas.append(f"  Entidades detectadas [{nombre_sp}]:")
            if ents:
                for _, (val, sc_e, lbl, _snip) in ents.items():
                    lineas.append(f"      • {lbl:<20} → {val}  (confianza: {sc_e:.2f})")
            else:
                lineas.append(f"      (ninguna)")
        # Fusionar para ejecución (sin duplicados, el primero gana)
        entidades_previas = {}
        for ents in entidades_por_subcaso.values():
            for param, val in ents.items():
                if param not in entidades_previas:
                    entidades_previas[param] = val
        if entidades_previas:
            q.entidades = [
                {"param": p, "label": v[2], "valor": v[0], "score": v[1]}
                for p, v in entidades_previas.items()
            ]
    elif entidades_previas:
        lineas.append("  Con las siguientes entidades:")
        for _, (val, sc_e, lbl, _snip) in entidades_previas.items():
            lineas.append(f"      • {lbl:<20} → {val}  (confianza: {sc_e:.2f})")
    else:
        lineas.append("  Sin ninguna entidad detectada.")

    q.tipo_ejecucion = tipo_pregunta

    # ── Validación de filtros requeridos + confirmación ───────────────────────
    filtros_req_pre = pregunta.get("filtros_requeridos", [])
    if filtros_req_pre and tipo_pregunta != "rag" and not all(p in entidades_previas for p in filtros_req_pre):
        labels_req_pre = [cli.PARAM_TO_FILTRO[p][1] for p in filtros_req_pre if p in cli.PARAM_TO_FILTRO]
        lbl_slash_pre = "/".join(labels_req_pre)
        lineas += [
            "",
            f"  Esta consulta requiere identificar los siguientes filtros/entidades ({lbl_slash_pre}),",
            "  por favor reformule su pregunta considerando dichos filtros",
            "",
            "  Su consulta: ",
        ]
        session["state"] = "REFORMULAR"
        return FlowResult(lineas=lineas)

    session["state"] = "CONFIRM"
    session["pendiente"] = {
        "catalogo": use_case_entry.get("catalogo"),
        "pregunta": pregunta,
        "nombre_caso": nombre_caso,
        "tipo_pregunta": tipo_pregunta,
        "user_query": user_query,
        "entidades": _entidades_a_json(entidades_previas),
    }
    lineas += ["", _PROMPT_CONFIRMAR]
    return FlowResult(lineas=lineas, botones=_BOTONES_CONFIRMAR)


def _handle_confirm(session_key: str, session: dict, texto: str) -> FlowResult:
    respuesta = texto.strip().upper()
    if respuesta == OMITIR_SENTINEL.upper() or respuesta == "OMITIR":
        respuesta = ""

    if respuesta not in ("S", "N", ""):
        lineas = ["  ⚠  Opción no válida. Por favor ingrese: S / N / Enter.", "", _PROMPT_CONFIRMAR]
        return FlowResult(lineas=lineas, botones=_BOTONES_CONFIRMAR)

    logger = _get_logger(session_key)

    if respuesta == "N":
        # VoverError → "Volviendo al menú principal..."
        logger.cerrar_consulta()
        session.pop("pendiente", None)
        session["state"] = "QUERY"
        lineas = ["", "  Volviendo al menú principal...", ""] + _lineas_menu_consulta()
        return FlowResult(lineas=lineas)

    return _ejecutar_pendiente(session_key, session, confirmar=respuesta)


def _ejecutar_pendiente(session_key: str, session: dict, confirmar: str) -> FlowResult:
    pendiente = session.pop("pendiente", None)
    session["state"] = "QUERY"
    if not pendiente:
        return FlowResult(lineas=_lineas_menu_consulta())

    logger = _get_logger(session_key)
    q = logger.current
    if q is None:
        # El proceso se reinició entre la pregunta y la confirmación
        q = logger.nueva_consulta()
        q.query_original = pendiente["user_query"]
        q.caso_nombre = pendiente["nombre_caso"]
        q.caso_catalogo = pendiente["catalogo"]
        q.tipo_ejecucion = pendiente["tipo_pregunta"]

    pregunta = pendiente["pregunta"]
    tipo_pregunta = pendiente["tipo_pregunta"]
    user_query = pendiente["user_query"]
    entidades_previas = _entidades_de_json(pendiente["entidades"])

    sql_filtro = session.get("sql_filtro")
    filtro_fijo_key = session.get("filtro_fijo_key")
    tipo_key = session.get("tipo_key", "3")
    agency_name = session.get("agency_name")

    lineas: list[str] = []

    # Reiniciar timer: excluir el tiempo que el usuario tardó en confirmar
    q.reiniciar_timer()

    if tipo_pregunta == "multiple":
        lineas += ["", "  Procesando su consulta, por favor espere..."]
        salida, _ = _capturado(
            cli.ejecutar_multiple,
            pregunta, pendiente["catalogo"], sql_filtro, filtro_fijo_key, user_query,
            entidades_previas, tipo_key=tipo_key, agency_name=agency_name,
        )
        lineas.append(salida)
    elif tipo_pregunta == "rag":
        salida, _ = _capturado(
            cli.ejecutar_rag,
            pregunta, user_query, query_log=q, agency_name=agency_name, tipo_key=tipo_key,
        )
        lineas.append(salida)
    else:
        texto_usuario = user_query if confirmar == "S" else ""
        entidades = entidades_previas if confirmar == "S" else {}

        lineas += ["", "  Procesando su consulta, por favor espere..."]
        salida, respuesta = _capturado(
            cli.ejecutar_consulta,
            pregunta, sql_filtro, entidades, texto_usuario, query_log=q, tipo_key=tipo_key,
        )
        if salida:
            lineas.append(salida)
        lineas += ["", "RESULTADO:", _SEP, respuesta]

    logger.cerrar_consulta()

    session["state"] = "OTRA"
    lineas += ["", _PROMPT_OTRA]
    return FlowResult(lineas=lineas, botones=_BOTONES_SN)


def _handle_retry(session_key: str, session: dict, texto: str) -> FlowResult:
    respuesta = texto.strip().upper()
    if respuesta not in ("S", "N"):
        lineas = ["  ⚠  Opción no válida. Por favor ingrese: S / N.", "", _PROMPT_RETRY]
        return FlowResult(lineas=lineas, botones=_BOTONES_SN)

    logger = _get_logger(session_key)
    logger.cerrar_consulta()

    if respuesta != "S":
        return FlowResult(
            lineas=["", "Gracias por utilizar el Sistema de Consultas IA. ¡Hasta pronto!"],
            terminado=True,
        )

    session["state"] = "QUERY"
    return FlowResult(lineas=_lineas_menu_consulta())


def _handle_otra(session_key: str, session: dict, texto: str) -> FlowResult:
    respuesta = texto.strip().upper()
    if respuesta not in ("S", "N"):
        lineas = ["  ⚠  Opción no válida. Por favor ingrese: S / N.", "", _PROMPT_OTRA]
        return FlowResult(lineas=lineas, botones=_BOTONES_SN)

    if respuesta != "S":
        return FlowResult(
            lineas=["", "Gracias por utilizar el Sistema de Consultas IA. ¡Hasta pronto!"],
            terminado=True,
        )

    session["state"] = "QUERY"
    return FlowResult(lineas=[""] + _lineas_menu_consulta())
