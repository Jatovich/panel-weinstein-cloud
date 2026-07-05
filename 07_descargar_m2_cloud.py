"""
07_descargar_m2_cloud.py
--------------------------
Descarga los datos semanales de M2 (oferta monetaria USA) desde la API
de FRED (Federal Reserve Economic Data) y los guarda en BigQuery.

Serie utilizada: WM2NS (Weekly M2 Money Stock, Not Seasonally Adjusted)
Frecuencia: semanal, con ~2 semanas de retraso respecto a la fecha actual.

La API key de FRED se lee de la variable de entorno FRED_API_KEY,
que en GitHub Actions viene de un Secret del repositorio.

Requisitos: requests, pandas, google-cloud-bigquery, db-dtypes, pyarrow
"""

import os
import json
import requests
import pandas as pd
from google.cloud import bigquery
from google.oauth2 import service_account

ID_PROYECTO_GCP = "panel-weinstein"
DATASET_BIGQUERY = "retiro"
TABLA_M2 = f"{ID_PROYECTO_GCP}.{DATASET_BIGQUERY}.m2_liquidez"

FRED_SERIES = "WM2NS"  # M2 semanal, no ajustado estacionalmente
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


def conectar_bigquery() -> bigquery.Client:
    clave_json = os.environ["GCP_SA_KEY"]
    credenciales = service_account.Credentials.from_service_account_info(
        json.loads(clave_json)
    )
    return bigquery.Client(credentials=credenciales, project=ID_PROYECTO_GCP)


def descargar_m2_fred() -> pd.DataFrame:
    """Descarga el histórico completo de M2 semanal desde FRED."""
    api_key = os.environ["FRED_API_KEY"]
    params = {
        "series_id": FRED_SERIES,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": "2010-01-01",  # 10+ años de histórico
    }
    resp = requests.get(FRED_URL, params=params, timeout=15)
    resp.raise_for_status()

    datos = resp.json()["observations"]
    df = pd.DataFrame(datos)[["date", "value"]]
    df = df[df["value"] != "."]  # FRED usa "." para datos no disponibles
    df["fecha"] = pd.to_datetime(df["date"]).dt.date
    df["m2_billones"] = df["value"].astype(float) / 1000  # de miles de millones a billones

    # Variación interanual (52 semanas atrás)
    df = df.sort_values("fecha").reset_index(drop=True)
    df["m2_hace_52s"] = df["m2_billones"].shift(52)
    df["m2_yoy_pct"] = ((df["m2_billones"] - df["m2_hace_52s"]) / df["m2_hace_52s"] * 100).round(2)

    # Tendencia: comparamos con hace 4 semanas
    m2_hace_4s = df["m2_billones"].shift(4)
    diferencia = df["m2_billones"] - m2_hace_4s
    umbral = df["m2_billones"] * 0.002  # menos del 0.2% = "plana"
    df["tendencia"] = "PLANA"
    df.loc[diferencia > umbral, "tendencia"] = "SUBIENDO"
    df.loc[diferencia < -umbral, "tendencia"] = "BAJANDO"

    return df[["fecha", "m2_billones", "m2_yoy_pct", "tendencia"]].dropna(
        subset=["m2_billones"]
    )


def guardar_m2_en_bigquery(cliente: bigquery.Client, df: pd.DataFrame):
    """Sustituye por completo la tabla m2_liquidez en BigQuery."""
    config = bigquery.LoadJobConfig(
        write_disposition="WRITE_TRUNCATE",
        autodetect=True,
    )
    cliente.load_table_from_dataframe(df, TABLA_M2, job_config=config).result()
    print(f"[OK] m2_liquidez: {len(df)} semanas guardadas en BigQuery.")


def main():
    print("Descargando M2 desde FRED...")
    df_m2 = descargar_m2_fred()
    print(f"  {len(df_m2)} observaciones descargadas.")

    print("Conectando a BigQuery...")
    cliente = conectar_bigquery()
    guardar_m2_en_bigquery(cliente, df_m2)
    print("¡M2 actualizado!")


if __name__ == "__main__":
    main()
