"""
panel_weinstein_cloud.py
---------------------------
Versión PÚBLICA del panel (pensada para Streamlit Community Cloud).
Misma visualización que panel_weinstein.py, pero lee los datos de
BigQuery en vez de conectarse directamente a tu MariaDB del NAS — así
la app no depende de que tu PC o tu NAS estén encendidos.

Las credenciales de Google Cloud se leen de st.secrets, NUNCA del
código ni de un fichero subido al repositorio. En local, se configuran
en .streamlit/secrets.toml (que añadiremos a .gitignore); en Streamlit
Community Cloud, se pegan en el panel de "Secrets" de la app.

Requisitos (instalar con py -m pip install ...):
    streamlit
    pandas
    plotly
    google-cloud-bigquery
    db-dtypes
"""

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from google.cloud import bigquery
from google.oauth2 import service_account

# ------------------------------------------------------------------
# CONFIGURACIÓN
# ------------------------------------------------------------------
ID_PROYECTO_GCP = "panel-weinstein"
DATASET_BIGQUERY = "retiro"

NOMBRES_ETAPA = {
    1: "Etapa 1 — Base / Acumulación",
    2: "Etapa 2 — Avance / Tendencia alcista",
    3: "Etapa 3 — Techo / Distribución",
    4: "Etapa 4 — Declive / Tendencia bajista",
}
COLOR_ETAPA = {1: "#94a3b8", 2: "#16a34a", 3: "#f59e0b", 4: "#dc2626"}


# ------------------------------------------------------------------
# Carga de datos desde BigQuery (con caché de 1 hora)
# ------------------------------------------------------------------
@st.cache_data(ttl=3600)
def conectar_bigquery() -> bigquery.Client:
    credenciales = service_account.Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"])
    )
    return bigquery.Client(credentials=credenciales, project=ID_PROYECTO_GCP)


@st.cache_data(ttl=3600)
def cargar_datos():
    cliente = conectar_bigquery()
    tabla_amplitud = f"`{ID_PROYECTO_GCP}.{DATASET_BIGQUERY}.indicadores_amplitud`"
    tabla_stage = f"`{ID_PROYECTO_GCP}.{DATASET_BIGQUERY}.stage_indice`"
    tabla_m2 = f"`{ID_PROYECTO_GCP}.{DATASET_BIGQUERY}.m2_liquidez`"
    tabla_putcall = f"`{ID_PROYECTO_GCP}.{DATASET_BIGQUERY}.putcall_indice`"

    amplitud = cliente.query(f"SELECT * FROM {tabla_amplitud} ORDER BY fecha").to_dataframe()
    stage = cliente.query(
        f"SELECT * FROM {tabla_stage} WHERE indice = 'SP500' ORDER BY fecha"
    ).to_dataframe()

    try:
        m2 = cliente.query(f"SELECT * FROM {tabla_m2} ORDER BY fecha").to_dataframe()
        m2["fecha"] = pd.to_datetime(m2["fecha"])
    except Exception:
        m2 = pd.DataFrame()

    try:
        putcall = cliente.query(
            f"SELECT * FROM {tabla_putcall} ORDER BY fecha DESC LIMIT 4"
        ).to_dataframe()
        putcall["fecha"] = pd.to_datetime(putcall["fecha"])
    except Exception:
        putcall = pd.DataFrame()

    amplitud["fecha"] = pd.to_datetime(amplitud["fecha"])
    stage["fecha"] = pd.to_datetime(stage["fecha"])
    return amplitud, stage, m2, putcall


# ------------------------------------------------------------------
# Configuración de página
# ------------------------------------------------------------------
st.set_page_config(page_title="Panel Weinstein", page_icon="📈", layout="wide")
st.title("📈 Panel de Mercado — Método Weinstein")
st.caption("S&P 500 · Indicadores de amplitud del capítulo 8")

amplitud, stage, m2, putcall = cargar_datos()

if amplitud.empty or stage.empty:
    st.warning("Todavía no hay datos sincronizados en BigQuery. Vuelve a intentarlo más tarde.")
    st.stop()

ultima_m2 = m2.iloc[-1] if not m2.empty else None
anterior_m2 = m2.iloc[-2] if len(m2) > 1 else ultima_m2

ultima_amplitud = amplitud.iloc[-1]
ultima_stage = stage.iloc[-1]
anterior_amplitud = amplitud.iloc[-2] if len(amplitud) > 1 else ultima_amplitud


# ------------------------------------------------------------------
# CABECERA: "de un vistazo"
# ------------------------------------------------------------------
col1, col2, col3, col4, col5 = st.columns(5)

etapa_actual = ultima_stage["etapa"]
etapa_texto = NOMBRES_ETAPA.get(etapa_actual, "Sin clasificar (media plana)")
etapa_color = COLOR_ETAPA.get(etapa_actual, "#6b7280")

with col1:
    st.markdown("**Etapa actual del S&P 500**")
    st.markdown(
        f"<h3 style='color:{etapa_color}'>{etapa_texto}</h3>",
        unsafe_allow_html=True,
    )
    confirmacion = ultima_stage.get("confirmado_volumen")
    if pd.notna(confirmacion):
        if confirmacion:
            st.caption("✅ Ruptura confirmada por volumen alto")
        else:
            st.caption("⚠️ Ruptura con volumen débil — confirmación dudosa")

with col2:
    st.metric(
        "% acciones sobre su media de 30 semanas",
        f"{ultima_amplitud['pct_sobre_media_30s']:.1f}%",
        delta=f"{ultima_amplitud['pct_sobre_media_30s'] - anterior_amplitud['pct_sobre_media_30s']:.1f} pts vs. semana anterior",
    )

with col3:
    diferencial = int(ultima_amplitud["nuevos_maximos_52s"] - ultima_amplitud["nuevos_minimos_52s"])
    st.metric(
        "Nuevos máximos − Nuevos mínimos (52 sem.)",
        diferencial,
        delta=f"{int(ultima_amplitud['nuevos_maximos_52s'])} máx. / {int(ultima_amplitud['nuevos_minimos_52s'])} mín.",
    )

with col4:
    avances_ultima = int(ultima_amplitud["avances"])
    declives_ultima = int(ultima_amplitud["declives"])
    saldo_ultima = avances_ultima - declives_ultima
    tendencia_ad = amplitud["linea_avance_declive"].iloc[-1] > amplitud["linea_avance_declive"].iloc[-5]

    cambio_indice = stage["cierre"].iloc[-1] - stage["cierre"].iloc[-2] if len(stage) > 1 else 0
    divergencia = (cambio_indice > 0 and not tendencia_ad) or (cambio_indice < 0 and tendencia_ad)

    st.markdown("**Línea Avance/Declive**")
    col4a, col4b = st.columns(2)
    with col4a:
        st.markdown(f"🟢 **{avances_ultima}** suben")
        st.markdown(f"🔴 **{declives_ultima}** bajan")
    with col4b:
        saldo_txt = f"+{saldo_ultima}" if saldo_ultima >= 0 else str(saldo_ultima)
        saldo_color = "#16a34a" if saldo_ultima >= 0 else "#dc2626"
        st.markdown(f"**Saldo:** <span style='color:{saldo_color}'>{saldo_txt}</span>", unsafe_allow_html=True)
        tendencia_txt = "↑ Subiendo" if tendencia_ad else "↓ Bajando"
        st.markdown(f"**Tendencia 5s:** {tendencia_txt}")
    if divergencia:
        st.warning("⚠️ Divergencia: índice y A/D van en sentido contrario")

with col5:
    st.markdown("**Liquidez M2 (USA)**")
    if ultima_m2 is not None:
        yoy = float(ultima_m2["m2_yoy_pct"]) if pd.notna(ultima_m2["m2_yoy_pct"]) else None
        tendencia_m2 = str(ultima_m2["tendencia"]) if pd.notna(ultima_m2["tendencia"]) else "—"
        m2_val = float(ultima_m2["m2_billones"])
        color_m2 = "#16a34a" if (yoy is not None and yoy > 0) else "#dc2626"
        tendencia_emoji = "↑" if tendencia_m2 == "SUBIENDO" else ("↓" if tendencia_m2 == "BAJANDO" else "→")
        yoy_txt = f"{yoy:+.1f}%" if yoy is not None else "N/D"
        st.markdown(
            f"<h3 style='color:{color_m2}'>{yoy_txt} YoY</h3>",
            unsafe_allow_html=True,
        )
        st.caption(f"{tendencia_emoji} {tendencia_m2.capitalize()} · {m2_val:.1f} B$")
        st.caption(f"Dato a: {ultima_m2['fecha'].date()}")
    else:
        st.caption("Sin datos de M2 todavía")

st.caption(f"Datos a fecha de la última semana cargada: {ultima_amplitud['fecha'].date()}")
st.divider()


# ------------------------------------------------------------------
# GRÁFICO 1: S&P 500 con media de 30 semanas, coloreado por etapa
# ------------------------------------------------------------------
st.subheader("S&P 500 — Precio, media de 30 semanas y etapa")

fig_precio = go.Figure()
fig_precio.add_trace(go.Scatter(
    x=stage["fecha"], y=stage["cierre"], name="Cierre semanal",
    line=dict(color="#1f2937", width=1.5),
))
fig_precio.add_trace(go.Scatter(
    x=stage["fecha"], y=stage["media_30s"], name="Media 30 semanas",
    line=dict(color="#3b82f6", width=2, dash="dot"),
))
for etapa_num, color in COLOR_ETAPA.items():
    subset = stage[stage["etapa"] == etapa_num]
    if not subset.empty:
        fig_precio.add_trace(go.Scatter(
            x=subset["fecha"], y=subset["cierre"], mode="markers",
            marker=dict(color=color, size=4),
            name=NOMBRES_ETAPA[etapa_num], showlegend=True,
        ))
fig_precio.update_layout(height=420, hovermode="x unified", legend=dict(orientation="h", y=-0.2))
st.plotly_chart(fig_precio, use_container_width=True)


# ------------------------------------------------------------------
# GRÁFICO 2: % de acciones sobre su media de 30 semanas
# ------------------------------------------------------------------
st.subheader("% de acciones del S&P 500 sobre su media de 30 semanas")
st.caption("Por encima del 70% suele señalar sobrecompra de mercado; por debajo del 30%, sobreventa.")

fig_pct = go.Figure()
fig_pct.add_trace(go.Scatter(
    x=amplitud["fecha"], y=amplitud["pct_sobre_media_30s"],
    fill="tozeroy", line=dict(color="#16a34a"), name="% sobre media 30s",
))
fig_pct.add_hline(y=70, line_dash="dash", line_color="#dc2626", annotation_text="Sobrecompra (70%)")
fig_pct.add_hline(y=30, line_dash="dash", line_color="#dc2626", annotation_text="Sobreventa (30%)")
fig_pct.update_layout(height=350, yaxis_range=[0, 100])
st.plotly_chart(fig_pct, use_container_width=True)


# ------------------------------------------------------------------
# GRÁFICO 3: Nuevos máximos vs. nuevos mínimos
# ------------------------------------------------------------------
st.subheader("Nuevos máximos vs. nuevos mínimos de 52 semanas")

fig_hl = go.Figure()
fig_hl.add_trace(go.Bar(
    x=amplitud["fecha"], y=amplitud["nuevos_maximos_52s"],
    name="Nuevos máximos", marker_color="#16a34a",
))
fig_hl.add_trace(go.Bar(
    x=amplitud["fecha"], y=-amplitud["nuevos_minimos_52s"],
    name="Nuevos mínimos", marker_color="#dc2626",
))
fig_hl.update_layout(height=320, barmode="relative", bargap=0.1)
st.plotly_chart(fig_hl, use_container_width=True)


# ------------------------------------------------------------------
# GRÁFICO 4: Línea Avance/Declive vs. precio del índice (divergencias)
# ------------------------------------------------------------------
st.subheader("Línea Avance/Declive vs. precio del índice")
st.caption(
    "Lo relevante de este oscilador no es su valor absoluto, sino sus divergencias "
    "con el precio: cerca de un giro de Etapa 3 a Etapa 4, el dinero suele rotar "
    "hacia los valores más fiables, por lo que el índice puede seguir marcando "
    "máximos mientras el saldo de avances menos declives ya es negativo. "
    "Compárala siempre junto al precio, no de forma aislada."
)

# Unimos precio del índice, línea A/D y el desglose avances/declives
df_combo = pd.merge(
    amplitud[["fecha", "linea_avance_declive", "avances", "declives"]],
    stage[["fecha", "cierre"]],
    on="fecha", how="inner",
).sort_values("fecha")

df_combo["cambio_indice"] = df_combo["cierre"].diff()
df_combo["saldo"] = df_combo["avances"] - df_combo["declives"]

# Preparamos textos del tooltip ya formateados (con su signo y su "color" en emoji,
# porque Plotly no permite colorear texto del tooltip de forma fiable)
df_combo["avances_txt"] = "🟢 " + df_combo["avances"].astype(int).astype(str)
df_combo["declives_txt"] = "🔴 " + df_combo["declives"].astype(int).astype(str)
df_combo["saldo_txt"] = df_combo["saldo"].apply(
    lambda v: f"🟢 +{int(v)}" if v >= 0 else f"🔴 {int(v)}"
)
df_combo["cambio_txt"] = df_combo["cambio_indice"].apply(
    lambda v: "N/D" if pd.isna(v) else (f"🟢 +{v:.0f} pts" if v >= 0 else f"🔴 {v:.0f} pts")
)

fechas = df_combo["fecha"].tolist()
valores_ad = df_combo["linea_avance_declive"].tolist()

fig_combo = make_subplots(specs=[[{"secondary_y": True}]])

# Precio del índice, en azul oscuro, en el eje principal (sin tooltip propio,
# lo sustituimos por el tooltip combinado de abajo)
fig_combo.add_trace(
    go.Scatter(
        x=df_combo["fecha"], y=df_combo["cierre"],
        name="S&P 500 (precio)", line=dict(color="#1e3a8a", width=2),
        hoverinfo="skip",
    ),
    secondary_y=False,
)

# Capa invisible que concentra el tooltip combinado: empresas que suben/bajan,
# saldo avance-declive, y variación de puntos del índice esa semana
fig_combo.add_trace(
    go.Scatter(
        x=df_combo["fecha"], y=df_combo["cierre"],
        mode="markers", marker=dict(size=8, color="rgba(0,0,0,0)"),
        customdata=df_combo[["avances_txt", "declives_txt", "saldo_txt", "cambio_txt"]].values,
        hovertemplate=(
            "<b>%{x|%d %b %Y}</b><br>"
            "Empresas que suben: %{customdata[0]}<br>"
            "Empresas que bajan: %{customdata[1]}<br>"
            "Saldo avance/declive: %{customdata[2]}<br>"
            "Variación S&P 500: %{customdata[3]}"
            "<extra></extra>"
        ),
        showlegend=False, name="",
    ),
    secondary_y=False,
)

# Línea A/D coloreada tramo a tramo: verde si sube, rojo si baja
VERDE, ROJO = "#16a34a", "#dc2626"
leyenda_verde_mostrada = False
leyenda_roja_mostrada = False
for i in range(1, len(valores_ad)):
    sube = valores_ad[i] >= valores_ad[i - 1]
    color = VERDE if sube else ROJO
    mostrar_leyenda = False
    nombre = ""
    if sube and not leyenda_verde_mostrada:
        mostrar_leyenda, nombre, leyenda_verde_mostrada = True, "Línea A/D (tramo alcista)", True
    elif not sube and not leyenda_roja_mostrada:
        mostrar_leyenda, nombre, leyenda_roja_mostrada = True, "Línea A/D (tramo bajista)", True

    fig_combo.add_trace(
        go.Scatter(
            x=fechas[i - 1:i + 1], y=valores_ad[i - 1:i + 1],
            mode="lines", line=dict(color=color, width=2),
            showlegend=mostrar_leyenda, name=nombre,
            legendgroup=color, hoverinfo="skip",
        ),
        secondary_y=True,
    )

fig_combo.update_yaxes(title_text="Precio S&P 500", secondary_y=False)
fig_combo.update_yaxes(title_text="Línea Avance/Declive", secondary_y=True)
fig_combo.update_layout(height=420, hovermode="x", legend=dict(orientation="h", y=-0.2))
st.plotly_chart(fig_combo, use_container_width=True)

st.divider()

# ------------------------------------------------------------------
# GRÁFICO 5: M2 (liquidez USA) vs. precio del S&P 500
# ------------------------------------------------------------------
st.subheader("Liquidez M2 (USA) vs. S&P 500")
st.caption(
    "La expansión del M2 (dinero en circulación) tiende a preceder o acompañar "
    "las subidas del mercado; su contracción suele anticipar correcciones. "
    "Observa la correlación entre ambas líneas y, especialmente, las divergencias."
)

if not m2.empty:
    m2_plot = m2.copy()
    m2_plot["fecha"] = pd.to_datetime(m2_plot["fecha"])
    stage_plot = stage[["fecha", "cierre"]].copy()
    stage_plot["fecha"] = pd.to_datetime(stage_plot["fecha"])

    fig_m2 = make_subplots(specs=[[{"secondary_y": True}]])

    fig_m2.add_trace(
        go.Scatter(
            x=stage_plot["fecha"], y=stage_plot["cierre"],
            name="S&P 500", line=dict(color="#1e3a8a", width=2),
        ),
        secondary_y=False,
    )
    fig_m2.add_trace(
        go.Scatter(
            x=m2_plot["fecha"], y=m2_plot["m2_billones"],
            name="M2 USA (billones $)", line=dict(color="#f59e0b", width=2),
            hovertemplate="<b>%{x|%d %b %Y}</b><br>M2: %{y:.1f} B$<extra></extra>",
        ),
        secondary_y=True,
    )

    fig_m2.update_yaxes(title_text="S&P 500 (puntos)", secondary_y=False)
    fig_m2.update_yaxes(title_text="M2 USA (billones $)", secondary_y=True)
    fig_m2.update_layout(
        height=380, hovermode="x unified",
        legend=dict(orientation="h", y=-0.2),
    )
    st.plotly_chart(fig_m2, use_container_width=True)
else:
    st.info("Los datos de M2 se cargarán en la próxima actualización semanal.")
st.subheader("📋 Análisis semanal")
st.caption("Texto generado automáticamente a partir de los datos. Puedes copiarlo y adaptarlo para tu newsletter o grupo de Telegram.")

fechas_disponibles = sorted(
    set(amplitud["fecha"].dt.date.tolist()) & set(stage["fecha"].dt.date.tolist()),
    reverse=True,
)

fecha_seleccionada = st.selectbox(
    "Semana a analizar:",
    options=fechas_disponibles,
    format_func=lambda f: f.strftime("%d/%m/%Y"),
    index=0,
)

fecha_sel_str = str(fecha_seleccionada)  # formato "2026-06-29"
amplitud_ord = amplitud.sort_values("fecha").reset_index(drop=True).copy()
amplitud_sel = amplitud_ord[amplitud_ord["fecha"].dt.strftime("%Y-%m-%d") <= fecha_sel_str].copy()

stage_ord = stage.sort_values("fecha").reset_index(drop=True).copy()
stage_sel = stage_ord[stage_ord["fecha"].dt.strftime("%Y-%m-%d") <= fecha_sel_str].copy()

if amplitud_sel.empty or stage_sel.empty:
    st.warning("No hay datos suficientes para la semana seleccionada.")
else:
    def generar_analisis(amplitud, stage, m2=None, putcall=None) -> str:
        ua = amplitud.iloc[-1]
        ant = amplitud.iloc[-2] if len(amplitud) > 1 else ua
        us = stage.iloc[-1]
        us_ant = stage.iloc[-2] if len(stage) > 1 else us

        fecha = ua["fecha"].strftime("%d de %B de %Y").replace(
            "January","enero").replace("February","febrero").replace("March","marzo"
            ).replace("April","abril").replace("May","mayo").replace("June","junio"
            ).replace("July","julio").replace("August","agosto").replace("September","septiembre"
            ).replace("October","octubre").replace("November","noviembre").replace("December","diciembre")

        etapa = int(us["etapa"]) if pd.notna(us["etapa"]) else None
        nombres_etapa = {
            1: "Etapa 1 (Base/Acumulación)",
            2: "Etapa 2 (Avance/Tendencia alcista)",
            3: "Etapa 3 (Techo/Distribución)",
            4: "Etapa 4 (Declive/Tendencia bajista)",
        }
        etapa_txt = nombres_etapa.get(etapa, "etapa sin clasificar")

        pct = float(ua["pct_sobre_media_30s"])
        pct_delta = pct - float(ant["pct_sobre_media_30s"])
        maximos = int(ua["nuevos_maximos_52s"])
        minimos = int(ua["nuevos_minimos_52s"])
        diferencial_hl = maximos - minimos
        avances = int(ua["avances"])
        declives = int(ua["declives"])
        saldo = avances - declives
        cambio_sp500 = float(us["cierre"]) - float(us_ant["cierre"])
        tendencia_ad_5s = (
            amplitud["linea_avance_declive"].iloc[-1] > amplitud["linea_avance_declive"].iloc[-5]
            if len(amplitud) >= 5 else True
        )
        divergencia = (cambio_sp500 > 0 and not tendencia_ad_5s) or (cambio_sp500 < 0 and tendencia_ad_5s)

        p1 = f"**Semana del {fecha}.** El S&P 500 se encuentra en {etapa_txt}, "
        if etapa == 2:
            p1 += "lo que indica que la tendencia de fondo sigue siendo alcista."
        elif etapa == 3:
            p1 += "señal de que el mercado puede estar perdiendo fuerza tras el avance reciente."
        elif etapa == 4:
            p1 += "confirmando que la tendencia dominante es bajista."
        elif etapa == 1:
            p1 += "lo que sugiere consolidación tras la caída anterior, sin tendencia clara todavía."
        else:
            p1 += "sin clasificación clara por el momento."
        variacion_txt = f"+{cambio_sp500:.0f}" if cambio_sp500 >= 0 else f"{cambio_sp500:.0f}"
        p1 += f" El índice cerró la semana con una variación de {variacion_txt} puntos respecto a la semana anterior."

        if pct >= 70:
            nivel_pct = "zona de sobrecompra (por encima del 70%)"
        elif pct >= 50:
            nivel_pct = "zona saludable (entre el 50% y el 70%)"
        elif pct >= 30:
            nivel_pct = "zona de debilidad (entre el 30% y el 50%)"
        else:
            nivel_pct = "zona de sobreventa (por debajo del 30%)"
        delta_txt = f"+{pct_delta:.1f}" if pct_delta >= 0 else f"{pct_delta:.1f}"
        p2 = (f"**Amplitud de mercado:** el {pct:.1f}% de las acciones del S&P 500 cotizan por encima "
              f"de su media de 30 semanas ({delta_txt} puntos respecto a la semana anterior), "
              f"situándose en {nivel_pct}.")

        if diferencial_hl > 50:
            lectura_hl = "señal de fortaleza interna clara"
        elif diferencial_hl > 0:
            lectura_hl = "ligero predominio alcista"
        elif diferencial_hl == 0:
            lectura_hl = "equilibrio entre compradores y vendedores"
        else:
            lectura_hl = "señal de debilidad interna"
        p3 = (f"**Nuevos máximos/mínimos de 52 semanas:** esta semana {maximos} acciones marcaron "
              f"nuevos máximos anuales frente a {minimos} que marcaron nuevos mínimos "
              f"(diferencial de {'+' if diferencial_hl >= 0 else ''}{diferencial_hl}), "
              f"lo que representa una {lectura_hl}.")

        tendencia_txt = "sigue subiendo" if tendencia_ad_5s else "lleva bajando"
        saldo_txt = f"+{saldo}" if saldo >= 0 else str(saldo)
        p4 = (f"**Línea Avance/Declive:** {avances} acciones avanzaron esta semana frente a {declives} "
              f"que retrocedieron (saldo {saldo_txt}). La línea acumulada {tendencia_txt} en las últimas 5 semanas.")
        if divergencia:
            if cambio_sp500 > 0 and not tendencia_ad_5s:
                p4 += (" ⚠️ **Atención:** el índice sube pero la línea A/D lleva semanas bajando — "
                       "divergencia bajista que históricamente ha anticipado giros de mercado.")
            elif cambio_sp500 < 0 and tendencia_ad_5s:
                p4 += (" ⚠️ **Atención:** el índice baja pero la línea A/D sigue subiendo — "
                       "divergencia alcista que puede indicar que la caída no tiene respaldo interno.")

        # --- Párrafo 5: M2 liquidez ---
        p5 = ""
        if m2 is not None and not m2.empty:
            um2 = m2.iloc[-1]
            yoy = float(um2["m2_yoy_pct"]) if pd.notna(um2["m2_yoy_pct"]) else None
            tendencia_m2 = str(um2["tendencia"]) if pd.notna(um2["tendencia"]) else None
            if yoy is not None:
                if yoy > 5:
                    lectura_m2 = "expansión significativa de liquidez, contexto históricamente favorable para la renta variable"
                elif yoy > 0:
                    lectura_m2 = "leve expansión de liquidez, contexto neutral-positivo"
                elif yoy > -3:
                    lectura_m2 = "ligera contracción de liquidez, contexto de cautela"
                else:
                    lectura_m2 = "contracción notable de liquidez, contexto históricamente desfavorable para la renta variable"
                p5 = (f"**Liquidez M2 (USA):** la oferta monetaria crece a un ritmo del {yoy:+.1f}% "
                      f"interanual y su tendencia reciente es {tendencia_m2.lower() if tendencia_m2 else 'indeterminada'}, "
                      f"lo que señala una {lectura_m2}.")

        # --- Párrafo 6: Put/Call ratio ---
        p6 = ""
        if putcall is not None and not putcall.empty:
            pc_val = float(putcall.iloc[0]["pc_ratio_vol"]) if "pc_ratio_vol" in putcall.columns else None
            if pc_val is not None:
                if pc_val > 1.5:
                    lectura_pc = (
                        f"un nivel muy elevado ({pc_val:.2f}), señal de miedo significativo: "
                        f"los inversores se están cubriendo masivamente ante posibles caídas a corto plazo. "
                        f"Este nivel, combinado con una rotación sectorial hacia valores defensivos "
                        f"(Energía, Salud), refuerza la cautela aunque el mercado siga en Etapa 2."
                    )
                elif pc_val > 1.2:
                    lectura_pc = (
                        f"un nivel elevado ({pc_val:.2f}), indicando que los inversores están "
                        f"aumentando su cobertura ante posibles zozobras a corto plazo. "
                        f"Conviene vigilar si esta señal de cautela se confirma con debilidad "
                        f"en los demás indicadores de amplitud."
                    )
                elif pc_val > 0.8:
                    lectura_pc = (
                        f"un nivel neutral ({pc_val:.2f}), sin señales de miedo ni euforia "
                        f"excesiva en el mercado de opciones."
                    )
                else:
                    lectura_pc = (
                        f"un nivel bajo ({pc_val:.2f}), reflejando cierta complacencia o euforia "
                        f"en el mercado — históricamente una señal de precaución contrarian."
                    )
                p6 = f"**Ratio Put/Call (SPY):** el mercado de opciones muestra {lectura_pc}"

        parrafos = [p1, p2, p3, p4]
        if p5:
            parrafos.append(p5)
        if p6:
            parrafos.append(p6)
        return "\n\n".join(parrafos)

    m2_sel = m2[m2["fecha"].dt.strftime("%Y-%m-%d") <= fecha_sel_str].copy() if not m2.empty else m2
    putcall_sel = putcall[putcall["fecha"].dt.strftime("%Y-%m-%d") <= fecha_sel_str].sort_values("fecha", ascending=False).head(1) if not putcall.empty else putcall
    texto_analisis = generar_analisis(amplitud_sel, stage_sel, m2_sel, putcall_sel)
    st.markdown(texto_analisis)

    texto_plano = texto_analisis.replace("**", "").replace("⚠️ ", "")
    st.components.v1.html(f"""
        <textarea id="texto_copia" style="position:absolute;left:-9999px;">{texto_plano}</textarea>
        <button onclick="
            var t = document.getElementById('texto_copia');
            t.select();
            document.execCommand('copy');
            this.innerText='✅ ¡Copiado!';
            setTimeout(() => this.innerText='📋 Copiar texto al portapapeles', 2000);
        " style="
            background:#16a34a;color:white;border:none;padding:8px 16px;
            border-radius:6px;cursor:pointer;font-size:14px;margin-top:8px;
        ">📋 Copiar texto al portapapeles</button>
    """, height=60)


st.divider()
st.caption("Panel basado en 'Secrets for Profiting in Bull and Bear Markets' de Stan Weinstein — capítulo 8.")
