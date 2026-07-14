"""
GuidedFlow — máquina de estados que replica el flujo guiado del CLI (main.py)
para canales conversacionales request/response (Google Chat).

main.py NO se modifica: este módulo importa sus funciones (clasificación de
caso de uso, interpretación de confirmación, extracción de entidades, ejecución
SQL/RAG/multiple, etc.) y replica únicamente la orquestación basada en
input()/print() como máquina de estados.

Motor de intención (alineado con el rediseño de `feat/function-calling`):
  - Agente 1 (`seleccionar_caso_de_uso_llm`): clasifica la consulta contra los
    casos de uso permitidos usando su descripción `usa_esto_cuando`, detecta
    preguntas meta-conversacionales y redacta el mensaje de confirmación.
  - Agente 2 (`_interpretar_confirmacion`): interpreta la respuesta libre del
    usuario (confirmó / corrigió con una aclaración / rechazó). Si no confirma,
    genera una `query_ajustada` y se vuelve a clasificar. No hay límite de
    reintentos: el usuario sale del loop confirmando o con un comando global.

Cada paso retorna un FlowResult compuesto de Bloques semánticos:
  - `texto` (CLI): copia EXACTA de lo que imprime el CLI — paridad verificable.
  - `kind` + `data`: información estructurada para que cada canal renderice
    a su manera (Google Chat usa adapters/chat_render.py).
La salida de las funciones de main.py que imprimen directamente
(ejecutar_consulta, ejecutar_rag, ejecutar_multiple, _mostrar_instrucciones,
_header) se captura tal cual via core/captura_stdout.py.

Estados:
    IDENT_TIPO   → espera opción de tipo de usuario (1/2)
    IDENT_VALOR  → espera nombre de agencia / NPN
    QUERY        → espera consulta en lenguaje natural
    CONFIRM      → espera respuesta libre de confirmación (Agente 2)
    REFORMULAR   → espera consulta reformulada (faltan filtros requeridos)
    OTRA         → espera respuesta libre a "¿Hay algo más...?"

Divergencias deliberadas (mínimas) respecto al CLI:
    - En CONFIRM se ofrece un botón rápido "✅ Sí, es correcto" además del
      texto libre; el botón envía un texto afirmativo que el Agente 2
      interpreta como confirmación.
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

# Ventana del historial conversacional. El rewriter del CLI consume las dos
# últimas entradas (pregunta + respuesta real); aquí se conservan más turnos
# por canal, pero al rewriter solo se le pasan las dos últimas.
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


_PROMPT_OTRA = "¿Hay algo más en lo que pueda ayudarte? "


def _bloque_confirmacion(mensaje: str, use_case_entry: dict | None) -> Bloque:
    """
    Bloque con el mensaje de confirmación que redactó el Agente 1. La
    confirmación es en lenguaje libre (sin botones): el usuario responde con
    texto y el Agente 2 lo interpreta.
    """
    return Bloque(
        "confirmar_prompt",
        "\n  " + mensaje + "\n\n  Su respuesta: ",
        data={"mensaje": mensaje, "tiene_caso": use_case_entry is not None},
    )


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
        pendiente = session.get("pendiente") or {}
        mensaje = pendiente.get("mensaje", "")
        return [_bloque_confirmacion(mensaje, pendiente.get("use_case_entry"))], None
    if estado == "OTRA":
        return [Bloque("otra_prompt", _PROMPT_OTRA)], None
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


def _historial_dicts(session: dict) -> list[dict]:
    """Historial conversacional como lista de {role, content} (user + assistant)."""
    return list(session.get("historial_reciente", []))


def _registrar_turno(session: dict, user_query: str, respuesta: str) -> None:
    """Añade el intercambio (pregunta + respuesta real) al historial conversacional."""
    historial = _historial_dicts(session)
    historial.append({"role": "user", "content": user_query})
    historial.append({"role": "assistant", "content": (respuesta or "")[:300]})
    session["historial_reciente"] = historial[-_MAX_HISTORIAL:]


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
    """
    Primera clasificación de un turno: el Agente 1 reescribe (usando el historial)
    y clasifica en una sola llamada, dejando la sesión en CONFIRM (o respondiendo
    directo si es una pregunta meta-conversacional).
    """
    logger = _get_logger(session_key)
    q = logger.nueva_consulta()
    q.query_original = user_query

    bloques: list[Bloque] = [Bloque("status", "\n  Analizando su consulta...")]

    # El Agente 1 fusiona reescritura + clasificación en una sola llamada. El
    # historial solo se pasa en la primera clasificación del turno; las
    # correcciones posteriores (loop de confirmación) ya son autosuficientes.
    historial = _historial_dicts(session)[-2:]
    return _proponer_caso(session_key, session, q, user_query, bloques, historial=historial)


def _proponer_caso(
    session_key: str,
    session: dict,
    q,
    query: str,
    bloques: list[Bloque] | None = None,
    historial: list[dict] | None = None,
) -> FlowResult:
    """
    Agente 1: en una sola llamada reescribe `query` (con `historial` si se pasa)
    y la clasifica, fija los campos de tracking y deja la sesión en CONFIRM con
    el mensaje de confirmación. Si detecta una pregunta meta-conversacional,
    responde directo y vuelve a QUERY. Devuelve además la `query_reescrita`, que
    se usa como consulta efectiva para la ejecución.
    """
    bloques = bloques or []

    _t = time.perf_counter()
    use_case_entry, mensaje_confirmacion, es_meta, query_reescrita = cli.seleccionar_caso_de_uso_llm(
        query, session["catalogos"], historial=historial
    )
    q.latencia_deteccion_ms = int((time.perf_counter() - _t) * 1000)
    q.query_reescrita = query_reescrita
    q.caso_exitoso = use_case_entry is not None
    q.confirmacion_mensaje = mensaje_confirmacion

    if use_case_entry is not None:
        q.caso_nombre = use_case_entry["nombre"]
        q.caso_catalogo = use_case_entry.get("catalogo")

    if es_meta:
        # Pregunta conversacional sobre el propio asistente: el mensaje ya es la
        # respuesta final, no hay nada que confirmar ni ejecutar. Se registra en
        # el historial y se vuelve a pedir una consulta (como el `continue` del CLI).
        _registrar_turno(session, query_reescrita, mensaje_confirmacion)
        _get_logger(session_key).cerrar_consulta()
        session["state"] = "QUERY"
        bloques.append(Bloque("meta", "\n  " + mensaje_confirmacion))
        bloques.append(_bloque_menu_consulta())
        return FlowResult(bloques=bloques)

    session["state"] = "CONFIRM"
    session["pendiente"] = {
        "use_case_entry": use_case_entry,
        "mensaje": mensaje_confirmacion,
        "user_query_efectiva": query_reescrita,
        "intentos": q.intentos_confirmacion,
    }
    bloques.append(_bloque_confirmacion(mensaje_confirmacion, use_case_entry))
    return FlowResult(bloques=bloques)


def _handle_confirm(session_key: str, session: dict, texto: str) -> FlowResult:
    """
    Agente 2: interpreta la respuesta libre del usuario al mensaje de
    confirmación. Si confirma un caso de uso, ejecuta; si no, usa la
    `query_ajustada` (o la respuesta libre) para reclasificar — sin límite de
    reintentos.
    """
    pendiente = session.get("pendiente")
    if not pendiente:
        session["state"] = "QUERY"
        return FlowResult(bloques=[_bloque_menu_consulta()])

    respuesta_usuario = texto.strip()
    if not respuesta_usuario:
        # Vuelve a mostrar la misma propuesta y pide respuesta de nuevo (como el CLI).
        use_case_entry = pendiente.get("use_case_entry")
        return FlowResult(
            bloques=[_bloque_confirmacion(pendiente["mensaje"], use_case_entry)],
        )

    logger = _get_logger(session_key)
    q = logger.current or logger.nueva_consulta()

    interpretacion = cli._interpretar_confirmacion(pendiente["mensaje"], respuesta_usuario)
    q.confirmado = interpretacion["confirmado"]
    q.intentos_confirmacion = pendiente.get("intentos", 0) + 1

    use_case_entry = pendiente.get("use_case_entry")
    if interpretacion["confirmado"] and use_case_entry is not None:
        return _ejecutar_pendiente(session_key, session)

    # No confirmó (o no había caso): reclasifica con el ajuste sugerido o, si no
    # hay, con su respuesta libre. Sin historial: la corrección es autosuficiente.
    query_clasificar = interpretacion["query_ajustada"] or respuesta_usuario
    return _proponer_caso(session_key, session, q, query_clasificar)


def _ejecutar_pendiente(session_key: str, session: dict) -> FlowResult:
    """
    Ejecuta el caso de uso confirmado: extrae entidades, valida filtros
    requeridos y ejecuta SQL/RAG/multiple. Réplica del cuerpo post-confirmación
    de ciclo_consultas.
    """
    pendiente = session.get("pendiente") or {}
    use_case_entry = pendiente.get("use_case_entry")
    if not use_case_entry:
        session["state"] = "QUERY"
        return FlowResult(bloques=[_bloque_menu_consulta()])

    logger = _get_logger(session_key)
    q = logger.current
    if q is None:
        q = logger.nueva_consulta()
        q.query_original = pendiente.get("user_query_efectiva", "")
        q.caso_nombre = use_case_entry["nombre"]
        q.caso_catalogo = use_case_entry.get("catalogo")

    pregunta = use_case_entry["pregunta"]
    tipo_pregunta = pregunta.get("tipo")
    user_query = pendiente.get("user_query_efectiva", "")

    sql_filtro = session.get("sql_filtro")
    filtro_fijo_key = session.get("filtro_fijo_key")
    tipo_key = session.get("tipo_key", "1")
    agency_name = session.get("agency_name")

    bloques: list[Bloque] = []

    # ── Pre-detección de entidades (réplica de ciclo_consultas, post-confirmación) ─
    entidades_previas: dict = {}
    if tipo_pregunta == "sql":
        _t = time.perf_counter()
        entidades_previas = cli.extraer_entidades(
            user_query, pregunta.get("parametros", []), filtro_fijo_key
        )
        q.latencia_entidades_ms = int((time.perf_counter() - _t) * 1000)
        q.entidades = [
            {"param": p, "label": v[2], "valor": v[0], "score": v[1]}
            for p, v in entidades_previas.items()
        ]
    elif tipo_pregunta == "multiple":
        catalogo_actual = cli.USE_CASES["options"].get(use_case_entry["catalogo"], {})
        preguntas_map_actual = {p["id"]: p for p in catalogo_actual.get("preguntas", [])}
        sub_pqs = [preguntas_map_actual[i] for i in pregunta.get("invoca", []) if i in preguntas_map_actual]
        entidades_por_subcaso: dict = {}
        for sp in sub_pqs:
            if sp.get("tipo") == "sql" and sp.get("parametros"):
                entidades_por_subcaso[sp["texto"]] = cli.extraer_entidades(
                    user_query, sp["parametros"], filtro_fijo_key
                )
        # Fusionar para ejecución (sin duplicados, el primero gana) — réplica de main.py
        for ents in entidades_por_subcaso.values():
            for param, val in ents.items():
                if param not in entidades_previas:
                    entidades_previas[param] = val
        if entidades_previas:
            q.entidades = [
                {"param": p, "label": v[2], "valor": v[0], "score": v[1]}
                for p, v in entidades_previas.items()
            ]

    q.tipo_ejecucion = tipo_pregunta

    # ── Validación de filtros requeridos ──────────────────────────────────────
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

    # Reiniciar timer: excluir el tiempo que el usuario tardó en confirmar
    q.reiniciar_timer()

    if tipo_pregunta == "multiple":
        bloques.append(Bloque("procesando", "\n  Procesando su consulta, por favor espere..."))
        salida, respuesta = _capturado(
            cli.ejecutar_multiple,
            pregunta, use_case_entry["catalogo"], sql_filtro, filtro_fijo_key, user_query,
            entidades_previas, tipo_key=tipo_key, agency_name=agency_name,
        )
        data_mult = _parse_resultado_multiple(salida)
        # ejecutar_multiple retorna el texto sintetizado; se prefiere sobre el
        # parseo de stdout (que depende del marcador "RESULTADO:").
        if isinstance(respuesta, str) and respuesta.strip():
            data_mult["respuesta"] = respuesta.strip()
        bloques.append(Bloque("multiple_exec", salida, data=data_mult))
        respuesta_mostrada = data_mult["respuesta"]
    elif tipo_pregunta == "rag":
        salida, respuesta = _capturado(
            cli.ejecutar_rag,
            pregunta, user_query, query_log=q, agency_name=agency_name, tipo_key=tipo_key,
        )
        data_rag = _parse_rag_salida(salida)
        # ejecutar_rag retorna el texto limpio de la respuesta; se prefiere sobre el
        # parseo de stdout, que dependía del marcador "RESPUESTA:" (comentado en main.py).
        # El parseo de stdout se conserva solo para extraer documentos y carrier.
        if isinstance(respuesta, str) and respuesta.strip():
            data_rag["respuesta"] = respuesta.strip()
        bloques.append(Bloque("rag", salida, data=data_rag))
        respuesta_mostrada = data_rag["respuesta"]
    else:
        bloques.append(Bloque("procesando", "\n  Procesando su consulta, por favor espere..."))
        salida, respuesta = _capturado(
            cli.ejecutar_consulta,
            pregunta, sql_filtro, entidades_previas, user_query, query_log=q, tipo_key=tipo_key,
        )
        if salida:
            bloques.append(Bloque("sql_debug", salida))
        bloques.append(Bloque(
            "resultado",
            "\nRESULTADO:\n" + _SEP + "\n" + respuesta,
            data={"respuesta": respuesta},
        ))
        respuesta_mostrada = respuesta

    # Historial conversacional: incluye la respuesta real del asistente.
    _registrar_turno(session, user_query, respuesta_mostrada or "")

    session.pop("pendiente", None)
    logger.cerrar_consulta()

    session["state"] = "OTRA"
    bloques.append(Bloque("otra_prompt", "\n" + _PROMPT_OTRA))
    return FlowResult(bloques=bloques)


def _handle_otra(session_key: str, session: dict, texto: str) -> FlowResult:
    """
    "¿Hay algo más...?" en lenguaje libre: si el usuario no quiere nada más
    (vacío o _PALABRAS_NO) cierra; si escribe otra consulta, se encadena
    directo como nueva consulta (réplica de _next_query en el CLI).
    """
    if not texto or texto.lower() in cli._PALABRAS_NO:
        return FlowResult(
            bloques=[Bloque("despedida", "\nGracias por utilizar el Sistema de Consultas IA. ¡Hasta pronto!")],
            terminado=True,
        )

    session["state"] = "QUERY"
    return _pipeline_consulta(session_key, session, texto)
