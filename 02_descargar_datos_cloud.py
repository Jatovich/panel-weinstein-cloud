"""
02_descargar_datos_cloud.py
------------------------------
Versión pensada para correr en GitHub Actions (no en tu PC ni en tu NAS).
Descarga los datos de mercado y los guarda DIRECTAMENTE en BigQuery —
sin pasar por MariaDB — para que la actualización sea 100% automática
y no dependa de que ningún ordenador tuyo esté encendido.

Las credenciales de Google Cloud se leen de la variable de entorno
GCP_SA_KEY (el contenido del .json de la cuenta de servicio, en texto).
En GitHub Actions, esa variable viene de un "Secret" del repositorio,
nunca se escribe en el código ni se sube a GitHub.

Requisitos (ver requirements.txt del repositorio):
    requests, beautifulsoup4, lxml, pandas, yfinance,
    google-cloud-bigquery, db-dtypes, pyarrow
"""

import os
import io
import json
import time
import requests
import pandas as pd
import yfinance as yf
from google.cloud import bigquery
from google.oauth2 import service_account

# ------------------------------------------------------------------
# CONFIGURACIÓN
# ------------------------------------------------------------------
ID_PROYECTO_GCP = "panel-weinstein"
DATASET_BIGQUERY = "retiro"
WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

TAMANO_LOTE = 40
PERIODO_HISTORICO = "10y"
PAUSA_ENTRE_LOTES = 2.0


def conectar_bigquery() -> bigquery.Client:
    clave_json = os.environ["GCP_SA_KEY"]
    credenciales = service_account.Credentials.from_service_account_info(json.loads(clave_json))
    return bigquery.Client(credentials=credenciales, project=ID_PROYECTO_GCP)


def asegurar_dataset(cliente: bigquery.Client):
    dataset_id = f"{ID_PROYECTO_GCP}.{DATASET_BIGQUERY}"
    try:
        cliente.get_dataset(dataset_id)
    except Exception:
        dataset = bigquery.Dataset(dataset_id)
        dataset.location = "EU"
        cliente.create_dataset(dataset)
        print(f"  Dataset '{DATASET_BIGQUERY}' creado en BigQuery.")


# ------------------------------------------------------------------
# Lista de componentes del S&P 500 (igual lógica que la versión MariaDB)
# ------------------------------------------------------------------
def obtener_componentes_sp500() -> pd.DataFrame:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    resp = requests.get(WIKI_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    tablas = pd.read_html(io.StringIO(resp.text))
    df = tablas[0].rename(columns={
        "Symbol": "ticker", "Security": "nombre", "GICS Sector": "sector",
    })[["ticker", "nombre", "sector"]]
    df["ticker"] = df["ticker"].str.replace(".", "-", regex=False)
    df["activo"] = True
    df["fecha_actualizacion"] = pd.Timestamp.today().date()
    return df


def guardar_constituyentes(cliente: bigquery.Client, df: pd.DataFrame):
    tabla_id = f"{ID_PROYECTO_GCP}.{DATASET_BIGQUERY}.sp500_constituyentes"
    config = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE", autodetect=True)
    trabajo = cliente.load_table_from_dataframe(df, tabla_id, job_config=config)
    trabajo.result()
    print(f"  [OK] {len(df)} componentes del S&P 500 guardados en BigQuery.")


# ------------------------------------------------------------------
# Precios semanales por lotes (Yahoo Finance)
# ------------------------------------------------------------------
def descargar_lote_precios(tickers: list[str], fecha_inicio: str | None = None) -> dict[str, pd.DataFrame]:
    resultado = {}
    parametros = {"start": fecha_inicio} if fecha_inicio else {"period": PERIODO_HISTORICO}
    try:
        datos = yf.download(
            tickers=tickers, interval="1wk", group_by="ticker",
            auto_adjust=False, threads=True, progress=False, **parametros,
        )
    except Exception as e:
        print(f"  [AVISO] Fallo al descargar el lote: {e}")
        return resultado

    for ticker in tickers:
        try:
            df_ticker = datos[ticker] if len(tickers) > 1 else datos
            df_ticker = df_ticker.dropna(how="all")
            if df_ticker.empty:
                continue
            df_ticker = df_ticker.reset_index().rename(columns={
                "Date": "fecha", "Open": "apertura", "High": "maximo",
                "Low": "minimo", "Close": "cierre", "Volume": "volumen",
            })
            df_ticker["fecha"] = pd.to_datetime(df_ticker["fecha"]).dt.date
            df_ticker["ticker"] = ticker
            resultado[ticker] = df_ticker[
                ["ticker", "fecha", "apertura", "maximo", "minimo", "cierre", "volumen"]
            ].dropna(subset=["cierre"])
        except (KeyError, TypeError):
            continue
    return resultado


def obtener_ultima_fecha(cliente: bigquery.Client):
    tabla = f"`{ID_PROYECTO_GCP}.{DATASET_BIGQUERY}.precios_semanales`"
    try:
        resultado = cliente.query(f"SELECT MAX(fecha) AS ultima FROM {tabla}").to_dataframe()
        valor = resultado["ultima"].iloc[0]
        return None if pd.isna(valor) else valor
    except Exception:
        return None  # la tabla todavía no existe: primera ejecución


def obtener_tickers_existentes(cliente: bigquery.Client) -> set:
    tabla = f"`{ID_PROYECTO_GCP}.{DATASET_BIGQUERY}.precios_semanales`"
    try:
        resultado = cliente.query(f"SELECT DISTINCT ticker FROM {tabla}").to_dataframe()
        return set(resultado["ticker"].tolist())
    except Exception:
        return set()


def fusionar_precios_en_bigquery(cliente: bigquery.Client, df_nuevo: pd.DataFrame):
    """Sube los precios nuevos a una tabla temporal y los FUSIONA (MERGE)
    con la tabla principal, evitando duplicados por (ticker, fecha)."""
    if df_nuevo.empty:
        return

    tabla_principal = f"{ID_PROYECTO_GCP}.{DATASET_BIGQUERY}.precios_semanales"
    tabla_staging = f"{ID_PROYECTO_GCP}.{DATASET_BIGQUERY}.precios_semanales_staging"

    config = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE", autodetect=True)
    cliente.load_table_from_dataframe(df_nuevo, tabla_staging, job_config=config).result()

    # Si la tabla principal todavía no existe (primera ejecución), la
    # creamos directamente a partir de la tabla temporal
    try:
        cliente.get_table(tabla_principal)
        tabla_existe = True
    except Exception:
        tabla_existe = False

    if not tabla_existe:
        config_copia = bigquery.CopyJobConfig()
        cliente.copy_table(tabla_staging, tabla_principal, job_config=config_copia).result()
        print(f"  [OK] {len(df_nuevo)} filas (primera carga) guardadas en precios_semanales.")
        return

    consulta_merge = f"""
        MERGE `{tabla_principal}` AS principal
        USING `{tabla_staging}` AS nuevo
        ON principal.ticker = nuevo.ticker AND principal.fecha = nuevo.fecha
        WHEN MATCHED THEN
          UPDATE SET apertura = nuevo.apertura, maximo = nuevo.maximo,
                     minimo = nuevo.minimo, cierre = nuevo.cierre, volumen = nuevo.volumen
        WHEN NOT MATCHED THEN
          INSERT (ticker, fecha, apertura, maximo, minimo, cierre, volumen)
          VALUES (ticker, fecha, apertura, maximo, minimo, cierre, volumen)
    """
    cliente.query(consulta_merge).result()
    print(f"  [OK] {len(df_nuevo)} filas fusionadas en precios_semanales (sin duplicados).")


# ------------------------------------------------------------------
# PROGRAMA PRINCIPAL
# ------------------------------------------------------------------
def main():
    cliente = conectar_bigquery()
    asegurar_dataset(cliente)

    print("Descargando lista de componentes del S&P 500...")
    componentes = obtener_componentes_sp500()
    guardar_constituyentes(cliente, componentes)
    tickers = componentes["ticker"].tolist()

    ultima_fecha = obtener_ultima_fecha(cliente)
    tickers_existentes = obtener_tickers_existentes(cliente)
    tickers_nuevos = [t for t in tickers if t not in tickers_existentes]
    tickers_a_actualizar = [t for t in tickers if t in tickers_existentes]

    if ultima_fecha is None:
        print(f"No hay datos previos: descarga completa inicial de {len(tickers)} tickers.")
        grupos = [(tickers, None)]
    else:
        fecha_inicio = (pd.Timestamp(ultima_fecha) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        print(f"Última fecha en BigQuery: {ultima_fecha}. Incremental desde {fecha_inicio}.")
        grupos = []
        if tickers_a_actualizar:
            grupos.append((tickers_a_actualizar, fecha_inicio))
        if tickers_nuevos:
            print(f"Tickers nuevos detectados: {tickers_nuevos}")
            grupos.append((tickers_nuevos, None))

    for lista_tickers, fecha_inicio_grupo in grupos:
        lotes = [lista_tickers[i:i + TAMANO_LOTE] for i in range(0, len(lista_tickers), TAMANO_LOTE)]
        for n_lote, lote in enumerate(lotes, start=1):
            print(f"  Lote {n_lote}/{len(lotes)} ({len(lote)} tickers)")
            precios_por_ticker = descargar_lote_precios(lote, fecha_inicio=fecha_inicio_grupo)
            if precios_por_ticker:
                df_lote = pd.concat(precios_por_ticker.values(), ignore_index=True)
                fusionar_precios_en_bigquery(cliente, df_lote)
            time.sleep(PAUSA_ENTRE_LOTES)

    print("¡Descarga completada!")


if __name__ == "__main__":
    main()
