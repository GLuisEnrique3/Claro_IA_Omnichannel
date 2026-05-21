import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import psycopg2

dsn = os.getenv("PGVECTOR_DSN")
print("DSN cargado:", "SI" if dsn else "NO")

conn = psycopg2.connect(dsn, connect_timeout=10)
cur = conn.cursor()

cur.execute(
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema='public' AND table_name LIKE 'db_%' ORDER BY 1"
)
tablas = [r[0] for r in cur.fetchall()]
print("Tablas encontradas:", tablas if tablas else "NINGUNA")

for t in tablas:
    cur.execute(f"SELECT COUNT(*) FROM {t}")
    count = cur.fetchone()[0]
    if count > 0:
        cur.execute(f"SELECT metadata->>'filename', COUNT(*) FROM {t} GROUP BY 1 ORDER BY 1")
        archivos = cur.fetchall()
        print(f"\n  {t}: {count} chunks")
        for archivo, n in archivos:
            print(f"    archivo={archivo}  chunks={n}")
    else:
        print(f"\n  {t}: VACIA")

conn.close()
