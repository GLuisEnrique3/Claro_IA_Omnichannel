# Cambios: Soporte de SQL por Rol de Usuario

## Contexto

Se agregó soporte para que cada caso de uso pueda tener una query SQL distinta
según el rol del usuario autenticado (Agencia, NPN, Management). Esto es necesario
para la próxima tabla de compensaciones, donde cada rol debe ver una vista diferente
de los datos.

---

## 1. `data/use_cases.json`

### Qué cambió

El campo `"sql"` fue reemplazado por `"sql_by_role"` en los 18 casos de uso de tipo SQL.

### Antes

```json
{
  "id": "1",
  "tipo": "sql",
  "sql": "SELECT COUNT(*) FROM ... WHERE ... {dynamic_filters}"
}
```

### Después

```json
{
  "id": "1",
  "tipo": "sql",
  "sql_by_role": {
    "1": "SELECT COUNT(*) FROM ... WHERE ... {dynamic_filters}",
    "2": "SELECT COUNT(*) FROM ... WHERE ... {dynamic_filters}",
    "3": "SELECT COUNT(*) FROM ... WHERE ... {dynamic_filters}"
  }
}
```

Las claves `"1"`, `"2"`, `"3"` corresponden a los roles definidos en `TIPOS_USUARIO`:

| Clave | Rol                    |
|-------|------------------------|
| `"1"` | Representante de Agencia |
| `"2"` | Agente NPN             |
| `"3"` | Management             |

**Por ahora los tres roles tienen el mismo SQL.** Cuando se agregue la tabla de
compensaciones, se definirán SQLs distintos por rol para esos casos de uso específicos.

---

## 2. `main.py`

### Cadena de propagación

El rol del usuario (`tipo_key`) se captura en `main()` al momento de la
autenticación y se pasa hacia abajo hasta la función que construye el SQL.

```
main()
  └─ tipo_key = "1" / "2" / "3"   ← viene de TIPOS_USUARIO al autenticarse
       └─ ciclo_consultas(..., tipo_key)
            ├─ ejecutar_consulta(..., tipo_key)
            │    └─ _construir_sql_con_llm(..., tipo_key)
            │         └─ pregunta["sql_by_role"][tipo_key]   ← SQL del rol
            └─ ejecutar_multiple(..., tipo_key)
                 └─ _ejecutar_consulta_silenciosa(..., tipo_key)
                      └─ _construir_sql_con_llm(..., tipo_key)
```

### Funciones modificadas

| Función | Cambio |
|---|---|
| `_construir_sql_con_llm` | Lee `pregunta["sql_by_role"][tipo_key]` en vez de `pregunta["sql"]` |
| `ejecutar_consulta` | Recibe `tipo_key`, actualiza el guard, lo pasa al LLM |
| `_ejecutar_consulta_silenciosa` | Recibe y propaga `tipo_key` |
| `ejecutar_multiple` | Recibe y propaga `tipo_key` |
| `ciclo_consultas` | Recibe y propaga `tipo_key` |
| `main` | Pasa `tipo_key` a `ciclo_consultas` |

### Guard condition actualizado

```python
# Antes
if pregunta.get("tipo") != "sql" or "sql" not in pregunta:

# Después
if pregunta.get("tipo") != "sql" or "sql_by_role" not in pregunta:
```

---

## 3. `app/engine.py`

Misma propagación de `tipo_key` para el flujo de la app Streamlit.

| Función | Cambio |
|---|---|
| `sql_response` | Recibe `tipo_key`, lo pasa a `_construir_sql_con_llm` |
| `multiple_response` | Recibe `tipo_key`, lo pasa a `_ejecutar_consulta_silenciosa` |

---

## 4. Acción requerida después de estos cambios

El archivo `data/use_cases_embeddings.pkl` guarda una copia serializada de cada
entrada del JSON. Debe regenerarse para que el sistema en memoria use el nuevo
formato `sql_by_role`:

```bash
python scripts/precalcular_use_cases.py
```

---

## Cómo agregar un caso de uso con SQL diferente por rol

Solo se modifica el JSON. No se toca Python.

```json
{
  "id": "comp_1",
  "texto": "Compensaciones",
  "tipo": "sql",
  "sql_by_role": {
    "1": "SELECT a.Name_Agencies, SUM(monto) ... GROUP BY a.Name_Agencies",
    "2": "SELECT c.Name, c.NPN__c, SUM(monto) ... GROUP BY c.Name, c.NPN__c",
    "3": "SELECT a.Name_Agencies, c.Name, SUM(monto) ... GROUP BY a.Name_Agencies, c.Name"
  }
}
```

Después de editar el JSON, volver a ejecutar `precalcular_use_cases.py`.
