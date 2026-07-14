# -*- coding: utf-8 -*-
"""
screener.py — Screener de momentum (metodología charla Rankia / D. Leyguarda)
=============================================================================
Paso semanal del workflow de GitHub Actions, a ejecutar DESPUÉS de la carga
de precios en BigQuery.

Embudo de filtros (sobre datos diarios):
  F1  Precio > 8$  y  volumen medio 20 sesiones > 1M acciones
  F2  Retorno > +25% en 100 sesiones  y  > +50% en 200 sesiones
  F3  Exceso de retorno vs S&P 500:  > +15% a 6 meses  y  > +30% a 3 meses
      (valores corregidos según la diapositiva original: exige ACELERACIÓN)
  F4  RSI(14) entre 40 y 65 (zona de consolidación)
  F5  Put/Call ratio (informativo, no filtra) — ratio ≤ 0.5 con 5.000+
      contratos marca senal_pc; además se guarda el put/call del SPY
      como termómetro de sentimiento del índice (tabla putcall_indice)

Además:
  - Mansfield RS semanal de los 11 sectores GICS (ETFs SPDR) → tabla sectores_rs
  - Cruce opcional con la etapa Weinstein de la tabla de Stage Analysis
  - Se guardan también los candidatos "en sala de espera": pasan F1-F3
    pero no F4 (fuertes pero aún extendidos; pueden entrar en zona la
    semana siguiente)

Escritura idempotente: se borran las filas de la fecha de cálculo antes de
insertar, de modo que re-ejecutar el job un mismo sábado no duplica datos.
"""

import logging
import sys
from datetime import date
import json
import os

import numpy as np
import pandas as pd
import yfinance as yf
from google.cloud import bigquery
from google.oauth2 import service_account


# ---------------------------------------------------------------------------
# CONFIG — ajustar a tu esquema real. Todo lo "adaptable" está aquí.
# ---------------------------------------------------------------------------
PROYECTO = "panel-weinstein"          # p.ej. el que ya usas en la carga semanal
DATASET = "retiro"

# Origen de los precios diarios: "bigquery" (recomendado, ya cargados por el
# paso anterior del workflow) o "yfinance" (descarga directa como respaldo).
ORIGEN_PRECIOS = "yfinance"

# Tabla maestra de precios diarios y nombres de sus columnas.
TABLA_PRECIOS = f"{PROYECTO}.{DATASET}.precios_diarios"
COL_FECHA, COL_TICKER, COL_CIERRE, COL_VOLUMEN = "fecha", "ticker", "cierre", "volumen"

# Tabla de componentes del S&P 500 con su sector GICS (la que alimentas
# desde Wikipedia). Poner a None para leer Wikipedia directamente.
TABLA_COMPONENTES = f"{PROYECTO}.{DATASET}.sp500_constituyentes"
COL_COMP_TICKER, COL_COMP_SECTOR = "ticker", "sector"

# Tabla de Stage Analysis (etapa Weinstein más reciente por ticker).
# Poner a None si no quieres el cruce.
# TABLA_ETAPAS = f"{PROYECTO}.{DATASET}.etapas_weinstein"
# COL_ETAPA_TICKER, COL_ETAPA, COL_ETAPA_FECHA = "ticker", "etapa", "fecha"
TABLA_ETAPAS = None

# Tablas de salida.
TABLA_CANDIDATOS = f"{PROYECTO}.{DATASET}.screener_candidatos"
TABLA_SECTORES = f"{PROYECTO}.{DATASET}.sectores_rs"
TABLA_ETFS = f"{PROYECTO}.{DATASET}.etfs_semanales"
TABLA_PC_INDICE = f"{PROYECTO}.{DATASET}.putcall_indice"

# Umbrales del embudo.
PRECIO_MIN = 8.0
VOLUMEN_MEDIO_MIN = 1_000_000        # acciones/día, media 20 sesiones
RET_100D_MIN = 0.25
RET_200D_MIN = 0.50
EXCESO_6M_MIN = 0.15                 # ~126 sesiones
EXCESO_3M_MIN = 0.30                 # ~63 sesiones  (aceleración)
RSI_MIN, RSI_MAX = 40.0, 65.0

SESIONES_6M, SESIONES_3M = 126, 63
INDICE = "^GSPC"

# ETFs SPDR para el Mansfield sectorial (sector GICS → ETF).
ETFS_SECTORIALES = {
    "Information Technology": "XLK",
    "Health Care": "XLV",
    "Financials": "XLF",
    "Consumer Discretionary": "XLY",
    "Communication Services": "XLC",
    "Industrials": "XLI",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Materials": "XLB",
}

# VARIABLES PARA OPCIONES
DIAS_VENCIMIENTO_MAX = 90     # ventana de vencimientos para el put/call
PC_RATIO_UMBRAL = 0.5         # umbral informativo (Leyguarda)
PC_VOLUMEN_MIN = 5000         # contratos mínimos para significancia


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("screener")


# ---------------------------------------------------------------------------
# Indicadores
# ---------------------------------------------------------------------------
def crear_cliente() -> bigquery.Client:
    """En Actions: credenciales desde el secreto GCP_SA_KEY (JSON en texto).
    En local: credenciales por defecto (GOOGLE_APPLICATION_CREDENTIALS)."""
    clave = os.environ.get("GCP_SA_KEY")
    if clave:
        cred = service_account.Credentials.from_service_account_info(json.loads(clave))
        return bigquery.Client(project=PROYECTO, credentials=cred)
    return bigquery.Client(project=PROYECTO)

def rsi_wilder(cierres: pd.Series, periodo: int = 14) -> float:
    """RSI de Wilder (media móvil exponencial con alpha=1/periodo).
    Devuelve el último valor de la serie."""
    delta = cierres.diff()
    ganancia = delta.clip(lower=0).ewm(alpha=1 / periodo, min_periods=periodo).mean()
    perdida = (-delta.clip(upper=0)).ewm(alpha=1 / periodo, min_periods=periodo).mean()
    rs = ganancia / perdida.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return float(rsi.iloc[-1])


def retorno(cierres: pd.Series, sesiones: int) -> float:
    """Retorno simple sobre las últimas `sesiones` sesiones."""
    if len(cierres) <= sesiones:
        return np.nan
    return float(cierres.iloc[-1] / cierres.iloc[-1 - sesiones] - 1)


def mansfield_rs(cierres_sem: pd.Series, indice_sem: pd.Series, semanas: int = 52) -> float:
    """Fuerza relativa de Mansfield sobre datos semanales.

    RP = cierre / índice;  Mansfield = (RP / SMA(RP, 52) - 1) * 100
    > 0 → el valor bate al índice respecto a su media de un año.
    """
    df = pd.concat([cierres_sem, indice_sem], axis=1, keys=["v", "i"]).dropna()
    if len(df) < semanas + 1:
        return np.nan
    rp = df["v"] / df["i"]
    sma = rp.rolling(semanas).mean()
    return float((rp.iloc[-1] / sma.iloc[-1] - 1) * 100)


# ---------------------------------------------------------------------------
# Carga de datos
# ---------------------------------------------------------------------------
def cargar_componentes(cliente: bigquery.Client) -> pd.DataFrame:
    """Tickers del S&P 500 con su sector GICS. Devuelve columnas [ticker, sector]."""
    if TABLA_COMPONENTES:
        sql = f"""
            SELECT {COL_COMP_TICKER} AS ticker, {COL_COMP_SECTOR} AS sector
            FROM `{TABLA_COMPONENTES}`
            WHERE activo = TRUE
        """
        df = cliente.query(sql).to_dataframe()
    else:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tabla = pd.read_html(url)[0]
        df = tabla.rename(columns={"Symbol": "ticker", "GICS Sector": "sector"})[
            ["ticker", "sector"]
        ]
    df["ticker"] = df["ticker"].str.replace(".", "-", regex=False)  # BRK.B → BRK-B
    log.info("Componentes cargados: %d tickers", len(df))
    return df


def cargar_precios_bigquery(cliente: bigquery.Client, tickers: list[str]) -> pd.DataFrame:
    """Precios diarios desde la tabla maestra. Últimos ~420 días naturales
    (≈ 290 sesiones, suficiente para el retorno a 200 sesiones)."""
    sql = f"""
        SELECT {COL_FECHA} AS fecha, {COL_TICKER} AS ticker,
               {COL_CIERRE} AS cierre, {COL_VOLUMEN} AS volumen
        FROM `{TABLA_PRECIOS}`
        WHERE {COL_FECHA} >= DATE_SUB(CURRENT_DATE(), INTERVAL 420 DAY)
          AND {COL_TICKER} IN UNNEST(@tickers)
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("tickers", "STRING", tickers)]
    )
    df = cliente.query(sql, job_config=cfg).to_dataframe()
    df["fecha"] = pd.to_datetime(df["fecha"])
    return df


def cargar_precios_yfinance(tickers: list[str]) -> pd.DataFrame:
    """Descarga diaria por lotes con pausa, para esquivar la limitación
    de peticiones de Yahoo con listas grandes."""
    import time

    LOTE = 50
    filas = []
    for i in range(0, len(tickers), LOTE):
        lote = tickers[i:i + LOTE]
        log.info("Descargando lote %d-%d de %d...",
                 i + 1, min(i + LOTE, len(tickers)), len(tickers))
        datos = yf.download(lote, period="14mo", interval="1d",
                            auto_adjust=True, group_by="ticker",
                            threads=True, progress=False)
        for t in lote:
            try:
                sub = datos[t][["Close", "Volume"]].dropna()
            except KeyError:
                continue
            if sub.empty:
                continue
            sub = sub.rename(columns={"Close": "cierre", "Volume": "volumen"})
            sub["ticker"] = t
            sub["fecha"] = sub.index
            filas.append(sub.reset_index(drop=True))
        time.sleep(2)
    return pd.concat(filas, ignore_index=True)


def cargar_indice_y_etfs() -> tuple[pd.Series, pd.Series, dict[str, pd.Series]]:
    """S&P 500 diario y semanal + cierres semanales de los ETFs sectoriales."""
    idx_d = yf.download(INDICE, period="14mo", interval="1d", auto_adjust=True)["Close"]
    if isinstance(idx_d, pd.DataFrame):
        idx_d = idx_d.squeeze()

    simbolos = [INDICE] + list(ETFS_SECTORIALES.values())
    sem = yf.download(simbolos, period="2y", interval="1wk",
                      auto_adjust=True, group_by="ticker", threads=True)
    idx_w = sem[INDICE]["Close"].dropna()
    etfs_w = {s: sem[e]["Close"].dropna() for s, e in ETFS_SECTORIALES.items()}
    return idx_d.dropna(), idx_w, etfs_w


def cargar_etapas(cliente: bigquery.Client) -> pd.DataFrame:
    """Etapa Weinstein más reciente por ticker. Vacío si no hay tabla."""
    if not TABLA_ETAPAS:
        return pd.DataFrame(columns=["ticker", "etapa"])
    sql = f"""
        SELECT {COL_ETAPA_TICKER} AS ticker, {COL_ETAPA} AS etapa
        FROM `{TABLA_ETAPAS}`
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY {COL_ETAPA_TICKER} ORDER BY {COL_ETAPA_FECHA} DESC
        ) = 1
    """
    try:
        return cliente.query(sql).to_dataframe()
    except Exception as exc:  # tabla inexistente u otro fallo no crítico
        log.warning("No se pudo leer etapas (%s); se continúa sin cruce.", exc)
        return pd.DataFrame(columns=["ticker", "etapa"])



# ---------------------------------------------------------------------------
# Cálculo del embudo
# ---------------------------------------------------------------------------
def evaluar_ticker(g: pd.DataFrame, idx_d: pd.Series) -> dict | None:
    """Aplica F1-F4 a un ticker. Devuelve dict de métricas o None si no hay
    histórico suficiente."""
    g = g.sort_values("fecha")
    cierres = g["cierre"].reset_index(drop=True)
    if len(cierres) < 210:
        return None

    precio = float(cierres.iloc[-1])
    vol_medio_20 = float(g["volumen"].tail(20).mean())
    r100 = retorno(cierres, 100)
    r200 = retorno(cierres, 200)

    # Exceso vs índice en ventanas alineadas por nº de sesiones.
    idx = idx_d.reset_index(drop=True)
    r6, r3 = retorno(cierres, SESIONES_6M), retorno(cierres, SESIONES_3M)
    i6, i3 = retorno(idx, SESIONES_6M), retorno(idx, SESIONES_3M)
    exceso_6m, exceso_3m = r6 - i6, r3 - i3

    rsi = rsi_wilder(cierres)

    f1 = precio > PRECIO_MIN and vol_medio_20 > VOLUMEN_MEDIO_MIN
    f2 = r100 > RET_100D_MIN and r200 > RET_200D_MIN
    f3 = exceso_6m > EXCESO_6M_MIN and exceso_3m > EXCESO_3M_MIN
    f4 = RSI_MIN <= rsi <= RSI_MAX

    return {
        "precio": round(precio, 2),
        "vol_medio_20d": int(vol_medio_20),
        "ret_100d": round(r100, 4),
        "ret_200d": round(r200, 4),
        "exceso_6m": round(exceso_6m, 4),
        "exceso_3m": round(exceso_3m, 4),
        "rsi_14": round(rsi, 1),
        "pasa_f1": f1, "pasa_f2": f2, "pasa_f3": f3, "pasa_f4": f4,
        "pasa_todo": f1 and f2 and f3 and f4,
    }


def calcular_screener(precios: pd.DataFrame, componentes: pd.DataFrame,
                      idx_d: pd.Series, etapas: pd.DataFrame,
                      fecha_calculo: date) -> pd.DataFrame:
    resultados, conteos = [], {"universo": 0, "f1": 0, "f2": 0, "f3": 0, "f4": 0}
    sector_por_ticker = dict(zip(componentes["ticker"], componentes["sector"]))

    for ticker, g in precios.groupby("ticker"):
        conteos["universo"] += 1
        m = evaluar_ticker(g, idx_d)
        if m is None:
            continue
        for f in ("f1", "f2", "f3", "f4"):
            # Conteo acumulativo del embudo: cada filtro sobre los que
            # pasaron los anteriores.
            previos = all(m[f"pasa_f{k}"] for k in range(1, int(f[1])))
            if previos and m[f"pasa_{f}"]:
                conteos[f] += 1
        # Se persisten los que pasan F1-F3 (finalistas + sala de espera).
        if m["pasa_f1"] and m["pasa_f2"] and m["pasa_f3"]:
            m.update(fecha=fecha_calculo, ticker=ticker,
                     sector=sector_por_ticker.get(ticker))
            resultados.append(m)

    log.info("Embudo: universo=%(universo)d → F1=%(f1)d → F2=%(f2)d "
             "→ F3=%(f3)d → F4=%(f4)d", conteos)

    df = pd.DataFrame(resultados)
    if df.empty:
        log.warning("Lista vacía esta semana (posible ausencia de líderes claros).")
        return df
    df = df.merge(etapas, on="ticker", how="left")
    orden = ["fecha", "ticker", "sector", "etapa", "precio", "vol_medio_20d",
             "ret_100d", "ret_200d", "exceso_6m", "exceso_3m", "rsi_14",
             "pasa_f1", "pasa_f2", "pasa_f3", "pasa_f4", "pasa_todo"]
    orden = [c for c in orden if c in df.columns]
    return df[orden].sort_values(["pasa_todo", "exceso_3m"], ascending=[False, False])


def calcular_sectores(idx_w: pd.Series, etfs_w: dict[str, pd.Series],
                      fecha_calculo: date) -> pd.DataFrame:
    filas = []
    for sector, cierres in etfs_w.items():
        filas.append({
            "fecha": fecha_calculo,
            "sector": sector,
            "etf": ETFS_SECTORIALES[sector],
            "mansfield_rs": round(mansfield_rs(cierres, idx_w), 2),
            "ret_3m": round(retorno(cierres, 13), 4),   # 13 semanas
            "ret_6m": round(retorno(cierres, 26), 4),   # 26 semanas
        })
    return pd.DataFrame(filas).sort_values("mansfield_rs", ascending=False)


# ---------------------------------------------------------------------------
# Persistencia idempotente en BigQuery
# ---------------------------------------------------------------------------
def guardar(cliente: bigquery.Client, df: pd.DataFrame, tabla: str,
            fecha_calculo: date) -> None:
    """Borra las filas de la fecha y añade las nuevas (re-ejecutable)."""
    if df.empty:
        log.info("Nada que guardar en %s.", tabla)
        return
    try:
        cliente.query(
            f"DELETE FROM `{tabla}` WHERE fecha = @f",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("f", "DATE", fecha_calculo)]
            ),
        ).result()
    except Exception:
        log.info("Tabla %s aún no existe; se creará al cargar.", tabla)
    job = cliente.load_table_from_dataframe(
        df, tabla,
        job_config=bigquery.LoadJobConfig(
            write_disposition="WRITE_APPEND",
            schema_update_options=[
                bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION
            ],
        ),
    )
    job.result()
    log.info("Guardadas %d filas en %s.", len(df), tabla)
    
def guardar_etfs(cliente: bigquery.Client, idx_w: pd.Series,
                 etfs_w: dict[str, pd.Series]) -> None:
    """Series semanales de los ETFs sectoriales + índice, para la gráfica
    de evolución del panel. Se reescribe entera en cada pasada."""
    filas = []
    series = {"S&P 500": idx_w, **etfs_w}
    for sector, serie in series.items():
        df = serie.rename("cierre").reset_index()
        df.columns = ["fecha", "cierre"]
        df["sector"] = sector
        filas.append(df)
    df_total = pd.concat(filas, ignore_index=True)
    job = cliente.load_table_from_dataframe(
        df_total, TABLA_ETFS,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE"),
    )
    job.result()
    log.info("Guardadas %d filas en %s.", len(df_total), TABLA_ETFS)

# -------------------------------------------------------------------------
# Ratios Put/Call
# -------------------------------------------------------------------------

def calcular_put_call(tickers: list[str]) -> pd.DataFrame:
    """Put/Call ratio (volumen y open interest) agregando los vencimientos
    de los próximos DIAS_VENCIMIENTO_MAX días. Solo para los candidatos
    (pocas peticiones). Devuelve NaN donde Yahoo no dé datos."""
    import time
    from datetime import datetime, timedelta

    limite = datetime.now() + timedelta(days=DIAS_VENCIMIENTO_MAX)
    filas = []
    for t in tickers:
        vol_call = vol_put = oi_call = oi_put = 0
        try:
            tk = yf.Ticker(t)
            vencimientos = [v for v in tk.options
                            if datetime.strptime(v, "%Y-%m-%d") <= limite]
            for v in vencimientos:
                cadena = tk.option_chain(v)
                vol_call += cadena.calls["volume"].fillna(0).sum()
                vol_put += cadena.puts["volume"].fillna(0).sum()
                oi_call += cadena.calls["openInterest"].fillna(0).sum()
                oi_put += cadena.puts["openInterest"].fillna(0).sum()
            time.sleep(0.5)
        except Exception as exc:
            log.warning("Sin datos de opciones para %s (%s).", t, exc)
        filas.append({
            "ticker": t,
            "pc_ratio_vol": round(vol_put / vol_call, 2) if vol_call else np.nan,
            "pc_ratio_oi": round(oi_put / oi_call, 2) if oi_call else np.nan,
            "vol_opciones": int(vol_call + vol_put),
        })
    return pd.DataFrame(filas)


# ---------------------------------------------------------------------------
def main() -> int:
    fecha_calculo = date.today()
    cliente = crear_cliente()

    componentes = cargar_componentes(cliente)
    tickers = componentes["ticker"].tolist()

    if ORIGEN_PRECIOS == "bigquery":
        precios = cargar_precios_bigquery(cliente, tickers)
    else:
        precios = cargar_precios_yfinance(tickers)
    log.info("Precios diarios: %d filas, %d tickers.",
             len(precios), precios["ticker"].nunique())

    idx_d, idx_w, etfs_w = cargar_indice_y_etfs()
    etapas = cargar_etapas(cliente)

    candidatos = calcular_screener(precios, componentes, idx_d, etapas, fecha_calculo)
    sectores = calcular_sectores(idx_w, etfs_w, fecha_calculo)
    if not candidatos.empty:
        log.info("Consultando opciones de %d candidatos...", len(candidatos))
        pc = calcular_put_call(candidatos["ticker"].tolist())
        candidatos = candidatos.merge(pc, on="ticker", how="left")
        candidatos["senal_pc"] = (
            (candidatos["pc_ratio_vol"] <= PC_RATIO_UMBRAL)
            & (candidatos["vol_opciones"] >= PC_VOLUMEN_MIN)
        )
    log.info("Consultando put/call del índice (SPY)...")
    pc_indice = calcular_put_call(["SPY"])
    pc_indice["fecha"] = fecha_calculo
    guardar(cliente, pc_indice, TABLA_PC_INDICE, fecha_calculo)
    guardar(cliente, candidatos, TABLA_CANDIDATOS, fecha_calculo)
    guardar(cliente, sectores, TABLA_SECTORES, fecha_calculo)
    guardar_etfs(cliente, idx_w, etfs_w)


    finalistas = candidatos[candidatos["pasa_todo"]] if not candidatos.empty else candidatos
    log.info("Resumen semanal: %d candidatos F1-F3, de ellos %d finalistas (F4).",
             len(candidatos), len(finalistas))
    if not finalistas.empty:
        log.info("Finalistas: %s", ", ".join(finalistas["ticker"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

