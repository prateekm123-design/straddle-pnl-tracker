import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from nse import NSE
from streamlit_autorefresh import st_autorefresh # <--- ADD THIS

# --- 1. NSE Client ---
NSE_CACHE_DIR = Path("/tmp/nse_cache")
NSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


@st.cache_resource
def get_nse_client():
  return NSE(download_folder=NSE_CACHE_DIR)


nse_client = get_nse_client()
st.sidebar.header("Data Source")
st.sidebar.success("Using NSE public data (free, no login)")


# --- 2. Expiry Extractor ---
@st.cache_data(ttl=3600)
def get_available_expiries():
  try:
    chain = nse_client.optionChain("HDFCBANK")
    raw_dates = chain.get("records", {}).get("expiryDates", [])
    parsed = sorted(
        datetime.strptime(d, "%d-%b-%Y") for d in raw_dates
    )
    return [d.strftime("%Y-%m-%d") for d in parsed]
  except Exception as e:
    st.sidebar.error(f"Could not fetch expiry list from NSE: {e}")
    return ["2026-09-29"]


available_expiries = get_available_expiries()

# --- 3. Sidebar Selection ---
st.sidebar.header("Option Settings")
selected_expiry = st.sidebar.selectbox(
    "Select Expiry Date", options=available_expiries
)


# --- 4. Load Trade Book ---
@st.cache_data
def load_trade_book():
  df = pd.read_excel("Stock options.xlsx", sheet_name="Stock")
  return df[["Instrument", "Strike", "Call/Put", "Qty", "Entry"]]


# --- 5. Heatmap Styling ---
def apply_heatmap(val):
  if pd.isna(val) or isinstance(val, str):
    return ""
  if val >= 50:
    return "background-color: #1b5e20; color: white;"
  elif val >= 40:
    return "background-color: #2e7d32; color: white;"
  elif val >= 30:
    return "background-color: #388e3c; color: white;"
  elif val >= 20:
    return "background-color: #4caf50; color: black;"
  elif val >= 10:
    return "background-color: #81c784; color: black;"
  elif val <= -100:
    return "background-color: #b71c1c; color: white;"
  elif val <= -80:
    return "background-color: #c62828; color: white;"
  elif val <= -60:
    return "background-color: #d32f2f; color: white;"
  elif val <= -40:
    return "background-color: #f44336; color: white;"
  elif val <= -20:
    return "background-color: #e57373; color: black;"
  return ""


def classify_pnl(val):
  if pd.isna(val) or isinstance(val, str):
    return ""
  if val >= 50:
    return "Profit to be booked"
  elif val >= 40:
    return "Profit 40"
  elif val >= 30:
    return "Profit 30"
  elif val >= 20:
    return "Profit 20"
  elif val >= 10:
    return "Profit 10"
  elif val >= 0:
    return "Profit"
  elif val > -20:
    return "Loss"
  elif val > -40:
    return "Loss -20"
  elif val > -60:
    return "Loss -40"
  elif val > -80:
    return "Loss -60"
  elif val > -100:
    return "Loss -80"
  else:
    return "Loss to be booked"


def fetch_chain_for_symbol(symbol, expiry_dt):
  try:
    chain = nse_client.optionChain(symbol, expiry_date=expiry_dt)
    data = chain.get("records", {}).get("data", [])
    price_map = {}
    for item in data:
      strike = item.get("strikePrice")
      if strike is None:
        continue
      for opt_type in ("CE", "PE"):
        leg = item.get(opt_type)
        if leg:
          price_map[(round(float(strike), 2), opt_type)] = leg.get("lastPrice", 0.0)
    return {"ok": True, "prices": price_map}
  except Exception as e:
    return {"ok": False, "detail": str(e)}


st.header("Straddle PnL Dashboard")
st.caption(f"Active Expiry: **{selected_expiry}** | Auto-refreshing every 5 minutes")


# --- 6. Auto-Refreshing Pricing Engine ---

# This triggers an immediate run on first load, then reruns the whole script every 5 minutes (300,000 milliseconds)
st_autorefresh(interval=5 * 60 * 1000, key="straddle_autorefresh")

# Notice we removed the @st.fragment decorator entirely
def render_pricing_table():
  col1, col2 = st.columns([1, 4])
  with col1:
    if st.button("Refresh Now"):
      st.rerun() # Removed scope="fragment"

  trades_df = load_trade_book()
  trades_df["LTP"] = 0.0
  trades_df["PnL %"] = 0.0

  status_text = st.empty()

  expiry_dt = datetime.strptime(selected_expiry, "%Y-%m-%d")
  unique_symbols = sorted(
      trades_df["Instrument"].astype(str).str.strip().str.upper().unique()
  )

  symbol_price_maps = {}
  failed_symbols = []

  for i, symbol in enumerate(unique_symbols):
    status_text.text(f"Fetching {symbol} chain ({i + 1}/{len(unique_symbols)})...")
    result = fetch_chain_for_symbol(symbol, expiry_dt)

    if result["ok"]:
      symbol_price_maps[symbol] = result["prices"]
    else:
      failed_symbols.append(f"{symbol}: {result['detail']}")

    if i < len(unique_symbols) - 1:
      time.sleep(0.6)

  if failed_symbols:
    st.error("Failed to fetch some symbols:\n\n" + "\n".join(failed_symbols))

  unmatched = []

  for index, row in trades_df.iterrows():
    instrument = str(row["Instrument"]).strip().upper()

    try:
      strike = round(float(row["Strike"]), 2)
    except (ValueError, TypeError):
      unmatched.append(f"{instrument} (bad strike: {row['Strike']})")
      continue

    opt_type = "CE" if str(row["Call/Put"]).strip().lower() == "call" else "PE"

    price_map = symbol_price_maps.get(instrument)
    if price_map is None:
      continue

    ltp = price_map.get((strike, opt_type))
    if ltp is None:
      unmatched.append(f"{instrument} {strike} {opt_type}")
      continue

    trades_df.at[index, "LTP"] = ltp
    entry = trades_df.at[index, "Entry"]
    if entry and entry > 0:
      trades_df.at[index, "PnL %"] = ((entry - ltp) / entry) * 100

  if unmatched:
    st.warning("Could not match some rows for expiry " + selected_expiry + ": " + ", ".join(unmatched))

  straddle_rows = []

  for (instrument, strike), group in trades_df.groupby(["Instrument", "Strike"]):
    call = group[group["Call/Put"].str.strip().str.lower() == "call"]
    put = group[group["Call/Put"].str.strip().str.lower() == "put"]

    entry_call = float(call["Entry"].iloc[0]) if not call.empty else 0.0
    entry_put = float(put["Entry"].iloc[0]) if not put.empty else 0.0
    ltp_call = float(call["LTP"].iloc[0]) if not call.empty else 0.0
    ltp_put = float(put["LTP"].iloc[0]) if not put.empty else 0.0
    qty_call = float(call["Qty"].iloc[0]) if not call.empty else 0.0
    qty_put = float(put["Qty"].iloc[0]) if not put.empty else 0.0

    combined_entry = entry_call + entry_put
    combined_ltp = ltp_call + ltp_put
    pnl_points = combined_entry - combined_ltp
    pnl_pct = (pnl_points / combined_entry * 100) if combined_entry > 0 else 0.0
    pnl_amount = (entry_call - ltp_call) * qty_call + (entry_put - ltp_put) * qty_put

    qty_display = str(int(qty_call)) if qty_call == qty_put else f"{int(qty_call)}/{int(qty_put)}"

    straddle_rows.append({
        "Instrument": instrument,
        "Strike": strike,
        "Qty": qty_display,
        "Entry C": combined_entry,
        "LTP C": combined_ltp,
        "PnL C": pnl_points,
        "PnL %": pnl_pct,
        "PnL (₹)": pnl_amount,
        "Classification": classify_pnl(pnl_pct),
    })

  straddle_df = pd.DataFrame(straddle_rows)

  # Sort by PnL % descending (high to low)
  if not straddle_df.empty:
    straddle_df = straddle_df.sort_values(by="PnL %", ascending=False).reset_index(drop=True)

    # Calculate Totals
    total_pnl_rupees = straddle_df["PnL (₹)"].sum()
    avg_pnl_pct = straddle_df["PnL %"].mean()

    # 3. Append Total row
    total_row = pd.DataFrame([{
        "Instrument": "TOTAL",
        "Strike": None,       # <--- FIX: Change "-" to None
        "Qty": "-",           # (Qty is already a string column, so "-" is fine here)
        "Entry C": None,
        "LTP C": None,
        "PnL C": None,
        "PnL %": avg_pnl_pct,
        "PnL (₹)": total_pnl_rupees,
        "Classification": classify_pnl(avg_pnl_pct),
    }])
    straddle_df = pd.concat([straddle_df, total_row], ignore_index=True)

  status_text.text(f"Last updated: {datetime.now().strftime('%H:%M:%S')}")

  styled_df = straddle_df.style.map(
      apply_heatmap, subset=["PnL %"]
  ).format({
      "PnL %": "{:.2f}%",
      "Entry C": "₹{:.2f}",
      "LTP C": "₹{:.2f}",
      "PnL C": "{:.2f}",
      "PnL (₹)": "₹{:,.2f}",
  }, na_rep="-")

  st.markdown(
      """
      <style>
      [data-testid="stDataFrame"] div[data-baseweb="table"] {
          width: 100% !important;
      }
      [data-testid="stDataFrame"] table {
          width: 100% !important;
      }
      </style>
      """,
      unsafe_allow_html=True,
  )

  # Calculate exact height to fit all rows (approx 35px per row + 40px for header)
  dynamic_height = (len(straddle_df) + 1) * 35 + 10

  st.dataframe(
      styled_df, 
      use_container_width=True, 
      height=dynamic_height, 
      hide_index=True
  )

# Call the function normally
render_pricing_table()