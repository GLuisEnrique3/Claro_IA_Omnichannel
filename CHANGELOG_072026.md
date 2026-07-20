# Changelog — 2026-07-20

Sesión enfocada en refactorizar la organización del código: `main.py` (1859 líneas)
concentraba absolutamente toda la lógica de la aplicación (retry de LLM, parser de
fechas, matching semántico, clasificación de intención, construcción/ejecución de SQL,
RAG sobre ChromaDB, orquestación de flujos múltiples, UI de consola y el loop
principal) en un único archivo. Se dividió en un paquete `app/` con un módulo por
responsabilidad, sin cambiar comportamiento.

---

## 1. Nueva estructura de carpetas

```
├── main.py                # Entrypoint: from app.cli import main
├── config/                # Sin cambios (LLM/Vertex, BigQuery, filtros, logger, Chroma)
└── app/
    ├── llm.py              # _llm_call (retry/backoff 429), _extraer_json
    ├── use_cases.py        # Carga use_cases.json / catalog_permissions.json,
    │                       #   TIPOS_USUARIO, PARAM_TO_FILTRO
    ├── ui.py                # Excepciones de control (SalirError/VoverError/MenuError),
    │                       #   _input, headers, instrucciones, _construir_recurso_presentacion
    ├── embeddings.py        # SentenceTransformer, matching semántico, detección de carrier
    ├── entities.py          # Parser de fechas en español, extraer_entidades
    ├── classifier.py        # Agente 1 (seleccionar_caso_de_uso_llm) y Agente 2
    │                       #   (_interpretar_confirmacion)
    ├── sql_engine.py        # Construcción de SQL con LLM + ejecución contra BigQuery
    │                       #   (versión normal y silenciosa)
    ├── rag_engine.py        # Consulta RAG sobre ChromaDB (versión normal y silenciosa)
    ├── multi_query.py       # Orquesta sub-casos SQL+RAG en paralelo y sintetiza la respuesta
    └── cli.py               # identificar_usuario, ciclo_consultas, main()
```

`main.py` en la raíz quedó reducido a:

```python
from app.cli import main

if __name__ == "__main__":
    main()
```

---

## 2. Criterio de división

- **`app/llm.py`** es el único punto con el retry/backoff genérico de Vertex AI
  (`_llm_call`) y el parseo de JSON devuelto por el modelo (`_extraer_json`); lo
  importan `classifier.py`, `sql_engine.py`, `rag_engine.py` y `multi_query.py` para no
  duplicar la lógica de reintentos.
- **`app/use_cases.py`** centraliza toda la configuración estática (`USE_CASES`,
  `TIPOS_USUARIO`, `PARAM_TO_FILTRO`, permisos de catálogo) para que `classifier.py`,
  `entities.py` y `cli.py` la reutilicen sin recargar los JSON cada uno.
- **`app/embeddings.py`** es el único módulo que instancia `SentenceTransformer`
  (`_get_embed_model`); `entities.py`, `rag_engine.py` y la identificación de usuario en
  `cli.py` reusan esa misma instancia perezosa.
- **`app/entities.py`** depende de `embeddings.py` (matching semántico) y
  `use_cases.py` (`PARAM_TO_FILTRO`).
- **`app/sql_engine.py`** y **`app/rag_engine.py`** quedan paralelos entre sí; cada uno
  expone una variante silenciosa (`_ejecutar_consulta_silenciosa` /
  `_ejecutar_rag_silenciosa`) que **`app/multi_query.py`** consume para ejecutar
  sub-casos en paralelo (`ThreadPoolExecutor`) y sintetizar una sola respuesta.
- **`app/ui.py`** absorbe toda la presentación de consola y las excepciones de control
  de flujo (`SalirError`, `VoverError`, `MenuError`), acopladas a `_input`.
- **`app/cli.py`** queda como capa de orquestación delgada: `identificar_usuario`,
  `ciclo_consultas` y `main()`, importando todo lo demás.

---

## 3. Fixes encontrados durante el traslado

- **`app/rag_engine.py` — latencia de respuesta mal medida:** al extraer `ejecutar_rag`
  se detectó que el timer usado para `query_log.latencia_respuesta_ms` reutilizaba por
  error `_t_rag` (el inicio de toda la consulta a ChromaDB) en vez de un timer propio
  justo antes de la llamada al LLM. Se restauró un `_t_resp = time.perf_counter()`
  dedicado, igual que en `sql_engine.ejecutar_consulta`.
- **Código muerto eliminado:** el bloque de impresión de "resultados encontrados" en
  `ejecutar_rag` estaba comentado (`"""..."""`) desde antes del refactor y nunca se
  ejecutaba; se quitó junto con las variables (`metas`, `dists`, `border`) que solo ese
  bloque usaba. Sin impacto funcional — el bloque no corría en ningún caso.

---

## 4. Identificación de usuario: de texto libre (fuzzy) a login por Usuario ARC

El login pedía escribir el nombre de la agencia o el NPN como texto libre, y lo
resolvía por **similitud semántica** (embeddings) contra listas precalculadas
(`data/filtros_validos.pkl`), con umbrales distintos por tipo (0.65 agencia, 0.95 NPN) y
reintento si el score no alcanzaba. Se reemplazó por un login **exacto** contra el
Usuario ARC (`Claro_ARC_User__c`), consultado en vivo a BigQuery.

- **`app/auth.py` (nuevo):** `buscar_usuario_arc(claro_arc_user)` ejecuta una consulta
  parametrizada (`@arc_user`, protegida contra inyección SQL vía
  `bigquery.ScalarQueryParameter`) que hace `JOIN` de `contact` con `dim_account_2`,
  `account_executives`, `NewBusinessTeam__c` y `ContractingSpecialist__c`, filtrando a
  contactos con NPN y Usuario ARC no nulos, y limitados a la agencia "Claro Insurance" o
  contactos con `Agency_Representative__c = 'Yes'`. El match contra `@arc_user` es
  insensible a mayúsculas/espacios (`UPPER(TRIM(...))`). Devuelve `npn`, `name`,
  `agency`, `subagencia`, `subsubagencia`, `arc_user` e `is_agency_representative`.
- **`app/cli.py` — `identificar_usuario()`:** ahora pide una sola vez el Usuario ARC
  (ya no hay prompt distinto por rol) y valida contra el resultado de
  `buscar_usuario_arc`:
  - Usuario ARC no encontrado → reintentar.
  - Rol "Representante de Agencia" elegido pero `is_agency_representative` es `False` →
    rechaza y sugiere entrar como "Agente NPN".
  - El campo requerido por el rol (`npn` o `agency`) viene vacío en el contacto →
    rechaza, pide contactar al administrador.
  - Se eliminó la segunda consulta a BigQuery que antes resolvía la agencia del Agente
    NPN por separado (`SELECT a.Name_Agencies ... WHERE c.NPN__c = ...`): ahora viene en
    el mismo resultado de `buscar_usuario_arc`, sin importar el rol.
- **`app/use_cases.py` — `TIPOS_USUARIO`:** se quitaron `umbral`/`prompt_id` (ya no
  aplica fuzzy-match); se agregó `requiere_agency_representative` (`True` para
  Representante, `False` para Agente NPN) y la constante `ARC_LOGIN_PROMPT`.
- **`app/ui.py`:** instrucciones de la sección "IDENTIFICACIÓN" actualizadas para
  reflejar el login único por Usuario ARC.

**Pendiente/a confirmar:** el filtro fijo
`(a.Name_Agencies = 'Claro Insurance' OR Agency_Representative__c = 'Yes')` limita el
universo total de logins (ambos roles) a esos dos grupos — se implementó tal cual se
especificó; si la intención era que cualquier agente con NPN pueda entrar como "Agente
NPN" y solo el rol "Representante" se restrinja por `Agency_Representative__c`, hay que
separar esa condición por rol en `_QUERY_ARC_USER`.

---

## Resumen de impacto

- `main.py`: 1859 líneas → **4 líneas** (entrypoint).
- Código repartido en **10 módulos** dentro de `app/` (1859 líneas totales, sin pérdida
  ni duplicación de lógica respecto al original).
- `config/` no se tocó: sigue concentrando inicialización de Vertex AI, BigQuery,
  filtros válidos, logger de sesión y cliente de ChromaDB.
- Verificado que los 11 archivos parsean correctamente (`ast.parse`) y que
  `import app.cli` resuelve toda la cadena de dependencias sin errores contra las
  credenciales reales del proyecto.
- No se probó el flujo interactivo completo en consola (requiere input real y acceso a
  BigQuery/ChromaDB con datos) — pendiente correr `python main.py` con una consulta de
  prueba end-to-end.
- Login rediseñado: de identificación por texto libre + similitud semántica, a
  identificación exacta por Usuario ARC (`Claro_ARC_User__c`) resuelta en vivo contra
  BigQuery (`app/auth.py`), con el rol (Agente NPN / Representante de Agencia)
  determinando qué campo del mismo contacto se usa como filtro fijo de sesión.
