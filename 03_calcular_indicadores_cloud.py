"""
03_calcular_indicadores_cloud.py
-----------------------------------
Versión para GitHub Actions: calcula los indicadores de amplitud y el
Stage Analysis a partir de los precios ya guardados en BigQuery (los
deja también en BigQuery). Misma lógica que la versión de MariaDB.

Requisitos: ver requirements.txt del repositorio.
"""

import os
import json
import pandas as pd
import numpy as np
import yfinance as yf
from google.cloud import bigquery
from google.oauth2 import service_account

ID_PROYECTO_GCP = "panel-weinstein"
DATASET_BIGQUERY = "retiro"
VENTANA_MEDIA = 30
VENTANA_52S = 52
VENTANA_VOLUMEN = 10
UMBRAL_VOLUMEN_RUPTURA = 1.3
UNIVERSO_MINIMO = 400


def conectar_bigquery() -> bigquery.Client:
    clave_json = os.environ["GCP_SA_KEY"]
    credenciales = service_account.Credentials.from_service_account_info(json.loads(clave_json))
    return bigquery.Client(credentials=credenciales, project=ID_PROYECTO_GCP)


def cargar_precios(cliente: bigquery.Client) -> pd.DataFrame:
    tabla = f"`{ID_PROYECTO_GCP}.{DATASET_BIGQUERY}.precios_semanales`"
    df = cliente.query(f"SELECT ticker, fecha, cierre FROM {tabla} ORDER BY ticker, fecha").to_dataframe()
    df["fecha"] = pd.to_datetime(df["fecha"])
    return df


# --- Misma lógica de cálculo que la versión MariaDB ---
def calcular_indicadores_amplitud(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["ticker", "fecha"]).copy()

    # Guardarraíl: descartar fechas con universo incompleto. Una semana con
    # pocos tickers produce indicadores basura (0.0%, saldos absurdos) y
    # contaminaría la línea A/D acumulada.
    conteo = df.groupby("fecha")["ticker"].transform("count")
    fechas_malas = df.loc[conteo < UNIVERSO_MINIMO, "fecha"].unique()
    if len(fechas_malas):
        print(f"  [AVISO] Descartadas fechas con universo < {UNIVERSO_MINIMO}: "
              f"{sorted(pd.to_datetime(fechas_malas).strftime('%Y-%m-%d'))}")
    df = df[conteo >= UNIVERSO_MINIMO].copy()

    df["media_30s"] = df.groupby("ticker")["cierre"].transform(
        lambda s: s.rolling(VENTANA_MEDIA, min_periods=VENTANA_MEDIA).mean()
    )
    df["sobre_media_30s"] = df["cierre"] > df["media_30s"]

    df["max_52s_prev"] = df.groupby("ticker")["cierre"].transform(
        lambda s: s.shift(1).rolling(VENTANA_52S, min_periods=VENTANA_52S).max()
    )
    df["min_52s_prev"] = df.groupby("ticker")["cierre"].transform(
        lambda s: s.shift(1).rolling(VENTANA_52S, min_periods=VENTANA_52S).min()
    )
    df["es_nuevo_maximo"] = df["cierre"] > df["max_52s_prev"]
    df["es_nuevo_minimo"] = df["cierre"] < df["min_52s_prev"]

    df["cierre_anterior"] = df.groupby("ticker")["cierre"].shift(1)
    df["avanzo"] = df["cierre"] > df["cierre_anterior"]
    df["declino"] = df["cierre"] < df["cierre_anterior"]

    resumen = df.groupby("fecha").agg(
        num_acciones_analizadas=("ticker", "count"),
        num_sobre_media_30s=("sobre_media_30s", "sum"),
        nuevos_maximos_52s=("es_nuevo_maximo", "sum"),
        nuevos_minimos_52s=("es_nuevo_minimo", "sum"),
        avances=("avanzo", "sum"),
        declives=("declino", "sum"),
    ).reset_index()

    resumen["pct_sobre_media_30s"] = (
        100 * resumen["num_sobre_media_30s"] / resumen["num_acciones_analizadas"]
    ).round(2)

    resumen = resumen.sort_values("fecha")
    resumen["linea_avance_declive"] = (resumen["avances"] - resumen["declives"]).cumsum()

    return resumen[[
        "fecha", "num_acciones_analizadas", "pct_sobre_media_30s",
        "nuevos_maximos_52s", "nuevos_minimos_52s",
        "avances", "declives", "linea_avance_declive",
    ]]


def determinar_etapa(cierre, media, pendiente):
    if pd.isna(cierre) or pd.isna(media):
        return None
    if pendiente == "SUBIENDO":
        return 2 if cierre > media else 1
    if pendiente == "BAJANDO":
        return 4 if cierre < media else 3
    return None


def calcular_stage_indice(ticker_indice: str = "^GSPC", nombre_guardado: str = "SP500") -> pd.DataFrame:
    datos = yf.download(ticker_indice, period="10y", interval="1wk", progress=False)
    datos = datos.reset_index().rename(columns={"Date": "fecha", "Close": "cierre", "Volume": "volumen"})
    datos["fecha"] = pd.to_datetime(datos["fecha"])
    if isinstance(datos.columns, pd.MultiIndex):
        datos.columns = [c[0] for c in datos.columns]

    datos["media_30s"] = datos["cierre"].rolling(VENTANA_MEDIA, min_periods=VENTANA_MEDIA).mean()
    media_previa = datos["media_30s"].shift(2)
    diferencia = datos["media_30s"] - media_previa
    umbral_plana = datos["media_30s"] * 0.001
    datos["pendiente_media"] = np.where(
        diferencia > umbral_plana, "SUBIENDO",
        np.where(diferencia < -umbral_plana, "BAJANDO", "PLANA")
    )

    media_volumen_prev = datos["volumen"].shift(1).rolling(VENTANA_VOLUMEN, min_periods=VENTANA_VOLUMEN).mean()
    datos["volumen_relativo"] = (datos["volumen"] / media_volumen_prev).round(2)

    etapas, etapa_anterior = [], None
    for cierre, media, pendiente in zip(datos["cierre"], datos["media_30s"], datos["pendiente_media"]):
        if pd.isna(media):
            etapas.append(None)
            etapa_anterior = None
            continue
        if pendiente == "SUBIENDO":
            etapa = 2 if cierre > media else (etapa_anterior if etapa_anterior in (1, 4) else 1)
        elif pendiente == "BAJANDO":
            etapa = 4 if cierre < media else (etapa_anterior if etapa_anterior in (2, 3) else 3)
        else:
            if etapa_anterior in (4, 1):
                etapa = 1
            elif etapa_anterior in (2, 3):
                etapa = 3
            else:
                etapa = None
        etapas.append(etapa)
        etapa_anterior = etapa
    datos["etapa"] = etapas

    etapa_previa_serie = datos["etapa"].shift(1)
    es_ruptura_alcista = (etapa_previa_serie == 1) & (datos["etapa"] == 2)
    es_ruptura_bajista = (etapa_previa_serie == 3) & (datos["etapa"] == 4)
    es_ruptura = es_ruptura_alcista | es_ruptura_bajista
    datos["confirmado_volumen"] = None
    datos.loc[es_ruptura, "confirmado_volumen"] = datos.loc[es_ruptura, "volumen_relativo"] >= UMBRAL_VOLUMEN_RUPTURA

    datos["indice"] = nombre_guardado
    return datos[[
        "fecha", "indice", "cierre", "media_30s", "volumen", "volumen_relativo",
        "pendiente_media", "etapa", "confirmado_volumen",
    ]].dropna(subset=["media_30s"])


def guardar_en_bigquery(cliente: bigquery.Client, df: pd.DataFrame, nombre_tabla: str):
    tabla_id = f"{ID_PROYECTO_GCP}.{DATASET_BIGQUERY}.{nombre_tabla}"
    config = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE", autodetect=True)
    cliente.load_table_from_dataframe(df, tabla_id, job_config=config).result()
    print(f"  [OK] {nombre_tabla}: {len(df)} filas guardadas en BigQuery.")


def main():
    cliente = conectar_bigquery()

    print("Cargando precios desde BigQuery...")
    precios = cargar_precios(cliente)
    print(f"  {len(precios)} filas de {precios['ticker'].nunique()} tickers.")

    print("Calculando indicadores de amplitud...")
    indicadores = calcular_indicadores_amplitud(precios)
    guardar_en_bigquery(cliente, indicadores, "indicadores_amplitud")

    print("Calculando Stage Analysis del S&P 500...")
    stage = calcular_stage_indice()
    guardar_en_bigquery(cliente, stage, "stage_indice")

    print("¡Cálculo completado!")


if __name__ == "__main__":
    main()
