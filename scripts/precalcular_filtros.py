import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import FILTROS_VALIDOS_PATH, FILTROS_EMBEDDINGS_PATH
from google.cloud import bigquery
import pickle

def cargar_filtros_desde_bq():
    client = bigquery.Client()
    filtros = {}
    
    # Line of Business
    query_lob = f"""
        SELECT DISTINCT Line_Of_Business 
        FROM `claroinsurance-dataplatform.claro_bi.dim_cslb` 
        WHERE Line_Of_Business IS NOT NULL
    """
    filtros["line_of_business"] = [row.Line_Of_Business for row in client.query(query_lob).result()]
    
    # Carrier
    query_carrier = f"""
        SELECT DISTINCT Carrier 
        FROM `claroinsurance-dataplatform.claro_bi.dim_cslb` 
        WHERE Carrier IS NOT NULL
    """
    filtros["carrier"] = [row.Carrier for row in client.query(query_carrier).result()]
    
    # State
    query_state = f"""
        SELECT DISTINCT State
        FROM `claroinsurance-dataplatform.claro_bi.dim_cslb` 
    """
    filtros["state"] = [row.State for row in client.query(query_state).result()]

    # Nomre de la agencia
    query_agency = f"""
        SELECT DISTINCT Name_Agencies 
        FROM `claroinsurance-dataplatform.claro_bi.dim_account_2` 
        WHERE Name_Agencies IS NOT NULL
    """
    filtros["agency"] = [row.Name_Agencies for row in client.query(query_agency).result()]

    # NPN
    query_npn = f"""
        SELECT DISTINCT NPN__c 
        FROM `claroinsurance-dataplatform.claro_bi.dim_contact` 
        WHERE NPN__c IS NOT NULL
    """
    filtros["npn"] = [row.NPN__c for row in client.query(query_npn).result()]

    # Nombre del Agente
    query_name = f"""
        SELECT DISTINCT Name
        FROM `claroinsurance-dataplatform.claro_bi.dim_contact` 
        WHERE NPN__c IS NOT NULL
    """
    filtros["name"] = [row.Name for row in client.query(query_name).result()]
    
    # Account Executive
    query_account_executives = f"""

    SELECT DISTINCT Name
    FROM `claroinsurance-dataplatform.salesforce_claro.account_executives`
    """
    filtros["account_executives"] = [row.Name for row in client.query(query_account_executives).result()]

    #-------------- Test 

    # Opp- StageName
    query_stage_name = f"""

    SELECT DISTINCT StageName 
    FROM `claro_IA.vw_pending_contacts_contracts`
    """
    filtros["stage_name"] = [row.StageName for row in client.query(query_stage_name).result()]

    # Opp- Sub StageName
    query_sub_stage = f"""

    SELECT DISTINCT Sub_Stage__c
    FROM `claro_IA.vw_pending_contacts_contracts`
    """
    filtros["sub_stage_name"] = [row.Sub_Stage__c for row in client.query(query_sub_stage).result()]

    # License State
    query_license_state = """
        SELECT DISTINCT LicenseState__c
        FROM `claroinsurance-dataplatform.claro_bi.vw_agent_licenses`
        WHERE LicenseState__c IS NOT NULL
    """
    filtros["license_state"] = [row.LicenseState__c for row in client.query(query_license_state).result()]

    # License Type
    query_license_type = """
        SELECT DISTINCT LicenseType__c
        FROM `claroinsurance-dataplatform.claro_bi.vw_agent_licenses`
        WHERE LicenseType__c IS NOT NULL
    """
    filtros["license_type"] = [row.LicenseType__c for row in client.query(query_license_type).result()]

    # License Status (Active/Inactive)
    query_license_status = """
        SELECT DISTINCT Status__c
        FROM `claroinsurance-dataplatform.claro_bi.vw_agent_licenses`
        WHERE Status__c IS NOT NULL
    """
    filtros["license_status"] = [row.Status__c for row in client.query(query_license_status).result()]

    # License Disposition
    query_license_disposition = """
        SELECT DISTINCT Disposition__c
        FROM `claroinsurance-dataplatform.claro_bi.vw_agent_licenses`
        WHERE Disposition__c IS NOT NULL
    """
    filtros["license_disposition"] = [row.Disposition__c for row in client.query(query_license_disposition).result()]

    # License Sub-Disposition
    query_license_sub_disposition = """
        SELECT DISTINCT SubDisposition__c
        FROM `claroinsurance-dataplatform.claro_bi.vw_agent_licenses`
        WHERE SubDisposition__c IS NOT NULL
    """
    filtros["license_sub_disposition"] = [row.SubDisposition__c for row in client.query(query_license_sub_disposition).result()]

    # License Source
    query_license_source = """
        SELECT DISTINCT Source__c
        FROM `claroinsurance-dataplatform.claro_bi.vw_agent_licenses`
        WHERE Source__c IS NOT NULL
    """
    filtros["license_source"] = [row.Source__c for row in client.query(query_license_source).result()]

    # License Class Name
    query_license_class = """
        SELECT DISTINCT LicenseClassName__c
        FROM `claroinsurance-dataplatform.salesforce_raw.NIPRLicenses__c`
        WHERE LicenseClassName__c IS NOT NULL
    """
    filtros["license_class"] = [row.LicenseClassName__c for row in client.query(query_license_class).result()]

    # License Line of Authority
    query_license_loa = """
        SELECT DISTINCT LineOfAuthority__c
        FROM `claroinsurance-dataplatform.salesforce_raw.NIPRLicenses__c`
        WHERE LineOfAuthority__c IS NOT NULL
    """
    filtros["license_line_of_authority"] = [row.LineOfAuthority__c for row in client.query(query_license_loa).result()]

    # Contract Status
    query_contract_status = """
        SELECT DISTINCT Status__c
        FROM `claroinsurance-dataplatform.salesforce_raw.New_Contracts__c`
        WHERE Status__c IS NOT NULL
    """
    filtros["contract_status"] = [row.Status__c for row in client.query(query_contract_status).result()]

    # Payment Status
    query_payment_status = """
        SELECT DISTINCT Payment_Status__c
        FROM `claroinsurance-dataplatform.salesforce_claro.payment`
        WHERE Payment_Status__c IS NOT NULL
    """
    filtros["payment_status"] = [row.Payment_Status__c for row in client.query(query_payment_status).result()]

    # Payment Type
    query_payment_type = """
        SELECT DISTINCT Payment_Type__c
        FROM `claroinsurance-dataplatform.salesforce_claro.payment`
        WHERE Payment_Type__c IS NOT NULL
    """
    filtros["payment_type"] = [row.Payment_Type__c for row in client.query(query_payment_type).result()]

    with open(FILTROS_VALIDOS_PATH, "wb") as f:
        pickle.dump(filtros, f)

    print(f"Cargados: {len(filtros['line_of_business'])} LOBs, {len(filtros['carrier'])} carriers, "
          f"{len(filtros['agency'])} agencias, {len(filtros['name'])} nombres, "
          f"{len(filtros['npn'])} NPNs, {len(filtros['state'])} estados, "
          f"{len(filtros['stage_name'])} stage names, {len(filtros['sub_stage_name'])} sub stages, "
          f"{len(filtros['account_executives'])} account executives, "
          f"{len(filtros['contract_status'])} contract statuses, "
          f"{len(filtros['payment_status'])} payment statuses, {len(filtros['payment_type'])} payment types")

    # ── Embeddings pre-computados por filtro ──────────────────────────────────
    print("\nGenerando embeddings pre-computados para búsqueda semántica...")
    from sentence_transformers import SentenceTransformer as _ST
    _model = _ST("sentence-transformers/all-MiniLM-L6-v2")

    filtros_embeddings = {}
    for key, valores in filtros.items():
        valores_str = [str(v) for v in valores if v is not None]
        if not valores_str:
            continue
        embs = _model.encode(valores_str, show_progress_bar=False).tolist()
        filtros_embeddings[key] = {"valores": valores_str, "embeddings": embs}
        print(f"  {key}: {len(valores_str)} embeddings generados")

    with open(FILTROS_EMBEDDINGS_PATH, "wb") as f:
        pickle.dump(filtros_embeddings, f)
    print(f"filtros_embeddings.pkl guardado en {FILTROS_EMBEDDINGS_PATH}")
    # ─────────────────────────────────────────────────────────────────────────

    return filtros

if __name__ == "__main__":
    cargar_filtros_desde_bq()
