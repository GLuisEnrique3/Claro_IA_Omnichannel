"""
GuidedFlow — máquina de estados que replica el flujo guiado del CLI (main.py)
para canales conversacionales request/response (Google Chat).

main.py NO se modifica: este módulo importa sus funciones (detección de caso
de uso, extracción de entidades, ejecución SQL/RAG/multiple, etc.) y replica
únicamente la orquestación basada en input()/print() como máquina de estados.

Cada paso retorna un FlowResult compuesto de Bloques semánticos:
  - `texto` (CLI): copia EXACTA de lo que imprime el CLI — paridad verificable.
  - `kind` + `data`: información estructurada para que cada canal renderice
    a su manera (Google Chat usa adapters/chat_render.py).
La salida de las funciones de main.py que imprimen directamente
(ejecutar_consulta, ejecutar_rag, ejecutar_multiple, _mostrar_instrucciones,
_header) se captura tal cual via core/captura_stdout.py.

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
import re
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
class Bloque:
    """
    Unidad semántica de salida.
    `texto` es la réplica EXACTA del CLI; `data` lleva la versión estructurada
    para renders por canal (puede ser None si el texto basta).
    """
    kind: str
    texto: str
    data: dict | None = None


@dataclass
class FlowResult:
    """Resultado de un paso del flujo: bloques semánticos + botones sugeridos."""
    bloques: list[Bloque] = field(default_factory=list)
    botones: list[tuple[str, str]] | None = None
    terminado: bool = False

    @property
    def lineas(self) -> list[str]:
        """Render CLI línea a línea (paridad exacta con main.py)."""
        return [linea for b in self.bloques for linea in b.texto.split("\n")]

    @property
    def texto(self) -> str:
        """Render CLI completo (paridad exacta con main.py)."""
        return "\n".join(self.lineas)


# ── Helpers de render (textos copiados EXACTAMENTE del CLI) ───────────────────

def _capturado(func, *args, **kwargs):
    """Ejecuta una función de main.py capturando sus prints. Retorna (salida, retorno)."""
    with capturar() as buf:
        retorno = func(*args, **kwargs)
    return buf.getvalue().removesuffix("\n"), retorno


def _bloque_header() -> Bloque:
    salida, _ = _capturado(cli._header)
    return Bloque("header", salida)


def _bloque_menu_identificacion() -> Bloque:
    lineas = ["🔐 IDENTIFICACIÓN DE USUARIO", _SEP, "Seleccione su tipo de usuario:"]
    for k, v in cli.TIPOS_USUARIO.items():
        lineas.append(f"  {k}. {v['nombre']}")
    lineas += ["", "Opción: "]
    return Bloque(
        "ident_menu",
        "\n".join(lineas),
        data={"tipos": [(k, v["nombre"]) for k, v in cli.TIPOS_USUARIO.items()]},
    )


def _botones_identificacion() -> list[tuple[str, str]]:
    return [(f"{k}. {v['nombre']}", k) for k, v in cli.TIPOS_USUARIO.items()]


def _bloque_menu_consulta() -> Bloque:
    return Bloque(
        "menu_consulta",
        "\n".join([_SEP, "", "¿Qué desea consultar hoy?", "", "  Su consulta: "]),
    )


_PROMPT_CONFIRMAR = "  ¿Desea Confirmar? (S = confirmar / N = reformular filtros / Enter = omitir filtros): "
_PROMPT_RETRY = "  ¿Desea intentar de nuevo? (S/N): "
_PROMPT_OTRA = "¿Desea realizar otra consulta? (S/N): "

_BOTONES_SN = [("S", "S"), ("N", "N")]
_BOTONES_CONFIRMAR = [
    ("✅ Confirmar (S)", "S"),
    ("✏️ Reformular filtros (N)", "N"),
    ("⏭ Omitir filtros (Enter)", OMITIR_SENTINEL),
]


def _prompt_actual(session: dict) -> tuple[list[Bloque], list[tuple[str, str]] | None]:
    """Prompt + botones del estado actual (para re-preguntar tras un keyword global)."""
    estado = session.get("state")
    if estado == "IDENT_TIPO":
        return [Bloque("prompt_opcion", "Opción: ")], _botones_identificacion()
    if estado == "IDENT_VALOR":
        tipo = cli.TIPOS_USUARIO[session["tipo_key"]]
        return [Bloque("prompt_valor", tipo["prompt_id"])], None
    if estado in ("QUERY", "REFORMULAR"):
        return [Bloque("prompt_consulta", "  Su consulta: ")], None
    if estado == "CONFIRM":
        return [Bloque("confirmar_prompt", _PROMPT_CONFIRMAR)], _BOTONES_CONFIRMAR
    if estado == "RETRY":
        return [Bloque("retry_prompt", _PROMPT_RETRY)], _BOTONES_SN
    if estado == "OTRA":
        return [Bloque("otra_prompt", _PROMPT_OTRA)], _BOTONES_SN
    return [], None


def _get_logger(session_key: str, nuevo: bool = False) -> SessionLogger:
    if nuevo or session_key not in _loggers:
        _loggers[session_key] = SessionLogger()
    return _loggers[session_key]


def _entidades_a_json(entidades: dict) -> dict:
    return {p: list(v) for p, v in entidades.items()}


def _entidades_de_json(entidades: dict) -> dict:
    return {p: tuple(v) for p, v in (entidades or {}).items()}


def _entidades_data(entidades: dict) -> list[dict]:
    return [
        {"label": v[2], "valor": v[0], "score": v[1]}
        for v in entidades.values()
    ]


# ── Parsers de salida capturada (para data estructurada) ──────────────────────

def _parse_rag_salida(salida: str) -> dict:
    """
    Extrae respuesta, documentos y carrier de la salida capturada de
    ejecutar_rag(). Si el formato no coincide, cae a respuesta = salida cruda.
    """
    data: dict = {"respuesta": salida.strip(), "documentos": [], "carrier": None}

    m_carrier = re.search(r"Carrier detectado: (.+)", salida)
    if m_carrier:
        data["carrier"] = m_carrier.group(1).strip()

    idx = salida.rfind("RESPUESTA:\n")
    if idx != -1:
        resto = salida[idx + len("RESPUESTA:\n"):]
        # Saltar la línea separadora (━━━)
        partes = resto.split("\n", 1)
        data["respuesta"] = (partes[1] if len(partes) > 1 else resto).strip()

    # Cajas de documentos: ┌...│ 📌 DOCUMENTO n (Similitud: x%) │ Fuente: ... │ "extracto"
    for caja in salida.split("┌")[1:]:
        cuerpo = caja.split("└")[0]
        lineas_caja = [
            linea.strip().strip("│").strip()
            for linea in cuerpo.split("\n")
            if linea.strip().startswith("│")
        ]
        if not lineas_caja:
            continue
        doc: dict = {"similitud": None, "fuente": None, "extracto": ""}
        m_sim = re.search(r"\(Similitud: ([\d.]+)%\)", lineas_caja[0])
        if m_sim:
            doc["similitud"] = float(m_sim.group(1))
        cuerpo_extracto: list[str] = []
        for linea in lineas_caja[1:]:
            if linea.startswith("Fuente: "):
                doc["fuente"] = linea.removeprefix("Fuente: ").strip()
            elif linea:
                cuerpo_extracto.append(linea)
        doc["extracto"] = " ".join(cuerpo_extracto).strip().strip('"')
        if doc["fuente"] or doc["extracto"]:
            data["documentos"].append(doc)

    return data


def _parse_resultado_multiple(salida: str) -> dict:
    """
    Separa el bloque de debug (SQLs, progreso) de la respuesta final en la
    salida capturada de ejecutar_multiple().
    """
    idx = salida.rfind("\nRESULTADO:\n")
    if idx == -1:
        return {"respuesta": salida.strip(), "debug": ""}
    resto = salida[idx + len("\nRESULTADO:\n"):]
    partes = resto.split("\n", 1)  # saltar la línea separadora
    return {
        "respuesta": (partes[1] if len(partes) > 1 else resto).strip(),
        "debug": salida[:idx].strip("\n"),
    }


# ── Entrada principal ──────────────────────────────────────────────────────────

def iniciar_sesion(session_key: str) -> FlowResult:
    """Crea una sesión nueva y retorna header + menú de identificación (como main())."""
    _get_logger(session_key, nuevo=True)
    session = {"state": "IDENT_TIPO"}
    session_store.save(session_key, session)
    return FlowResult(
        bloques=[_bloque_header(), _bloque_menu_identificacion()],
        botones=_botones_identificacion(),
    )


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
        return FlowResult(
            bloques=[Bloque("salir", "\nSesión finalizada. ¡Hasta pronto!")],
            terminado=True,
        )

    if lower in cli._PALABRAS_MENU:
        session.clear()
        session["state"] = "IDENT_TIPO"
        bloques = [
            Bloque("nueva_sesion", "\n  Volviendo a la identificación de usuario...\n"),
            _bloque_menu_identificacion(),
        ]
        return FlowResult(bloques=bloques, botones=_botones_identificacion())

    if lower in cli._PALABRAS_VOLVER:
        return _manejar_volver(session_key, session)

    if lower in cli._PALABRAS_INSTRUCCIONES:
        salida, _ = _capturado(cli._mostrar_instrucciones)
        prompt, botones = _prompt_actual(session)
        return FlowResult(bloques=[Bloque("instrucciones", salida)] + prompt, botones=botones)

    if lower in cli._PALABRAS_ESCALAR:
        prompt, botones = _prompt_actual(session)
        return FlowResult(
            bloques=[Bloque("escalar", "\n  Funcionalidad todavía en proceso.\n")] + prompt,
            botones=botones,
        )

    if lower in cli._PALABRAS_SALUDO:
        saludo = "\n".join([
            "",
            "  ¡Hola! Soy tu asistente de Claro Insurance.",
            "  Puedo ayudarte con consultas sobre contratos, comisiones y documentos normativos.",
            '  Escribe "instrucciones" para ver todo lo que puedes consultar.',
            "  ¡Quedo atento!",
            "",
        ])
        prompt, botones = _prompt_actual(session)
        return FlowResult(bloques=[Bloque("saludo", saludo)] + prompt, botones=botones)

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
        bloques=[_bloque_header(), _bloque_menu_identificacion()],
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
        bloques = [
            Bloque("volver", "\n  Volviendo al menú principal...\n"),
            _bloque_menu_consulta(),
        ]
        return FlowResult(bloques=bloques)

    if estado in ("IDENT_TIPO", "IDENT_VALOR"):
        session.clear()
        session["state"] = "IDENT_TIPO"
        bloques = [
            Bloque("nueva_sesion", "\n  Volviendo a la identificación de usuario...\n"),
            _bloque_menu_identificacion(),
        ]
        return FlowResult(bloques=bloques, botones=_botones_identificacion())

    session["state"] = "QUERY"
    bloques = [
        Bloque("volver", "\n  Volviendo al menú principal...\n"),
        _bloque_menu_consulta(),
    ]
    return FlowResult(bloques=bloques)


# ── Identificación ─────────────────────────────────────────────────────────────

def _handle_ident_tipo(session_key: str, session: dict, texto: str) -> FlowResult:
    opcion = texto.strip()
    if opcion not in cli.TIPOS_USUARIO:
        bloques = [
            Bloque("invalida", "⚠  Opción no válida. Intente nuevamente.\n"),
            _bloque_menu_identificacion(),
        ]
        return FlowResult(bloques=bloques, botones=_botones_identificacion())

    tipo = cli.TIPOS_USUARIO[opcion]
    session["tipo_key"] = opcion

    if tipo["filtro_key"] is None:
        return _completar_identificacion(session_key, session, opcion, tipo["nombre"], None, None)

    candidatos = cli.FILTROS_VALIDOS.get(tipo["filtro_key"], [])
    if not candidatos:
        bloques = [
            Bloque("invalida", f"⚠  Sin registros en filtro '{tipo['filtro_key']}'. Contacte al administrador.\n"),
            _bloque_menu_identificacion(),
        ]
        return FlowResult(bloques=bloques, botones=_botones_identificacion())

    session["state"] = "IDENT_VALOR"
    return FlowResult(bloques=[Bloque("prompt_valor", tipo["prompt_id"])])


def _handle_ident_valor(session_key: str, session: dict, texto: str) -> FlowResult:
    tipo_key = session["tipo_key"]
    tipo = cli.TIPOS_USUARIO[tipo_key]
    valor_input = texto.strip()

    if not valor_input:
        return FlowResult(bloques=[
            Bloque("invalida", "⚠  El campo no puede estar vacío.\n"),
            Bloque("prompt_valor", tipo["prompt_id"]),
        ])

    candidatos = cli.FILTROS_VALIDOS.get(tipo["filtro_key"], [])
    embs_pre = cli.FILTROS_EMBEDDINGS.get(tipo["filtro_key"], {}).get("embeddings")

    match, score = cli._buscar_semantico(valor_input, candidatos, tipo["umbral"], embs_pre)

    if not match:
        bloques = [
            Bloque(
                "ident_error",
                f"⚠  Sin coincidencia para '{valor_input}' "
                f"(mejor similitud: {score:.2f}, umbral: {tipo['umbral']:.2f}).\n"
                "   Verifique e intente nuevamente.\n",
            ),
            Bloque("prompt_valor", tipo["prompt_id"]),
        ]
        return FlowResult(bloques=bloques)

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

    if valor_id and score is not None:
        if tipo_key == "2" and agency_name:
            bienvenida = (
                f'✅ Bienvenido, el sistema ha reconocido tu ingreso "NPN:{valor_id}", '
                f'"Agencia:{agency_name}", nivel de coincidencia {score:.2f}'
            )
        else:
            bienvenida = (
                f'✅ Bienvenido, el sistema ha reconocido tu ingreso "{valor_id}", '
                f"nivel de coincidencia {score:.2f}"
            )
    else:
        bienvenida = f"✅ Bienvenido, {nombre_tipo}"

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

    bloques = [Bloque("bienvenida", "\n" + bienvenida), _bloque_menu_consulta()]
    return FlowResult(bloques=bloques)


# ── Ciclo de consultas (réplica de ciclo_consultas) ────────────────────────────

def _handle_query(session_key: str, session: dict, texto: str) -> FlowResult:
    if not texto:
        return FlowResult(bloques=[
            Bloque("invalida", "  Por favor ingrese una consulta."),
            _bloque_menu_consulta(),
        ])
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

    bloques: list[Bloque] = [Bloque("status", "\n  Analizando su consulta...")]

    # Solo pasa la última entrada al rewriter (ver comentario en ciclo_consultas)
    if len(user_query.split()) > 4:
        user_query_efectiva = user_query
    else:
        user_query_efectiva = cli.reescribir_consulta(historial[-1:], user_query)
    if user_query_efectiva != user_query:
        bloques.append(Bloque("debug", f"  #--DEBUG Consulta reescrita: {user_query_efectiva}"))

    _t = time.perf_counter()
    normalized = cli.transformar_consulta_con_llm(user_query_efectiva)
    q.latencia_normalizacion_ms = int((time.perf_counter() - _t) * 1000)
    q.query_normalizado = normalized

    bloques.append(Bloque("debug", f"  #--DEBUG Pregunta Formateada: {normalized}"))

    _t = time.perf_counter()
    use_case_entry, score = cli.detectar_caso_de_uso(normalized, catalogos)
    q.latencia_deteccion_ms = int((time.perf_counter() - _t) * 1000)
    q.caso_score = score
    q.caso_exitoso = use_case_entry is not None

    if use_case_entry is None:
        bloques.append(Bloque(
            "no_flujo",
            "\n  No fue posible identificar un flujo asociado a su solicitud. Puede reformular su mensaje para intentar nuevamente el proceso automatizado, o escalar su caso a un agente humano para obtener asistencia personalizada.\n\n  Por favor reformule su consulta con más detalle.",
        ))
        q.reiniciar_timer()  # excluir el tiempo de espera del usuario al decidir si reintenta
        session["state"] = "RETRY"
        bloques.append(Bloque("retry_prompt", _PROMPT_RETRY))
        return FlowResult(bloques=bloques, botones=_BOTONES_SN)

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

    # Réplica de main.py: el CLI ya solo muestra el flujo identificado — los
    # detalles de entidades y consultas paralelas se omiten de la interfaz,
    # pero se conservan en flujo_data para debug/tracking.
    flujo_lineas = [
        "",
        f"  Tu pregunta se ha identificado mediante el flujo: {nombre_caso} con un nivel de coincidencia del {score:.2f}%.",
    ]
    flujo_data: dict = {"nombre": nombre_caso, "score": score, "entidades": [], "multiple": None}

    if tipo_pregunta == "multiple":
        catalogo_actual = cli.USE_CASES["options"].get(use_case_entry["catalogo"], {})
        preguntas_map_actual = {p["id"]: p for p in catalogo_actual.get("preguntas", [])}
        invoca_nombres = [
            preguntas_map_actual[i]["texto"]
            for i in pregunta.get("invoca", [])
            if i in preguntas_map_actual
        ]
        # Extraer entidades por sub-caso (usa la query ORIGINAL) — solo para data
        sub_pqs = [preguntas_map_actual[i] for i in pregunta.get("invoca", []) if i in preguntas_map_actual]
        entidades_por_subcaso: dict = {}
        for sp in sub_pqs:
            if sp.get("tipo") == "sql" and sp.get("parametros"):
                entidades_por_subcaso[sp["texto"]] = cli.extraer_entidades(
                    user_query, sp["parametros"], filtro_fijo_key
                )
        subcasos_data = [
            {"nombre": nombre_sp, "entidades": _entidades_data(ents)}
            for nombre_sp, ents in entidades_por_subcaso.items()
        ]
        flujo_data["multiple"] = {"invoca_nombres": invoca_nombres, "subcasos": subcasos_data}
        # Fusión de entidades desactivada — réplica del bloque comentado en main.py:
        # entidades_previas queda vacío y ejecutar_multiple re-extrae internamente.
    elif entidades_previas:
        flujo_data["entidades"] = _entidades_data(entidades_previas)

    bloques.append(Bloque("flujo", "\n".join(flujo_lineas), data=flujo_data))

    q.tipo_ejecucion = tipo_pregunta

    # ── Validación de filtros requeridos + confirmación ───────────────────────
    filtros_req_pre = pregunta.get("filtros_requeridos", [])
    if filtros_req_pre and tipo_pregunta != "rag" and not all(p in entidades_previas for p in filtros_req_pre):
        labels_req_pre = [cli.PARAM_TO_FILTRO[p][1] for p in filtros_req_pre if p in cli.PARAM_TO_FILTRO]
        lbl_slash_pre = "/".join(labels_req_pre)
        bloques.append(Bloque(
            "filtros_requeridos",
            f"\n  Esta consulta requiere identificar los siguientes filtros/entidades ({lbl_slash_pre}),"
            "\n  por favor reformule su pregunta considerando dichos filtros"
            "\n\n  Su consulta: ",
            data={"labels": lbl_slash_pre},
        ))
        session["state"] = "REFORMULAR"
        return FlowResult(bloques=bloques)

    session["state"] = "CONFIRM"
    session["pendiente"] = {
        "catalogo": use_case_entry.get("catalogo"),
        "pregunta": pregunta,
        "nombre_caso": nombre_caso,
        "tipo_pregunta": tipo_pregunta,
        "user_query": user_query,
        "entidades": _entidades_a_json(entidades_previas),
    }
    bloques.append(Bloque("confirmar_prompt", "\n" + _PROMPT_CONFIRMAR))
    return FlowResult(bloques=bloques, botones=_BOTONES_CONFIRMAR)


def _handle_confirm(session_key: str, session: dict, texto: str) -> FlowResult:
    respuesta = texto.strip().upper()
    if respuesta == OMITIR_SENTINEL.upper() or respuesta == "OMITIR":
        respuesta = ""

    if respuesta not in ("S", "N", ""):
        bloques = [
            Bloque("invalida", "  ⚠  Opción no válida. Por favor ingrese: S / N / Enter.\n"),
            Bloque("confirmar_prompt", _PROMPT_CONFIRMAR),
        ]
        return FlowResult(bloques=bloques, botones=_BOTONES_CONFIRMAR)

    logger = _get_logger(session_key)

    if respuesta == "N":
        # VoverError → "Volviendo al menú principal..."
        logger.cerrar_consulta()
        session.pop("pendiente", None)
        session["state"] = "QUERY"
        bloques = [
            Bloque("volver", "\n  Volviendo al menú principal...\n"),
            _bloque_menu_consulta(),
        ]
        return FlowResult(bloques=bloques)

    return _ejecutar_pendiente(session_key, session, confirmar=respuesta)


def _ejecutar_pendiente(session_key: str, session: dict, confirmar: str) -> FlowResult:
    pendiente = session.pop("pendiente", None)
    session["state"] = "QUERY"
    if not pendiente:
        return FlowResult(bloques=[_bloque_menu_consulta()])

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

    bloques: list[Bloque] = []

    # Reiniciar timer: excluir el tiempo que el usuario tardó en confirmar
    q.reiniciar_timer()

    if tipo_pregunta == "multiple":
        bloques.append(Bloque("procesando", "\n  Procesando su consulta, por favor espere..."))
        salida, _ = _capturado(
            cli.ejecutar_multiple,
            pregunta, pendiente["catalogo"], sql_filtro, filtro_fijo_key, user_query,
            entidades_previas, tipo_key=tipo_key, agency_name=agency_name,
        )
        bloques.append(Bloque("multiple_exec", salida, data=_parse_resultado_multiple(salida)))
    elif tipo_pregunta == "rag":
        salida, _ = _capturado(
            cli.ejecutar_rag,
            pregunta, user_query, query_log=q, agency_name=agency_name, tipo_key=tipo_key,
        )
        bloques.append(Bloque("rag", salida, data=_parse_rag_salida(salida)))
    else:
        texto_usuario = user_query if confirmar == "S" else ""
        entidades = entidades_previas if confirmar == "S" else {}

        bloques.append(Bloque("procesando", "\n  Procesando su consulta, por favor espere..."))
        salida, respuesta = _capturado(
            cli.ejecutar_consulta,
            pregunta, sql_filtro, entidades, texto_usuario, query_log=q, tipo_key=tipo_key,
        )
        if salida:
            bloques.append(Bloque("sql_debug", salida))
        bloques.append(Bloque(
            "resultado",
            "\nRESULTADO:\n" + _SEP + "\n" + respuesta,
            data={"respuesta": respuesta},
        ))

    logger.cerrar_consulta()

    session["state"] = "OTRA"
    bloques.append(Bloque("otra_prompt", "\n" + _PROMPT_OTRA))
    return FlowResult(bloques=bloques, botones=_BOTONES_SN)


def _handle_retry(session_key: str, session: dict, texto: str) -> FlowResult:
    respuesta = texto.strip().upper()
    if respuesta not in ("S", "N"):
        bloques = [
            Bloque("invalida", "  ⚠  Opción no válida. Por favor ingrese: S / N.\n"),
            Bloque("retry_prompt", _PROMPT_RETRY),
        ]
        return FlowResult(bloques=bloques, botones=_BOTONES_SN)

    logger = _get_logger(session_key)
    logger.cerrar_consulta()

    if respuesta != "S":
        return FlowResult(
            bloques=[Bloque("despedida", "\nGracias por utilizar el Sistema de Consultas IA. ¡Hasta pronto!")],
            terminado=True,
        )

    session["state"] = "QUERY"
    return FlowResult(bloques=[_bloque_menu_consulta()])


def _handle_otra(session_key: str, session: dict, texto: str) -> FlowResult:
    respuesta = texto.strip().upper()
    if respuesta not in ("S", "N"):
        bloques = [
            Bloque("invalida", "  ⚠  Opción no válida. Por favor ingrese: S / N.\n"),
            Bloque("otra_prompt", _PROMPT_OTRA),
        ]
        return FlowResult(bloques=bloques, botones=_BOTONES_SN)

    if respuesta != "S":
        return FlowResult(
            bloques=[Bloque("despedida", "\nGracias por utilizar el Sistema de Consultas IA. ¡Hasta pronto!")],
            terminado=True,
        )

    session["state"] = "QUERY"
    return FlowResult(bloques=[Bloque("salto", ""), _bloque_menu_consulta()])
