import sys
import time
import json
import hashlib
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import streamlit as st
import streamlit.components.v1 as _components

from main import (
    TIPOS_USUARIO,
    FILTROS_VALIDOS,
    FILTROS_EMBEDDINGS,
    USE_CASES,
    PARAM_TO_FILTRO,
    _buscar_semantico,
    reescribir_consulta,
    seleccionar_caso_de_uso_llm,
    _interpretar_confirmacion,
    extraer_entidades,
)
from app.engine import sql_response, rag_response, multiple_response
from config import client
from config.logger import SessionLogger

st.set_page_config(
    page_title="Claro Insurance Omnichannel IA",
    page_icon="🤖",
    layout="centered",
)

# ── Keywords especiales (espejo de main.py) ────────────────────────────────────
_KW_SALIR        = {"salir", "exit", "q", "quit"}
_KW_VOLVER       = {"volver", "back"}
_KW_MENU         = {"nueva sesión", "nueva sesion"}
_KW_INSTRUCCIONES = {"instrucciones", "help", "ayuda", "guia", "guía"}
_KW_ESCALAR      = {"escalar", "escalar a un humano"}
_KW_SALUDO       = {"hola", "hola!", "hey", "buenas", "buenas tardes", "buenos días", "buenos dias", "buenas noches"}

_CATALOG_INFO = {
    "A": {
        "nombre": "Contratos",
        "emoji": "📋",
        "temas": [
            "Cantidad y detalle de contratos activos, pendientes e inactivos",
            "Detalle general de contratos (activos + pendientes combinados)",
            "Status del contrato en el sistema `(*)`",
            "Motivo del retraso en la aprobación `(*)`",
            "Instructivos de gestión (ACA · Medicare · Life · Supplementary)",
            "Licencias",
            "Oferta de productos disponibles por carrier y estado",
            "Validación para generación de contrato `(*)`",
        ],
    },
    "B": {
        "nombre": "Pagos y Comisiones",
        "emoji": "💰",
        "temas": [
            "Comisiones pagadas",
            "Comisiones bloqueadas",
            "Detalle de comisiones `(*)`",
            "Pre-liquidación de comisiones",
            "Detalle de pre-liquidación `(*)`",
            "Frecuencia de liquidación por carrier",
            "Calendario de pago de comisiones a agentes",
            "Estado del contrato y agente por póliza `(*)`",
            "Razones de pago pendiente o bloqueado `(*)`",
            "Detalle de reconciliaciones por póliza `(*)`",
            "Guía de reconciliaciones de comisiones",
        ],
    },
    "C": {
        "nombre": "Documentos Normativos",
        "emoji": "📄",
        "temas": [
            "Plazos estándar de envío de contratos",
            "Horarios y roles del equipo",
            "ARC Off-Exchanges FAQ",
            "Claro Insurance FAQ",
        ],
    },
}


def _build_instrucciones(catalogos: list | None = None) -> str:
    secciones = []
    for key, info in _CATALOG_INFO.items():
        if catalogos is None or key in catalogos:
            temas_md = "\n".join(f"  - {t}" for t in info["temas"])
            secciones.append(f"**{info['emoji']} {info['nombre']}**\n{temas_md}")

    temas_bloque = "\n\n".join(secciones) if secciones else "_Sin catálogos disponibles._"

    return f"""**FLUJO GENERAL DEL SISTEMA**

**1. Identificación**
Al ingresar seleccioná tu tipo de usuario:
- **Representante de Agencia** — consultas filtradas a tu agencia.
- **Agente NPN** — consultas filtradas a tus propios datos.
- **Management** — acceso sin restricción de filtro.

**2. Consulta en lenguaje natural**
Escribí tu pregunta como si le hablaras a una persona. El sistema:
- Detecta automáticamente el tema solicitado.
- Extrae filtros mencionados en tu consulta.
- Te pide confirmación antes de ejecutar.
- Devuelve el resultado con una explicación.

**Módulos disponibles para tu perfil**

{temas_bloque}

`(*)` Estas consultas requieren al menos un filtro obligatorio.

**Filtros reconocidos:** Carrier · Estado · Línea de negocio · Agencia · NPN · Ejecutivo · Fecha de pago · Mes de comisión · Número de póliza · Estado de pago · Tipo de pago

**Comandos disponibles:**
| Comando | Acción |
|---|---|
| `instrucciones` / `help` / `ayuda` | Muestra esta guía |
| `nueva sesión` | Limpia el historial y empieza de cero |
| `escalar a un humano` | Solicita asistencia de un agente |
| `salir` / `exit` | Cierra la sesión |
"""


# ── Estado inicial ─────────────────────────────────────────────────────────────
_CATALOG_PERMS_PATH = _ROOT / "data" / "catalog_permissions.json"
_CATALOG_PERMS: dict = {}
try:
    _CATALOG_PERMS = json.loads(_CATALOG_PERMS_PATH.read_text(encoding="utf-8"))
except Exception:
    _CATALOG_PERMS = {}


def _init_state():
    defaults = {
        "authenticated": False,
        "tipo_key": None,
        "tipo_nombre": None,
        "filtro_fijo_key": None,
        "sql_filtro": None,
        "catalogos": [],
        "usuario_valor": None,
        "agency_name": None,
        "messages": [],
        "pending": None,
        "debug_info": {},
        "session_logger": None,
        "app_authenticated": False,
        "app_username": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_state()


def _logout():
    st.session_state.clear()
    st.rerun()


# ── Ejecutar el caso de uso pendiente ─────────────────────────────────────────
def _ejecutar_pending(pending: dict, query_log=None) -> tuple[str, dict]:
    """Retorna (respuesta, debug_extra) donde debug_extra puede contener sql, chunks, sqls."""
    use_case_entry = pending["use_case_entry"]
    pregunta = use_case_entry["pregunta"]
    tipo = pregunta.get("tipo")
    debug_extra: dict = {}
    tipo_key = st.session_state.tipo_key
    agency_name = st.session_state.agency_name

    if tipo == "rag":
        texto = rag_response(
            pregunta, pending["user_query"], query_log=query_log, debug_out=debug_extra,
            agency_name=agency_name, tipo_key=tipo_key,
        )
        return texto, debug_extra
    elif tipo == "multiple":
        texto = multiple_response(
            pregunta,
            use_case_entry["catalogo"],
            st.session_state.sql_filtro,
            st.session_state.filtro_fijo_key,
            pending["user_query"],
            pending["entidades"],
            debug_out=debug_extra,
            tipo_key=tipo_key,
            agency_name=agency_name,
        )
        return texto, debug_extra
    else:
        texto, sql = sql_response(
            pregunta,
            st.session_state.sql_filtro,
            pending["entidades"],
            pending["user_query"],
            query_log=query_log,
            debug_out=debug_extra,
            tipo_key=tipo_key,
        )
        return texto, debug_extra


# ── Acceso a la aplicación (primer gate) ──────────────────────────────────────
_USERS_FILE = Path(__file__).resolve().parent / "users.json"


def _load_users() -> dict:
    if not _USERS_FILE.exists():
        return {}
    try:
        return json.loads(_USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def show_app_login():
    col_center = st.columns([1, 2, 1])[1]
    with col_center:
        st.markdown("<br><br>", unsafe_allow_html=True)
        st.markdown("## 🤖 Claro Insurance Omnichannel IA")
        st.markdown("Ingresá tus credenciales para acceder.")
        st.markdown("---")

        username = st.text_input("Usuario", placeholder="tu.nombre")
        password = st.text_input("Contraseña", type="password")

        if st.button("Ingresar", type="primary", use_container_width=True):
            users = _load_users()
            if not users:
                st.error("No hay usuarios configurados. Contactá al administrador.")
                return

            hashed = _hash(password)
            if username in users and users[username] == hashed:
                st.session_state.app_authenticated = True
                st.session_state.app_username = username
                st.rerun()
            else:
                st.error("Usuario o contraseña incorrectos.")


# ── Pantalla de login ──────────────────────────────────────────────────────────
def show_login():
    st.title("🤖 Sistema de Consultas Omnichannel IA")
    st.caption("Claro Insurance — Identificate para continuar")
    st.markdown("---")

    tipo_key = st.radio(
        "Seleccioná tu tipo de usuario:",
        options=list(TIPOS_USUARIO.keys()),
        format_func=lambda k: f"{k}. {TIPOS_USUARIO[k]['nombre']}",
    )
    tipo = TIPOS_USUARIO[tipo_key]

    st.markdown("")

    # Filtrar catálogos según catalog_permissions.json (por tipo de usuario)
    _perms = _CATALOG_PERMS.get(tipo_key)
    catalogos_disponibles = [c for c in tipo["catalogos"] if c in _perms] if _perms else tipo["catalogos"]

    if not catalogos_disponibles:
        st.error("Este tipo de usuario no tiene acceso a ningún catálogo. Contactá al administrador.")
        return

    if tipo["filtro_key"] is None:
        if st.button("Ingresar como Management", type="primary", use_container_width=True):
            logger = SessionLogger()
            logger.registrar_identificacion(tipo["nombre"], None, None)
            st.session_state.update({
                "authenticated": True,
                "tipo_key": tipo_key,
                "tipo_nombre": tipo["nombre"],
                "filtro_fijo_key": None,
                "sql_filtro": None,
                "catalogos": catalogos_disponibles,
                "usuario_valor": None,
                "session_logger": logger,
            })
            st.rerun()
    else:
        valor_input = st.text_input(
            tipo["prompt_id"],
            placeholder="Ingresá el valor para identificarte...",
        )
        if st.button("Identificar", type="primary", use_container_width=True):
            if not valor_input.strip():
                st.warning("El campo no puede estar vacío.")
                return

            candidatos = FILTROS_VALIDOS.get(tipo["filtro_key"], [])
            if not candidatos:
                st.error(f"Sin registros en filtro '{tipo['filtro_key']}'. Contactá al administrador.")
                return

            embs_pre = FILTROS_EMBEDDINGS.get(tipo["filtro_key"], {}).get("embeddings")

            with st.spinner("Verificando..."):
                match, score = _buscar_semantico(
                    valor_input.strip(), candidatos, tipo["umbral"], embs_pre
                )

            if match:
                valor_seguro = match.replace("'", "''")
                sql_filtro = tipo["sql_filtro"].replace("{valor}", valor_seguro) if tipo["sql_filtro"] else None

                # Resolver agencia del usuario (para filtros de RAG por agencia)
                agency_name = None
                if tipo_key == "1":
                    agency_name = match
                elif tipo_key == "2":
                    with st.spinner("Resolviendo agencia..."):
                        rows = list(client.query(f"""
                            SELECT a.Name_Agencies
                            FROM `claroinsurance-dataplatform.salesforce_claro.contact` c
                            LEFT JOIN `claroinsurance-dataplatform.claro_bi.dim_account_2` a ON a.Id = c.AccountId
                            WHERE c.NPN__c = '{valor_seguro}'
                            LIMIT 1
                        """).result())
                    agency_name = rows[0]["Name_Agencies"] if rows else None

                logger = SessionLogger()
                logger.registrar_identificacion(tipo["nombre"], match, score)
                st.session_state.update({
                    "authenticated": True,
                    "tipo_key": tipo_key,
                    "tipo_nombre": tipo["nombre"],
                    "filtro_fijo_key": tipo["filtro_key"],
                    "sql_filtro": sql_filtro,
                    "catalogos": catalogos_disponibles,
                    "usuario_valor": match,
                    "agency_name": agency_name,
                    "session_logger": logger,
                })
                st.rerun()
            else:
                st.error(
                    f"Sin coincidencia para **'{valor_input}'** "
                    f"(similitud: {score:.2f} — umbral requerido: {tipo['umbral']:.2f}). "
                    "Verificá el valor e intentá nuevamente."
                )


# ── Sidebar de debug ──────────────────────────────────────────────────────────
def _format_sql(sql: str) -> str:
    """Agrega saltos de línea antes de las cláusulas SQL principales para mejorar legibilidad."""
    import re
    sql = re.sub(r'\s+', ' ', sql.strip())
    breaks = [
        (r'(?i)\s+(LEFT\s+OUTER\s+JOIN)\s+', '\n  LEFT OUTER JOIN '),
        (r'(?i)\s+(LEFT\s+JOIN)\s+',         '\n  LEFT JOIN '),
        (r'(?i)\s+(RIGHT\s+JOIN)\s+',        '\n  RIGHT JOIN '),
        (r'(?i)\s+(INNER\s+JOIN)\s+',        '\n  INNER JOIN '),
        (r'(?i)\s+(FULL\s+JOIN)\s+',         '\n  FULL JOIN '),
        (r'(?i)\s+(JOIN)\s+',                '\n  JOIN '),
        (r'(?i)\s+(FROM)\s+',                '\nFROM '),
        (r'(?i)\s+(WHERE)\s+',               '\nWHERE '),
        (r'(?i)\s+(AND)\s+',                 '\n  AND '),
        (r'(?i)\s+(OR)\s+',                  '\n  OR '),
        (r'(?i)\s+(ON)\s+',                  '\n    ON '),
        (r'(?i)\s+(GROUP\s+BY)\s+',          '\nGROUP BY '),
        (r'(?i)\s+(ORDER\s+BY)\s+',          '\nORDER BY '),
        (r'(?i)\s+(HAVING)\s+',              '\nHAVING '),
        (r'(?i)\s+(LIMIT)\s+',               '\nLIMIT '),
    ]
    for pattern, replacement in breaks:
        sql = re.sub(pattern, replacement, sql)
    return sql.strip()


def _render_sidebar():
    with st.sidebar:
        st.markdown("### 🔍 Debug — Última consulta")
        st.markdown("---")

        d = st.session_state.debug_info
        if not d:
            st.caption("Aún no se procesó ninguna consulta.")
            return

        st.markdown("**📝 Query original**")
        st.code(d.get("query_original", "—"), language=None)

        reescrita = d.get("query_reescrita")
        if reescrita:
            st.markdown("**✏️ Query reescrita**")
            st.info(reescrita)

        st.markdown("**🤖 Mensaje del Agente 1 (selección de caso de uso)**")
        st.code(d.get("mensaje_confirmacion", "—"), language=None)

        flujo = d.get("flujo_nombre")
        catalogo = d.get("flujo_catalogo")
        if flujo:
            st.markdown("**🎯 Flujo detectado**")
            st.success(f"{flujo}")
            st.caption(f"Catálogo: {catalogo or '—'}")
        else:
            st.markdown("**🎯 Flujo detectado**")
            st.error("Sin coincidencia")

        st.markdown("**⚙️ Tipo de ejecución**")
        tipo = d.get("tipo_ejecucion", "—")
        tipo_color = {"sql": "🟦", "rag": "🟩", "multiple": "🟨"}.get(tipo, "⬜")
        st.markdown(f"{tipo_color} `{tipo}`")

        entidades = d.get("entidades", {})
        st.markdown("**🏷️ Entidades detectadas**")
        if entidades:
            for _, (val, sc, lbl, _) in entidades.items():
                st.markdown(f"- **{lbl}:** {val} `{sc:.2f}`")
        else:
            st.caption("Ninguna")

        # SQL individual
        sql = d.get("sql")
        if sql:
            st.markdown("---")
            st.markdown("**🗄️ SQL ejecutado**")
            st.code(_format_sql(sql), language="sql")

        # SQLs de flujos múltiples
        sqls = d.get("sqls", {})
        if sqls:
            st.markdown("---")
            st.markdown("**🗄️ SQLs ejecutados**")
            for nombre, sql_sub in sqls.items():
                with st.expander(nombre, expanded=False):
                    st.code(_format_sql(sql_sub), language="sql")

        # Chunks RAG
        chunks = d.get("chunks", [])
        if chunks:
            st.markdown("---")
            st.markdown(f"**📄 Chunks RAG recuperados ({len(chunks)})**")
            for i, chunk in enumerate(chunks, 1):
                with st.expander(f"Chunk {i}", expanded=False):
                    st.caption(chunk[:600] + "..." if len(chunk) > 600 else chunk)

        estado = d.get("estado", "")
        if estado:
            st.markdown("---")
            st.markdown(f"**Estado:** {estado}")


# ── Scroll al final del chat ──────────────────────────────────────────────────
def _scroll_to_bottom():
    st.components.v1.html(
        """
        <script>
        setTimeout(function() {
            try {
                var doc = window.parent.document;
                var selectors = [
                    'section.main',
                    '.main',
                    '[data-testid="stAppViewContainer"]',
                    '[data-testid="block-container"]',
                    '.block-container'
                ];
                var scrolled = false;
                for (var i = 0; i < selectors.length; i++) {
                    var el = doc.querySelector(selectors[i]);
                    if (el && el.scrollHeight > el.clientHeight) {
                        el.scrollTop = el.scrollHeight;
                        scrolled = true;
                        break;
                    }
                }
                if (!scrolled) {
                    window.parent.scrollTo(0, doc.body.scrollHeight);
                }
            } catch(e) {}
        }, 150);
        </script>
        """,
        height=0,
    )


# ── Pantalla de chat ───────────────────────────────────────────────────────────
def show_chat():
    _render_sidebar()
    _scroll_to_bottom()

    tipo_nombre = st.session_state.tipo_nombre
    usuario_valor = st.session_state.usuario_valor

    col_info, col_logout = st.columns([5, 1])
    with col_info:
        if usuario_valor:
            st.markdown(f"**{tipo_nombre}** — `{usuario_valor}`")
        else:
            st.markdown(f"**{tipo_nombre}**")
    with col_logout:
        if st.button("Salir"):
            _logout()

    st.markdown("---")

    # Historial de mensajes completados
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Scroll automático al último mensaje
    _components.html(
        "<script>window.parent.scrollTo(0, window.parent.document.body.scrollHeight);</script>",
        height=0,
        scrolling=False,
    )

    # Propuesta pendiente (si hay una, ya está en st.session_state.messages y se
    # mostró en el loop de arriba). La confirmación/corrección se escribe en el
    # mismo cuadro de texto de abajo, igual que "Su respuesta:" en main.py.
    pending = st.session_state.pending
    placeholder = "Tu respuesta..." if pending else "¿Qué desea consultar?"

    if user_query := st.chat_input(placeholder):
        q_lower = user_query.strip().lower()

        # ── Keywords especiales (interceptan siempre, incluso con una propuesta
        # pendiente — igual que _input() en main.py, que no altera el loop de
        # confirmación cuando el usuario saluda o pide instrucciones) ──────────
        if q_lower in _KW_SALIR or q_lower in _KW_VOLVER:
            _logout()
            return

        if q_lower in _KW_MENU:
            st.session_state.messages = []
            st.session_state.pending = None
            st.rerun()
            return

        if q_lower in _KW_SALUDO:
            st.session_state.messages.append({"role": "user", "content": user_query})
            st.session_state.messages.append({
                "role": "assistant",
                "content": (
                    "¡Hola! Soy tu asistente de Claro Insurance. "
                    "Puedo ayudarte con consultas sobre contratos, comisiones y documentos normativos.\n\n"
                    "Escribe **instrucciones** para ver todo lo que puedes consultar. ¡Quedo atento!"
                ),
            })
            st.rerun()
            return

        if q_lower in _KW_INSTRUCCIONES:
            st.session_state.messages.append({"role": "user", "content": user_query})
            st.session_state.messages.append({
                "role": "assistant",
                "content": _build_instrucciones(st.session_state.catalogos),
            })
            st.rerun()
            return

        if q_lower in _KW_ESCALAR:
            st.session_state.messages.append({"role": "user", "content": user_query})
            st.session_state.messages.append({
                "role": "assistant",
                "content": (
                    "Entendido. Un agente del equipo se comunicará contigo a la brevedad. "
                    "Mientras tanto podés seguir realizando otras consultas."
                ),
            })
            st.rerun()
            return
        # ──────────────────────────────────────────────────────────────────────

        if pending is None:
            _procesar_nueva_consulta(user_query)
        else:
            _procesar_respuesta_confirmacion(user_query, pending)


def _procesar_nueva_consulta(user_query: str) -> None:
    """Primer turno: reescritura + Agente 1. Si encuentra un caso de uso, queda
    'pending' a la espera de que el usuario confirme con texto libre."""
    catalogos = st.session_state.catalogos

    st.session_state.debug_info = {"query_original": user_query, "estado": "⏳ Analizando..."}

    with st.status("Analizando tu consulta...", expanded=True) as _status:
        _hist = [
            {"role": m["role"], "content": m["content"][:200].replace("\n", " ")}
            for m in st.session_state.messages[-4:]
        ]
        user_query_efectiva = reescribir_consulta(_hist, user_query)
        if user_query_efectiva != user_query:
            st.session_state.debug_info["query_reescrita"] = user_query_efectiva

        st.write("Identificando el caso de uso...")
        _t1 = time.perf_counter()
        use_case_entry, mensaje_confirmacion, es_meta = seleccionar_caso_de_uso_llm(user_query_efectiva, catalogos)
        st.session_state.debug_info["latencia_deteccion_ms"] = int((time.perf_counter() - _t1) * 1000)
        _status.update(label="Análisis completado", state="complete", expanded=False)

    st.session_state.messages.append({"role": "user", "content": user_query})
    st.session_state.debug_info["mensaje_confirmacion"] = mensaje_confirmacion

    if use_case_entry is None:
        st.session_state.debug_info.update({
            "flujo_nombre": None,
            "flujo_catalogo": None,
            "tipo_ejecucion": None,
            "entidades": {},
            "estado": "💬 Conversacional" if es_meta else "❌ Sin flujo detectado",
        })
        st.session_state.messages.append({"role": "assistant", "content": mensaje_confirmacion})
        # Si no es meta, seguimos negociando (igual que main.py: nunca se rinde,
        # la próxima respuesta del usuario se usa para reclasificar).
        st.session_state.pending = None if es_meta else {
            "user_query": user_query,
            "user_query_efectiva": user_query_efectiva,
            "use_case_entry": None,
            "mensaje_confirmacion": mensaje_confirmacion,
        }
        st.rerun()
        return

    st.session_state.debug_info.update({
        "flujo_nombre": use_case_entry["nombre"],
        "flujo_catalogo": use_case_entry.get("catalogo"),
        "estado": "⏳ Esperando confirmación",
    })
    st.session_state.pending = {
        "user_query": user_query,
        "user_query_efectiva": user_query_efectiva,
        "use_case_entry": use_case_entry,
        "mensaje_confirmacion": mensaje_confirmacion,
    }
    st.session_state.messages.append({"role": "assistant", "content": mensaje_confirmacion})
    st.rerun()


def _procesar_respuesta_confirmacion(respuesta_usuario: str, pending: dict) -> None:
    """Agente 2: interpreta la respuesta a la propuesta pendiente. Si confirma,
    ejecuta; si no, reclasifica con el ajuste (o la respuesta libre) y vuelve a
    proponer — sin límite de intentos, igual que el loop de main.py."""
    st.session_state.messages.append({"role": "user", "content": respuesta_usuario})

    mensaje_confirmacion = pending["mensaje_confirmacion"]
    use_case_entry = pending["use_case_entry"]

    with st.spinner("Procesando tu respuesta..."):
        interpretacion = _interpretar_confirmacion(mensaje_confirmacion, respuesta_usuario)

    if interpretacion["confirmado"] and use_case_entry is not None:
        _confirmar_y_ejecutar(pending)
        return

    nueva_query_clasificar = interpretacion["query_ajustada"] or respuesta_usuario
    catalogos = st.session_state.catalogos

    with st.spinner("Afinando tu consulta..."):
        nuevo_use_case_entry, nuevo_mensaje, es_meta = seleccionar_caso_de_uso_llm(nueva_query_clasificar, catalogos)

    if es_meta:
        st.session_state.debug_info.update({"estado": "💬 Conversacional"})
        st.session_state.messages.append({"role": "assistant", "content": nuevo_mensaje})
        st.session_state.pending = None
        st.rerun()
        return

    st.session_state.debug_info["mensaje_confirmacion"] = nuevo_mensaje
    st.session_state.debug_info.update({
        "flujo_nombre": nuevo_use_case_entry["nombre"] if nuevo_use_case_entry else None,
        "flujo_catalogo": nuevo_use_case_entry.get("catalogo") if nuevo_use_case_entry else None,
        "estado": "⏳ Esperando confirmación" if nuevo_use_case_entry else "❌ Sin flujo detectado",
    })

    st.session_state.pending = {
        **pending,
        "use_case_entry": nuevo_use_case_entry,
        "mensaje_confirmacion": nuevo_mensaje,
    }
    st.session_state.messages.append({"role": "assistant", "content": nuevo_mensaje})
    st.rerun()


def _confirmar_y_ejecutar(pending: dict) -> None:
    """Caso de uso confirmado: extrae entidades (una sola vez, con la query
    reescrita del primer turno), valida filtros obligatorios y ejecuta."""
    use_case_entry = pending["use_case_entry"]
    pregunta = use_case_entry["pregunta"]
    tipo_pregunta = pregunta.get("tipo")
    user_query = pending["user_query"]
    user_query_efectiva = pending["user_query_efectiva"]
    filtro_fijo_key = st.session_state.filtro_fijo_key

    entidades: dict = {}
    _t2 = time.perf_counter()
    if tipo_pregunta == "sql":
        entidades = extraer_entidades(user_query_efectiva, pregunta.get("parametros", []), filtro_fijo_key)
    elif tipo_pregunta == "multiple":
        catalogo_actual = USE_CASES["options"].get(use_case_entry["catalogo"], {})
        preguntas_map = {p["id"]: p for p in catalogo_actual.get("preguntas", [])}
        sub_pqs = [preguntas_map[i] for i in pregunta.get("invoca", []) if i in preguntas_map]
        for sp in sub_pqs:
            if sp.get("tipo") == "sql" and sp.get("parametros"):
                sub_ents = extraer_entidades(user_query, sp["parametros"], filtro_fijo_key)
                for param, val in sub_ents.items():
                    if param not in entidades:
                        entidades[param] = val
    st.session_state.debug_info["latencia_entidades_ms"] = int((time.perf_counter() - _t2) * 1000)
    st.session_state.debug_info.update({
        "flujo_nombre": use_case_entry["nombre"],
        "flujo_catalogo": use_case_entry.get("catalogo"),
        "tipo_ejecucion": tipo_pregunta,
        "entidades": entidades,
    })

    # Validar filtros requeridos
    filtros_req = pregunta.get("filtros_requeridos", [])
    if filtros_req and tipo_pregunta != "rag" and not all(p in entidades for p in filtros_req):
        labels_req = [PARAM_TO_FILTRO[p][1] for p in filtros_req if p in PARAM_TO_FILTRO]
        st.session_state.messages.append({
            "role": "assistant",
            "content": (
                f"Esta consulta requiere que especifiques: **{' / '.join(labels_req)}**. "
                "Por favor escribí una nueva consulta incluyendo esa información."
            ),
        })
        st.session_state.pending = None
        st.rerun()
        return

    nombre_caso = use_case_entry["nombre"]
    mensaje_confirmacion = pending["mensaje_confirmacion"]

    logger = st.session_state.session_logger
    q = logger.nueva_consulta() if logger else None

    if q:
        d = st.session_state.debug_info
        q.query_original            = user_query
        q.query_reescrita           = d.get("query_reescrita", "")
        q.caso_nombre               = nombre_caso
        q.caso_catalogo             = use_case_entry.get("catalogo")
        q.caso_exitoso              = True
        q.confirmacion_mensaje      = mensaje_confirmacion
        q.confirmado                = True
        q.tipo_ejecucion            = tipo_pregunta
        q.latencia_deteccion_ms     = d.get("latencia_deteccion_ms")
        q.latencia_entidades_ms     = d.get("latencia_entidades_ms")
        q.entidades                 = [
            {"param": p, "label": v[2], "valor": v[0], "score": v[1]}
            for p, v in entidades.items()
        ]

    st.session_state.debug_info["estado"] = "⚙️ Ejecutando..."
    tipo_label = {"sql": "Consultando base de datos...", "rag": "Buscando en documentos...", "multiple": "Ejecutando consultas en paralelo..."}.get(tipo_pregunta, "Procesando...")
    with st.status(tipo_label, expanded=True) as _status:
        st.write("Generando respuesta con IA...")
        respuesta, exec_debug = _ejecutar_pending(
            {"use_case_entry": use_case_entry, "entidades": entidades, "user_query": user_query},
            query_log=q,
        )
        st.session_state.debug_info.update(exec_debug)
        _status.update(label="Listo", state="complete", expanded=False)

    if q:
        logger.cerrar_consulta()

    st.session_state.debug_info["estado"] = "✅ Completado"
    st.session_state.messages.append({
        "role": "assistant",
        "content": f"**{nombre_caso}**\n\n{respuesta}",
    })
    st.session_state.pending = None
    st.rerun()


# ── Entry point ────────────────────────────────────────────────────────────────
if not st.session_state.app_authenticated:
    show_app_login()
elif not st.session_state.authenticated:
    show_login()
else:
    show_chat()
