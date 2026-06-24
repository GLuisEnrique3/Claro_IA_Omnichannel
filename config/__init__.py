import os
from dotenv import load_dotenv
import pickle

import vertexai
from google.oauth2 import service_account
from vertexai.generative_models import GenerativeModel
from google.cloud import bigquery

load_dotenv()

#---- LLM ----#
_credentials = service_account.Credentials.from_service_account_file(
    os.getenv("VERTEX_CREDENTIALS_JSON")
)

vertexai.init(
    project=os.getenv("VERTEX_PROJECT_ID", "claroinsurance-dataplatform"),
    location=os.getenv("VERTEX_LOCATION", "us-central1"),
    credentials=_credentials,
)

llm_model     = GenerativeModel("gemini-2.5-flash") 
llm_sql_model = GenerativeModel("gemini-2.5-pro")         # construcción de SQL

#---- Consultas BigQuery ----#
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = os.getenv("BQ_CREDENTIALS_JSON")
client = bigquery.Client()

#---- Filtros válidos ----#
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
FILTROS_VALIDOS_PATH = os.path.join(DATA_DIR, "filtros_validos.pkl")
FILTROS_EMBEDDINGS_PATH = os.path.join(DATA_DIR, "filtros_embeddings.pkl")

try:
    with open(FILTROS_VALIDOS_PATH, "rb") as f:
        FILTROS_VALIDOS = pickle.load(f)
except Exception:
    FILTROS_VALIDOS = {"line_of_business": [], "carrier": [], "agency": []}

try:
    with open(FILTROS_EMBEDDINGS_PATH, "rb") as f:
        FILTROS_EMBEDDINGS = pickle.load(f)
except Exception:
    FILTROS_EMBEDDINGS = {}