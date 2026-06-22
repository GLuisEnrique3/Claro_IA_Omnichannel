# Claro Insurance — Omnichannel IA

Sistema de consulta inteligente en lenguaje natural para datos operativos de Claro Insurance. Permite a representantes de agencias, agentes NPN y personal de gerencia consultar contratos, comisiones, pagos y documentos normativos en español, usando modelos de lenguaje generativo y búsqueda semántica.

---

## Arquitectura general

```
Usuario (texto en español)
        │
        ▼
  Reescritura con contexto         ← Gemini Flash Lite (usa el turno anterior: pregunta + respuesta real)
        │
        ▼
  ┌─────────────────────────────────────────────────────────┐
  │  Agente 1 — Identificación de intención                  │
  │  ← Gemini Flash Lite, clasifica contra "usa_esto_cuando"  │
  │    de cada caso de uso permitido, o detecta que es una     │
  │    pregunta conversacional/meta sobre el propio asistente   │
  │                                                             │
  │  Agente 2 — Confirmación                                   │
  │  ← Gemini Flash Lite interpreta la respuesta del usuario:   │
  │    confirmó / corrigió / rechazó. Sin límite de reintentos  │
  │    — sigue afinando hasta que el usuario confirme o use un  │
  │    comando global (salir, volver, nueva sesión)              │
  └─────────────────────────────────────────────────────────┘
        │
        ▼ (una vez confirmado)
  Extracción de entidades          ← N-gramas + embeddings precalculados + regex (fechas, NPN, pólizas, IDs)
        │
        ├──── SQL ──────────────→ Gemini Pro construye el SQL → BigQuery → Gemini Flash Lite redacta la respuesta
        ├──── RAG ──────────────→ ChromaDB (top 5 docs) → Gemini Flash Lite redacta la respuesta
        └──── MULTIPLE ─────────→ Ejecución paralela de varios sub-casos (SQL+RAG) → síntesis con Gemini Flash Lite
        │
        ▼
  Log asíncrono (JSONL + BigQuery)
```

> Si la consulta es conversacional/meta ("quién eres", "qué puedes hacer", "qué temas
> puedo consultar"), el Agente 1 responde directo usando un recurso de presentación
> real (los temas disponibles según el catálogo del usuario), sin pasar por SQL/RAG ni
> por el loop de confirmación.

### Componentes principales

| Componente | Tecnología | Rol |
|---|---|---|
| LLM conversacional | Gemini 2.5 Flash Lite (Vertex AI) | Reescritura con contexto, Agente 1 (clasificación + respuestas meta), Agente 2 (interpretación de confirmación), síntesis de respuestas |
| LLM SQL | Gemini 2.5 Pro (Vertex AI) | Generación de SQL para BigQuery |
| Base de datos | Google BigQuery | Fuente de datos operativos |
| Vector store | ChromaDB (local persistente) | RAG sobre documentos normativos |
| Embeddings | `all-MiniLM-L6-v2` (SentenceTransformers) | Identificación de usuario (agencia/NPN) y extracción de entidades (carrier, estado, etc.) por similitud coseno — **no** se usa para detectar el caso de uso, eso lo hace el LLM directamente |
| Logs | JSONL diario + tabla BigQuery | Trazabilidad y monitoreo |

### Interfaces disponibles

| Interfaz | Cómo correrla | Notas |
|---|---|---|
| CLI (consola) | `python main.py` | Identificación y consultas por texto plano en la terminal. |
| Web (Streamlit) | `streamlit run app/app_streamlit.py` | Login propio (`app/users.json`) + identificación de usuario, chat con historial visual, sidebar de debug (query reescrita, SQL ejecutado, chunks RAG, latencias). Usa el mismo flujo conversacional que el CLI (texto libre para confirmar, sin botones, sin límite de reintentos). |

Ambas interfaces reutilizan las mismas funciones de `main.py` (clasificación, extracción de entidades, construcción de SQL, RAG). `app/engine.py` reimplementa las versiones de ejecución SQL/RAG/multiple para devolver texto en vez de imprimir por consola.

---

## Estructura del proyecto

```
Omnichannel - Chroma/
├── main.py                        # Núcleo de la aplicación (CLI + lógica compartida)
├── .env                           # Variables de entorno (no se sube al repo)
├── env_example.txt                # Plantilla de variables de entorno
│
├── app/
│   ├── app_streamlit.py           # Interfaz web (chat, login, sidebar de debug)
│   ├── engine.py                  # Versiones de ejecución SQL/RAG/multiple que devuelven texto
│   ├── users.json                 # Credenciales de acceso a la app web
│   └── requirements.txt           # Dependencias específicas de la app web
│
├── config/
│   ├── __init__.py                # Inicialización de Vertex AI y BigQuery
│   ├── chroma_client.py           # Wrapper de ChromaDB
│   └── logger.py                  # Sistema de logging (JSONL + BigQuery)
│
├── data/
│   ├── use_cases.json             # Definición de todos los casos de uso
│   ├── catalog_permissions.json   # Permisos por tipo de usuario
│   ├── filtros_validos.pkl        # Valores de filtros cacheados (de BigQuery)
│   └── filtros_embeddings.pkl     # Embeddings precalculados de filtros
│
├── scripts/
│   ├── precalcular_filtros.py     # Recarga filtros desde BigQuery y los cachea
│   ├── precalcular_embeddings.py  # Indexa PDFs en ChromaDB
│   ├── precalcular_use_cases.py   # (legado, no usado por main.py actualmente — ver nota abajo)
│   ├── manage_users.py            # Gestión de usuarios del sistema
│   └── delete_collections.py      # Limpieza de colecciones en ChromaDB
│
├── test/
│   ├── revisar_filtros_validos.py        # Verifica filtros cacheados
│   ├── revisar_use_cases_embeddings.py   # (legado, ver nota abajo)
│   └── revisar_embeddings.py             # Verifica embeddings de documentos
│
├── pdf/                            # Documentos fuente para RAG (PDFs)
├── words/                          # Documentos fuente para RAG (.docx, instructivos por carrier)
├── chroma_db/                      # Almacenamiento persistente de ChromaDB
├── logs/                           # Logs diarios en formato JSONL
├── docs/                           # Documentación adicional del proyecto
└── notes/                          # Notas internas del proyecto
```

> **Nota — `precalcular_use_cases.py` / `revisar_use_cases_embeddings.py`:** quedaron de
> una versión anterior en la que la detección de caso de uso se hacía por similitud
> coseno contra `semantic_examples`. Hoy esa detección la hace el Agente 1 (LLM) leyendo
> directamente el campo `usa_esto_cuando` de cada caso en `use_cases.json` — estos dos
> scripts no se ejecutan en el flujo actual.

---

## Configuración inicial

### 1. Requisitos previos

- Python 3.10+
- Acceso a Google Cloud Platform con dos cuentas de servicio:
  - Una para **Vertex AI** (modelos Gemini)
  - Una para **BigQuery** (datos operativos)
- Archivos `.json` de credenciales ubicados en `./service/`

### 2. Instalar dependencias

```bash
pip install python-dotenv google-cloud-aiplatform google-auth \
    google-cloud-bigquery chromadb sentence-transformers \
    pypdf numpy python-docx
```

### 3. Configurar variables de entorno

Copia `env_example.txt` como `.env` en la raíz del proyecto y completa los valores:

```env
VERTEX_CREDENTIALS_JSON=./service/claro-gemini-key.json
VERTEX_PROJECT_ID=claroinsurance-dataplatform
VERTEX_LOCATION=us-central1
BQ_CREDENTIALS_JSON=./service/clouddemo-service-account.json
CHROMA_PATH=./chroma_db
```

---

## Precálculo de datos (pasos obligatorios antes del primer uso)

Estos scripts deben ejecutarse **una vez** al iniciar el proyecto y **cada vez que cambien los datos fuente**.

### Paso 1 — Cargar filtros desde BigQuery

Descarga los valores válidos de filtros (carriers, estados, agencias, NPNs, etc.) y los cachea localmente con sus embeddings.

```bash
python scripts/precalcular_filtros.py
```

Genera:
- `data/filtros_validos.pkl`
- `data/filtros_embeddings.pkl`

### Paso 2 — Indexar documentos normativos en ChromaDB

Lee los PDFs/`.docx` de `./pdf/` y `./words/`, los divide en chunks y los almacena en ChromaDB con sus embeddings.

```bash
python scripts/precalcular_embeddings.py
```

Genera colecciones en `./chroma_db/` para búsqueda semántica (RAG).

> `scripts/precalcular_use_cases.py` **no** es necesario ejecutarlo — quedó de una
> versión anterior donde la detección de caso de uso se hacía por embeddings contra
> `semantic_examples`. Hoy esa detección la hace el Agente 1 (LLM) leyendo directamente
> `usa_esto_cuando` en `data/use_cases.json`, sin precálculo previo.

---

## Ejecución

Con los precálculos completos, podés iniciar cualquiera de las dos interfaces:

**Consola (CLI):**
```bash
python main.py
```

**Web (Streamlit):**
```bash
streamlit run app/app_streamlit.py
```
La app web pide primero credenciales propias (`app/users.json`, gestionable con
`scripts/manage_users.py`) y luego la identificación de usuario de negocio (agencia /
NPN / management), igual que el CLI.

En ambos casos, tras identificarte el sistema entra en el ciclo de consultas: escribís
tu pregunta en lenguaje natural, el Agente 1 propone un caso de uso, y confirmás o
corregís con texto libre hasta que se ejecute.

---

## Lógica interna detallada

### Identificación de usuario

El sistema reconoce tres tipos de usuario:

| Tipo | Descripción | Catálogos disponibles |
|---|---|---|
| 1 | Representante de Agencia | A, B, C |
| 2 | Agente NPN | A, B, C |
| 3 | Gerencia | A, B, C |

La identificación usa matching semántico (embeddings + umbral de confianza configurable entre 0.65 y 0.95 según el tipo).

### Pipeline de consulta

1. **Reescritura con contexto**: si la consulta depende del turno anterior (ej. "¿y por carrier?"), el LLM la reescribe incorporando lo necesario, usando como contexto la pregunta y la **respuesta real** del turno anterior (no solo la pregunta). Si la consulta ya es autosuficiente, se devuelve intacta.

2. **Agente 1 — Identificación de caso de uso**: el LLM recibe la lista de casos de uso permitidos (según el tipo de usuario) junto con su descripción `usa_esto_cuando`, y elige el que mejor calce — sin embeddings ni umbral numérico, es una decisión del modelo. Si la consulta es conversacional/meta sobre el propio asistente ("quién eres", "qué puedes hacer", "qué temas puedo consultar"), responde directo citando los temas reales disponibles para ese usuario, sin entrar al flujo de negocio.

3. **Agente 2 — Confirmación**: el LLM interpreta la respuesta del usuario al mensaje propuesto por el Agente 1 (confirmó / corrigió con una aclaración / rechazó sin más). Si no confirma, se usa su corrección (o su respuesta libre) para volver a clasificar con el Agente 1. **No hay límite de reintentos**: el ciclo sigue hasta que el usuario confirme explícitamente, o use un comando global (`salir`, `volver`, `nueva sesión`).

4. **Extracción de entidades** (una vez confirmado el caso de uso): descompone la consulta en N-gramas (1 a 4 palabras) y los compara contra los embeddings de los valores válidos de cada filtro. Cada tipo de filtro tiene su propio umbral de similitud (`PARAM_TO_FILTRO` en `main.py`). Casos especiales:
   - **Fechas**: parser de expresiones en español ("el mes pasado", "mayo 2024", "hace 2 meses")
   - **Commission Month**: igual que fechas, pero solo se activa si el usuario menciona explícitamente "commission month" o "mes de comisión"
   - **NPN**: regex explícita ("NPN 1234567") con fallback a embeddings
   - **Número de póliza** / **ID de oportunidad**: regex después de la palabra clave correspondiente

5. **Validación de filtros obligatorios**: si el caso de uso tiene `filtros_requeridos` y no se detectó ninguno, se le pide al usuario que reformule con ese dato — sin ejecutar nada.

6. **Ejecución**:
   - `sql`: Gemini Pro genera una query BigQuery a partir de la plantilla `sql_by_role` (una por tipo de usuario) definida en `use_cases.json`, inyectando el filtro fijo del usuario y los filtros dinámicos detectados. Si BigQuery falla, reintenta hasta 2 veces, pasándole el error al modelo para que corrija el SQL. Solo se permiten consultas `SELECT` — cualquier intento de `INSERT`, `UPDATE`, `DELETE`, `DROP`, etc. es bloqueado por regex antes de ejecutarse.
   - `rag`: codifica la consulta y busca en ChromaDB los 5 fragmentos más relevantes (3 en flujos `multiple`). Gemini Flash Lite redacta la respuesta basándose únicamente en esos fragmentos.
   - `multiple`: ejecuta varios sub-casos (SQL y/o RAG) en paralelo (`ThreadPoolExecutor`) y fusiona los resultados en una sola respuesta con Gemini Flash Lite.

### Sistema de logging

Cada consulta genera un registro JSON con:

- Query original y reescrita
- Caso de uso detectado, catálogo, y si fue exitoso
- Mensaje de confirmación, si el usuario confirmó, e intentos de confirmación
- Entidades extraídas (parámetro, valor, score de similitud)
- SQL generada e intentos realizados
- Filas devueltas por BigQuery
- Latencias individuales (detección, entidades, SQL, BQ, RAG, respuesta) en milisegundos
- Respuesta final del LLM

Los logs se escriben en `./logs/YYYY-MM-DD.jsonl` (una línea JSON por consulta) y se envían de forma asíncrona a la tabla `claroinsurance-dataplatform.claro_IA.model_tracking` en BigQuery.

---

## Catálogos y casos de uso

Los casos de uso están definidos en `data/use_cases.json`. Cada caso incluye:

- `tipo`: `sql`, `rag`, o `multiple`
- `usa_esto_cuando`: descripción en lenguaje natural de cuándo aplica este caso — es lo que lee el Agente 1 (LLM) para clasificar la consulta del usuario
- `sql_by_role`: plantillas de query por tipo de usuario (`"1"`, `"2"`, `"3"`) con placeholder `{dynamic_filters}` para el filtro fijo del usuario identificado
- `parametros`: lista de columnas SQL (`PARAM_TO_FILTRO`) que el caso puede recibir como filtro dinámico
- `descripciones`: definición de esas columnas para que el LLM las use correctamente al construir el SQL y al redactar la respuesta
- `filtros_requeridos`: parámetros obligatorios — si no se detectan, no se ejecuta nada y se le pide al usuario que los incluya
- `entity_resolution` y `ending_resolution`: reglas de formato e instrucciones específicas para la respuesta final
- `invoca`: (solo en casos `multiple`) lista de ids de sub-casos a ejecutar en paralelo
- `semantic_examples`: variaciones en lenguaje natural — **campo legado**, ya no se usa para clasificar (ver nota sobre `precalcular_use_cases.py` más arriba)

### Catálogo A — Contratos
Consultas sobre estado de contratos: activos, pendientes, inactivos, detalles, licencias, validaciones.

### Catálogo B — Pagos y Comisiones
Consultas sobre comisiones pagadas, preliquidaciones, reconciliaciones, pagos pendientes por agente o agencia.

### Catálogo C — Documentos Normativos
RAG sobre PDFs institucionales: calendarios, FAQs, instructivos de carriers.

---

## Administración y mantenimiento

### Actualizar filtros (cuando cambien datos en BigQuery)

```bash
python scripts/precalcular_filtros.py
```

### Re-indexar documentos (cuando se agreguen o modifiquen PDFs)

```bash
python scripts/precalcular_embeddings.py
```

### Verificar el estado de los datos precalculados

```bash
python test/revisar_filtros_validos.py
python test/revisar_embeddings.py
```

(`test/revisar_use_cases_embeddings.py` es legado, ver nota sobre `precalcular_use_cases.py` más arriba.)

### Limpiar colecciones de ChromaDB

```bash
python scripts/delete_collections.py
```

### Gestión de usuarios

```bash
# Agregar usuario
python scripts/manage_users.py add

# Listar usuarios
python scripts/manage_users.py list

# Eliminar usuario
python scripts/manage_users.py remove
```

---

## Agregar nuevos casos de uso

1. Abrir `data/use_cases.json`
2. Agregar un nuevo objeto dentro del catálogo correspondiente (`A`, `B`, o `C`) con los campos: `tipo`, `usa_esto_cuando`, `sql_by_role` (uno por tipo de usuario `"1"`/`"2"`/`"3"`), `parametros`, `descripciones`, `filtros_requeridos` (si aplica), `entity_resolution`, `ending_resolution`
3. Redactar bien el `usa_esto_cuando` — es el único texto que usa el Agente 1 (LLM) para decidir si este caso aplica a la consulta del usuario, no hay paso de precálculo ni embeddings que regenerar.

No se requiere modificar `main.py` para agregar casos de uso estándar.

---

## Agregar nuevos documentos normativos (RAG)

1. Colocar el archivo (PDF o `.docx`) en `./pdf/` o `./words/` según corresponda
2. Agregar su configuración en `scripts/precalcular_embeddings.py` (nombre de archivo, colección destino, metadatos como carrier y categoría, estrategia de chunking)
3. Re-ejecutar la indexación:
   ```bash
   python scripts/precalcular_embeddings.py
   ```

---

## Variables de entorno requeridas

| Variable | Descripción |
|---|---|
| `VERTEX_CREDENTIALS_JSON` | Ruta al JSON de credenciales para Vertex AI |
| `VERTEX_PROJECT_ID` | ID del proyecto en GCP |
| `VERTEX_LOCATION` | Región de Vertex AI (ej. `us-central1`) |
| `BQ_CREDENTIALS_JSON` | Ruta al JSON de credenciales para BigQuery |
| `CHROMA_PATH` | Directorio de almacenamiento de ChromaDB |
