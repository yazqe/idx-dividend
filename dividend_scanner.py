"""IDX Dividend Scanner — screen saham IDX dengan yield tinggi & analisis kelayakan."""
import json, sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
import yfinance as yf
import pandas as pd
import pytz

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

WIB     = pytz.timezone('Asia/Jakarta')
OUT_DIR = Path(__file__).parent / "analysis" / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SCANNER_URL = "https://scanner.tradingview.com/indonesia/scan"
HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://id.tradingview.com",
    "User-Agent": "Mozilla/5.0",
}

MIN_YIELD  = 5.0   # % minimum dividend yield
MAX_PAYOUT = 150.0 # % — IDX coal/mining sering bayar dari cadangan, threshold lebih longgar
MIN_YEARS  = 2     # minimum years of dividend history


def get_dividend_candidates(n=100):
    """Get top IDX stocks by market cap — yfinance will filter by dividend yield."""
    r = requests.post(SCANNER_URL, headers=HEADERS, json={
        "filter": [
            {"left": "market_cap_basic", "operation": "greater", "right": 500_000_000_000},  # min 500B IDR
            {"left": "volume",           "operation": "greater", "right": 200_000},
        ],
        "symbols": {"query": {"types": ["stock"]}},
        "columns": ["name", "close", "change", "volume", "sector"],
        "sort":    {"sortBy": "market_cap_basic", "sortOrder": "desc"},
        "range":   [0, n]
    }, timeout=15)
    if r.status_code != 200:
        print(f"Scanner error: {r.status_code}")
        return []
    results = []
    for item in r.json().get("data", []):
        d = item["d"]
        ticker = d[0].replace("IDX:", "")
        results.append({
            "ticker":   ticker,
            "price":    d[1],
            "yield_tv": 0,
            "dps":      0,
            "payout_tv": 0,
            "sector":   d[4] or "–",
        })
    return results


def analyze_dividend(ticker, tv_data):
    """Deep analysis using yfinance — history, growth, ex-date, sustainability."""
    try:
        stock = yf.Ticker(f"{ticker}.JK")
        info  = stock.info or {}
        divs  = stock.dividends

        price        = tv_data.get("price") or info.get("currentPrice") or 0
        dps          = tv_data.get("dps") or info.get("lastDividendValue") or 0
        # yfinance dividendYield IDX = salah (100x lipat) — hitung manual dari history
        # yfinance payoutRatio IDX juga sering salah — hitung manual dari EPS
        annual_div   = 0.0
        payout_ratio = 0.0
        sector       = tv_data.get("sector") or info.get("sector") or "–"
        mktcap       = info.get("marketCap")

        ex_div_ts   = info.get("exDividendDate")
        ex_div_date = datetime.fromtimestamp(ex_div_ts).strftime("%d %b %Y") if ex_div_ts else None

        div_history    = []
        div_growth_pct = None
        div_years      = 0
        div_consistent = False

        if divs is not None and not divs.empty:
            divs.index = pd.to_datetime(divs.index)
            annual = divs.resample("YE").sum()
            annual = annual[annual > 0]
            div_years = len(annual)
            if div_years >= 1:
                div_history = [
                    {"year": str(y.year), "amount": round(float(v), 2)}
                    for y, v in annual.items()
                ][-5:]
                annual_div = float(annual.iloc[-1])
                dps = annual_div  # pakai dari history, lebih akurat
            if div_years >= 2:
                last = float(annual.iloc[-1])
                prev = float(annual.iloc[-2])
                div_growth_pct = round((last - prev) / prev * 100, 1) if prev else None
                div_consistent = bool(all(annual > 0))

        # Hitung yield dari history (lebih akurat dari yfinance.dividendYield)
        yield_pct = round(annual_div / price * 100, 2) if price and annual_div else 0.0

        # Hitung payout ratio dari EPS jika tersedia
        eps = info.get("trailingEps") or info.get("epsTrailingTwelveMonths") or 0
        if eps and eps > 0 and annual_div > 0:
            payout_ratio = round(annual_div / eps * 100, 1)
        else:
            payout_ratio = 0.0  # tidak bisa dihitung, jangan penalty

        recommendation = _get_recommendation(
            yield_pct=yield_pct, payout_ratio=payout_ratio,
            div_growth_pct=div_growth_pct, div_years=div_years,
            div_consistent=div_consistent, ex_div_ts=ex_div_ts,
        )

        return {
            "ticker":         ticker,
            "price":          round(float(price), 0),
            "sector":         sector,
            "market_cap":     mktcap,
            "yield_pct":      round(float(yield_pct), 2),
            "dps":            round(float(dps), 2),
            "payout_ratio":   round(float(payout_ratio), 1),
            "ex_div_date":    ex_div_date,
            "div_years":      div_years,
            "div_growth_pct": div_growth_pct,
            "div_consistent": div_consistent,
            "div_history":    div_history,
            "recommendation": recommendation["action"],
            "strategy":       recommendation["strategy"],
            "reasoning":      recommendation["reasoning"],
            "rating":         recommendation["rating"],
            "warnings":       recommendation["warnings"],
            "analyzed_at":    datetime.now(WIB).strftime("%d %b %Y %H:%M WIB"),
        }

    except Exception as e:
        print(f"  ⚠️  {ticker}: {e}")
        return None


def _get_recommendation(yield_pct, payout_ratio, div_growth_pct,
                         div_years, div_consistent, ex_div_ts):
    warnings = []
    rating   = 3

    if payout_ratio > MAX_PAYOUT:
        warnings.append(f"Payout ratio {payout_ratio:.0f}% — terlalu tinggi, dividen mungkin tidak sustainable")
    if yield_pct > 20:
        warnings.append(f"Yield {yield_pct:.1f}% — terlalu tinggi, kemungkinan akan dipotong (dividend trap)")
    if div_years < MIN_YEARS:
        warnings.append(f"Riwayat dividen hanya {div_years} tahun — terlalu pendek untuk dianalisis")
    if div_growth_pct is not None and div_growth_pct < -30:
        warnings.append(f"Dividen turun {abs(div_growth_pct):.0f}% dibanding tahun lalu")

    days_to_ex = None
    if ex_div_ts:
        days_to_ex = (datetime.fromtimestamp(ex_div_ts) - datetime.now()).days

    if yield_pct >= 7:      rating += 1
    if div_years >= 5:      rating += 1
    if div_consistent:      rating += 1
    if payout_ratio > 80:   rating -= 1
    if div_growth_pct and div_growth_pct > 10: rating += 1
    if div_growth_pct and div_growth_pct < -20: rating -= 1
    if yield_pct > 20:      rating -= 2
    rating = max(1, min(5, rating))

    if yield_pct < MIN_YIELD:
        return {"action": "WATCH", "strategy": "Yield Rendah",
                "reasoning": f"Yield {yield_pct:.1f}% belum mencapai target 5%. Pantau saat harga turun.",
                "rating": rating, "warnings": warnings}

    if payout_ratio > MAX_PAYOUT or yield_pct > 20 or (div_growth_pct and div_growth_pct < -40):
        return {"action": "AVOID", "strategy": "Dividend Trap",
                "reasoning": (f"Yield tinggi ({yield_pct:.1f}%) tapi payout ratio {payout_ratio:.0f}% tidak sustainable. "
                              "Hindari — kemungkinan besar dividen akan dipotong."),
                "rating": rating, "warnings": warnings}

    if days_to_ex is not None and 0 < days_to_ex <= 14:
        return {"action": "BUY_DIVIDEND", "strategy": "Pre-Ex-Dividend",
                "reasoning": (f"Ex-dividend dalam {days_to_ex} hari! Yield {yield_pct:.1f}% menarik. "
                              "Beli sebelum ex-date untuk dapat dividen. Waspadai penurunan harga setelah ex-div."),
                "rating": rating, "warnings": warnings}

    if div_growth_pct is not None and div_growth_pct < -15 and div_years >= 2:
        return {"action": "SELL_CAPITAL_GAIN", "strategy": "Alihkan ke Capital Gain",
                "reasoning": (f"Dividen turun {abs(div_growth_pct):.0f}% YoY — tren memburuk. "
                              "Lebih baik ambil capital gain jika harga masih bagus daripada tunggu dividen yang menyusut."),
                "rating": rating, "warnings": warnings}

    if yield_pct >= MIN_YIELD and div_consistent and payout_ratio <= MAX_PAYOUT:
        growing = div_growth_pct and div_growth_pct > 5
        action  = "BUY_DIVIDEND" if growing else "HOLD_DIVIDEND"
        return {"action": action, "strategy": "Dividend Investing",
                "reasoning": (f"Yield {yield_pct:.1f}% dengan payout ratio {payout_ratio:.0f}% yang sehat. "
                              f"{'Dividen tumbuh ' + str(div_growth_pct) + '% YoY — momentum bagus. ' if growing else 'Dividen stabil. '}"
                              "Cocok untuk investor dividen jangka panjang."),
                "rating": rating, "warnings": warnings}

    return {"action": "WATCH", "strategy": "Perlu Observasi",
            "reasoning": f"Yield {yield_pct:.1f}% memenuhi syarat tapi perlu konfirmasi lebih lanjut.",
            "rating": rating, "warnings": warnings}


def main():
    now = datetime.now(WIB)
    print(f"\n📊 IDX Dividend Scanner")
    print(f"   {now.strftime('%d %B %Y %H:%M WIB')}\n")

    print("🔍 Mencari kandidat saham dividen dari TradingView...")
    candidates = get_dividend_candidates(80)
    print(f"   Ditemukan {len(candidates)} kandidat\n")

    results = []
    for i, tv_data in enumerate(candidates, 1):
        ticker = tv_data["ticker"]
        print(f"[{i}/{len(candidates)}] {ticker}...", end=" ", flush=True)
        result = analyze_dividend(ticker, tv_data)
        if result:
            results.append(result)
            icons = {"BUY_DIVIDEND": "💰", "HOLD_DIVIDEND": "✅",
                     "SELL_CAPITAL_GAIN": "📈", "AVOID": "⛔", "WATCH": "👀"}
            print(f"{icons.get(result['recommendation'],'?')} {result['recommendation']} yield={result['yield_pct']}% ⭐{result['rating']}")
        else:
            print("skip")

    order = {"BUY_DIVIDEND": 0, "HOLD_DIVIDEND": 1, "WATCH": 2, "SELL_CAPITAL_GAIN": 3, "AVOID": 4}
    results.sort(key=lambda x: (order.get(x["recommendation"], 5), -x.get("rating", 0)))

    summary = {k: sum(1 for r in results if r["recommendation"] == k)
               for k in ["BUY_DIVIDEND", "HOLD_DIVIDEND", "SELL_CAPITAL_GAIN", "AVOID", "WATCH"]}

    output = {
        "generated_at": now.strftime("%d %B %Y %H:%M WIB"),
        "date":         now.strftime("%d %B %Y"),
        "total":        len(results),
        "min_yield":    MIN_YIELD,
        "summary":      summary,
        "results":      results,
    }

    out_file = OUT_DIR / "latest.json"
    with open(out_file, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Done — {len(results)} saham")
    print(f"   💰 {summary['BUY_DIVIDEND']} | ✅ {summary['HOLD_DIVIDEND']} | "
          f"📈 {summary['SELL_CAPITAL_GAIN']} | ⛔ {summary['AVOID']} | 👀 {summary['WATCH']}")
    print(f"   Saved: {out_file}")
    return output


if __name__ == "__main__":
    main()
