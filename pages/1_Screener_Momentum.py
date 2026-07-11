# -*- coding: utf-8 -*-
"""
pages/1_Screener_Momentum.py — Página del screener de momentum
================================================================
Lee las tablas generadas por screener.py (screener_candidatos, sectores_rs)
más stage_indice y precios_semanales, y monta la vista en cascada:

  1. Cabecera: semáforo de régimen (etapa del S&P 500) + métricas
  2. Panel sectorial: Mansfield RS con conmutadores (preselección RS > 0)
  3. Tabla de candidatos filtrada por sectores activos
  4. Detalle: velas semanales + MM30 del ticker seleccionado

Todo se sirve desde BigQuery; no hay llamadas a yfinance en tiempo de consulta.
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.cloud import bigquery
from google.oauth2 import service_account

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
PROYECTO = "panel-weinstein"
DATASET = "retiro"

TABLA_CANDIDATOS = f"{PROYECTO}.{DATASET}.screener_candidatos"
TABLA_SECTORES = f"{PROYECTO}.{DATASET}.sectores_rs"
TABLA_STAGE_INDICE = f"{PROYECTO}.{DATASET}.stage_indice"
TABLA_PRECIOS_SEM = f"{PROYECTO}.{DATASET}.precios_semanales"

# Valor de la columna `indice` en stage_indice que corresponde al S&P 500.
# AJUSTAR si en tu tabla se llama de otra forma ("SP500", "^GSPC", "GSPC"...).
#INDICE_REGIMEN = "^GSPC"
INDICE_REGIMEN = "SP500"

ETAPAS_TEXTO = {1: "Etapa 1 · Suelo", 2: "Etapa 2 · Avance",
                3: "Etapa 3 · Techo", 4: "Etapa 4 · Declive"}


# ---------------------------------------------------------------------------
# Conexión — IGUALAR al patrón de autenticación de tu app actual.
# ---------------------------------------------------------------------------
@st.cache_resource
def crear_cliente() -> bigquery.Client:
    """Cliente BigQuery. En Streamlit Cloud: credenciales desde st.secrets.
    En local: credenciales por defecto (GOOGLE_APPLICATION_CREDENTIALS).

    NOTA: sustituye "gcp_service_account" por la clave que use tu
    panel_weinstein.py si es distinta — copia sus líneas tal cual.
    """
    if "gcp_service_account" in st.secrets:
        cred = service_account.Credentials.from_service_account_info(
            dict(st.secrets["gcp_service_account"])
        )
        return bigquery.Client(project=PROYECTO, credentials=cred)
    return bigquery.Client(project=PROYECTO)


@st.cache_data(ttl=3600)
def consultar(sql: str) -> pd.DataFrame:
    return crear_cliente().query(sql).to_dataframe()


# ---------------------------------------------------------------------------
# Cargas
# ---------------------------------------------------------------------------
def fechas_disponibles() -> list:
    df = consultar(
        f"SELECT DISTINCT fecha FROM `{TABLA_CANDIDATOS}` ORDER BY fecha DESC"
    )
    return df["fecha"].tolist()


def cargar_candidatos(fecha) -> pd.DataFrame:
    return consultar(f"""
        SELECT * FROM `{TABLA_CANDIDATOS}`
        WHERE fecha = '{fecha}'
        ORDER BY pasa_todo DESC, exceso_3m DESC
    """)


def cargar_sectores(fecha) -> pd.DataFrame:
    df = consultar(f"""
        SELECT sector, etf, mansfield_rs, ret_3m, ret_6m
        FROM `{TABLA_SECTORES}`
        WHERE fecha = (SELECT MAX(fecha) FROM `{TABLA_SECTORES}`
                       WHERE fecha <= '{fecha}')
        ORDER BY mansfield_rs DESC
    """)
    return df


def cargar_regimen() -> pd.Series | None:
    df = consultar(f"""
        SELECT * FROM `{TABLA_STAGE_INDICE}`
        WHERE indice = '{INDICE_REGIMEN}'
        ORDER BY fecha DESC LIMIT 1
    """)
    return df.iloc[0] if not df.empty else None


def cargar_precios_ticker(ticker: str) -> pd.DataFrame:
    df = consultar(f"""
        SELECT fecha, apertura, maximo, minimo, cierre, volumen
        FROM `{TABLA_PRECIOS_SEM}`
        WHERE ticker = '{ticker}'
        ORDER BY fecha
    """)
    df["fecha"] = pd.to_datetime(df["fecha"])
    df["mm30"] = df["cierre"].rolling(30).mean()
    return df


# ---------------------------------------------------------------------------
# Página
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Screener Momentum", page_icon="🎯", layout="wide")
st.title("Screener de momentum")

fechas = fechas_disponibles()
if not fechas:
    st.warning("Aún no hay datos del screener. Se generan cada sábado "
               "con el workflow semanal.")
    st.stop()

fecha_sel = st.selectbox("Semana", fechas, index=0,
                         format_func=lambda f: f.strftime("%d/%m/%Y"))

candidatos = cargar_candidatos(fecha_sel)
sectores = cargar_sectores(fecha_sel)
regimen = cargar_regimen()

# --- 1. Cabecera: semáforo + métricas -------------------------------------
finalistas = candidatos[candidatos["pasa_todo"]]
sala_espera = candidatos[~candidatos["pasa_todo"]]

c1, c2, c3, c4 = st.columns(4)

if regimen is not None:
    etapa_num = int(regimen["etapa"])
    etapa_txt = ETAPAS_TEXTO.get(etapa_num, f"Etapa {regimen['etapa']}")
    if etapa_num == 2:
        c1.success(f"**S&P 500 — {etapa_txt}**\n\nScreener plenamente operativo.")
    elif etapa_num in (1, 3):
        c1.warning(f"**S&P 500 — {etapa_txt}**\n\nPrudencia: régimen de transición.")
    else:
        c1.error(f"**S&P 500 — {etapa_txt}**\n\nCandidatos en cuarentena: "
                 "el índice no acompaña.")
else:
    c1.info("Sin dato de etapa del índice (revisar INDICE_REGIMEN).")

c2.metric("Candidatos F1-F3", len(candidatos))
c3.metric("Finalistas (F4)", len(finalistas))
c4.metric("Sala de espera", len(sala_espera),
          help="Pasan liquidez, retorno y fuerza relativa, pero el RSI "
               "está fuera de la zona 40-65: fuertes pero extendidos.")
with st.expander("¿Qué significa cada filtro?"):
    st.markdown("""
| Filtro | Criterio | Qué busca |
|---|---|---|
| **F1 · Liquidez** | Precio > 8$ y volumen medio 20 sesiones > 1M | Valores donde operan institucionales |
| **F2 · Retorno absoluto** | +25% en 100 sesiones y +50% en 200 | Tendencia alcista consolidada (Etapa 2) |
| **F3 · Fuerza relativa (RS)** | Batir al S&P 500 en +15% a 6 meses y +30% a 3 | **Aceleración** frente al mercado, no solo fortaleza |
| **F4 · RSI 40-65** | Oscilador de Wilder en zona neutra | El descanso: consolidación tras el impulso, ni caída libre ni sobrecompra |

**Finalista** = pasa los cuatro. **Sala de espera** = pasa F1-F3 pero su RSI está fuera de zona: fuerte pero extendido; puede entrar en zona las próximas semanas.

*Ojo: F3 (RS, fuerza relativa contra el índice) y F4 (RSI, oscilador) son cosas distintas pese al nombre parecido.*
""")

st.divider()

# --- 2. Panel sectorial -----------------------------------------------------
st.subheader("Sectores por fuerza relativa Mansfield")

hueco_grafica = st.container()   # la gráfica irá aquí, sobre los toggles

conteo_sector = candidatos.groupby("sector")["ticker"].count()

if "sectores_activos" not in st.session_state:
    st.session_state.sectores_activos = {
        row["sector"]: bool(row["mansfield_rs"] > 0)
        for _, row in sectores.iterrows()
    }

cols = st.columns(4)
for i, (_, row) in enumerate(sectores.iterrows()):
    sector = row["sector"]
    n = int(conteo_sector.get(sector, 0))
    etiqueta = (f"{sector} · RS {row['mansfield_rs']:+.1f}"
                + (f" · {n} cand." if n else ""))
    st.session_state.sectores_activos[sector] = cols[i % 4].toggle(
        etiqueta,
        value=st.session_state.sectores_activos.get(sector, False),
        key=f"tg_{sector}",
    )

activos = [s for s, v in st.session_state.sectores_activos.items() if v]

# --- Gráfica de evolución (base 100), reactiva a los toggles ---------------
def cargar_etfs_semanales() -> pd.DataFrame:
    try:
        df = consultar(f"SELECT fecha, sector, cierre "
                       f"FROM `{PROYECTO}.{DATASET}.etfs_semanales` "
                       f"ORDER BY fecha")
        df["fecha"] = pd.to_datetime(df["fecha"])
        return df
    except Exception:
        return pd.DataFrame()

evol = cargar_etfs_semanales()
with hueco_grafica:
    if evol.empty:
        st.info("La gráfica de evolución se activará tras la próxima pasada "
                "del screener (requiere la tabla etfs_semanales).")
    else:
        ventana = st.radio("Ventana", ["6 meses", "1 año", "2 años"],
                           index=1, horizontal=True)
        semanas = {"6 meses": 26, "1 año": 52, "2 años": 104}[ventana]
        corte = evol["fecha"].max() - pd.Timedelta(weeks=semanas)
        vista_evol = evol[evol["fecha"] >= corte]

        fig_evol = go.Figure()
        for sector in ["S&P 500"] + activos:
            serie = vista_evol[vista_evol["sector"] == sector]
            if serie.empty:
                continue
            base = serie["cierre"].iloc[0]
            fig_evol.add_trace(go.Scatter(
                x=serie["fecha"], y=serie["cierre"] / base * 100,
                name=sector,
                line=dict(width=3.5, color="#444444", dash="dot")
                     if sector == "S&P 500" else dict(width=1.8),
            ))
        fig_evol.update_layout(
            height=380, yaxis_title="Base 100",
            legend=dict(orientation="h", y=-0.15),
            margin=dict(t=10, b=10),
        )
        st.plotly_chart(fig_evol, use_container_width=True)
        

# --- 3. Tabla de candidatos --------------------------------------------------
st.subheader("Candidatos")

mostrar_espera = st.checkbox("Incluir sala de espera", value=False)
vista = candidatos if mostrar_espera else finalistas
vista = vista[vista["sector"].isin(activos)]

if vista.empty:
    st.info("Ningún candidato en los sectores activos esta semana.")
else:
    st.dataframe(
        vista[["ticker", "sector", "precio", "ret_100d", "ret_200d",
               "exceso_6m", "exceso_3m", "rsi_14", "pasa_todo"]],
        hide_index=True,
        use_container_width=True,
        column_config={
            "ticker": "Ticker",
            "sector": "Sector",
            "precio": st.column_config.NumberColumn("Precio", format="$%.2f"),
            "ret_100d": st.column_config.NumberColumn("R. 100d", format="percent"),
            "ret_200d": st.column_config.NumberColumn("R. 200d", format="percent"),
            "exceso_6m": st.column_config.NumberColumn("Exceso 6m", format="percent"),
            "exceso_3m": st.column_config.NumberColumn("Exceso 3m", format="percent"),
            "rsi_14": st.column_config.NumberColumn("RSI", format="%.0f"),
            "pasa_todo": st.column_config.CheckboxColumn("Finalista"),
        },
    )

    # --- 4. Detalle -----------------------------------------------------------
    st.subheader("Detalle")
    ticker_sel = st.selectbox("Valor", vista["ticker"].tolist())

    precios = cargar_precios_ticker(ticker_sel)
    if precios.empty:
        st.info(f"Sin histórico semanal de {ticker_sel} en {TABLA_PRECIOS_SEM}.")
    else:
        precios = precios.tail(104)  # dos años de velas
        fig = go.Figure()
        fig.add_trace(go.Candlestick(
            x=precios["fecha"], open=precios["apertura"],
            high=precios["maximo"], low=precios["minimo"],
            close=precios["cierre"], name=ticker_sel,
        ))
        fig.add_trace(go.Scatter(
            x=precios["fecha"], y=precios["mm30"],
            name="MM30 semanas", line=dict(width=2),
        ))
        fila = candidatos[candidatos["ticker"] == ticker_sel].iloc[0]
        fig.update_layout(
            title=(f"{ticker_sel} · semanal — RSI {fila['rsi_14']:.0f} · "
                   f"exceso 3m {fila['exceso_3m']:+.0%}"),
            xaxis_rangeslider_visible=False,
            height=520,
            legend=dict(orientation="h", y=1.02),
        )
        st.plotly_chart(fig, use_container_width=True)

        st.caption(
            "Buscar: base de consolidación con volumen decreciente y "
            "posible rotura por encima del máximo de la base con volumen. "
            "El screener selecciona; la entrada la decide el gráfico."
        )
