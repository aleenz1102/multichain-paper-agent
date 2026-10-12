from __future__ import annotations
import json, math, os, re, sqlite3, time
from datetime import datetime, timezone, date
from pathlib import Path
from contextlib import contextmanager
from typing import Optional
from threading import Lock, RLock
from urllib.request import Request, urlopen
from urllib.parse import urlencode
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from app.market_data import CoinGeckoFreeProvider, COINGECKO_IDS
from app.intelligence import TrendingUniverseProvider, CryptoNewsProvider
from app.research import walk_forward_report
from app.strategy import combined_signal, build_trade_plan
from app.backtest import BacktestConfig, run_backtest
from app.auto_monitor import MonitorConfig, PaperSignalMonitor
from app.candle_data import BinancePublicCandleProvider
from app.auto_scheduler import PaperAutoScheduler

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT.parent / "paper_agent.db"
app = FastAPI(title="Multi-Chain Paper Trading Agent", version="0.3.0")

# JSON API responses are explicitly non-cacheable by browsers and intermediary proxies.
@app.middleware("http")
async def set_security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response
live_provider = CoinGeckoFreeProvider()
universe_provider = TrendingUniverseProvider()
news_provider = CryptoNewsProvider()

# Synthetic demo universe. No live provider or signing functionality is included.
ASSETS = [
    {"symbol":"SOL","name":"Solana","chain":"Solana","price":145.0,"volatility":0.018,"liquidity":8500000},
    {"symbol":"ETH","name":"Ethereum","chain":"Ethereum","price":3200.0,"volatility":0.012,"liquidity":24000000},
    {"symbol":"AERO","name":"Aerodrome","chain":"Base","price":0.92,"volatility":0.035,"liquidity":1800000},
    {"symbol":"ARB","name":"Arbitrum","chain":"Arbitrum","price":0.74,"volatility":0.028,"liquidity":3200000},
    {"symbol":"BNB","name":"BNB","chain":"BNB Chain","price":590.0,"volatility":0.015,"liquidity":7200000},
    {"symbol":"JUP","name":"Jupiter","chain":"Solana","price":0.81,"volatility":0.04,"liquidity":2600000},
    {"symbol":"STRK","name":"Starknet","chain":"Ethereum","price":0.102,"volatility":0.055,"liquidity":5000000},
    {"symbol":"PENGU","name":"Pudgy Penguins","chain":"Solana","price":0.0082,"volatility":0.045,"liquidity":4500000},
    {"symbol":"TIA","name":"Celestia","chain":"Celestia","price":0.60,"volatility":0.04,"liquidity":3800000},
    {"symbol":"DRV","name":"Derive","chain":"Ethereum","price":0.53,"volatility":0.05,"liquidity":1200000},
    {"symbol":"HYPE","name":"Hyperliquid","chain":"Hyperliquid","price":85.0,"volatility":0.035,"liquidity":9000000},
    {"symbol":"TRUMP","name":"Official Trump","chain":"Solana","price":1.88,"volatility":0.065,"liquidity":6500000},
    {"symbol":"SWARM","name":"SWARM ENGINE","chain":"Ethereum","price":0.025,"volatility":0.12,"liquidity":400000},
    {"symbol":"USDC","name":"USD Coin","chain":"Multi-chain","price":1.0,"volatility":0.001,"liquidity":50000000},
]
DEFAULTS = {
    "virtual_cash": 100000.0, "fee_bps": 10.0, "slippage_bps": 8.0,
    "max_trade_pct": 10.0, "daily_loss_limit_pct": 3.0,
    "max_positions": 4, "paused": False, "auto_monitor_enabled": False,
    "max_drawdown_pct": 10.0, "max_exposure_pct": 60.0,
    "high_water_equity": 100000.0, "market_data_mode": "synthetic",
    "risk_lockout_reason": "", "risk_lockout_date": ""
}
_order_lock = RLock()
_auto_cycle_lock = Lock()
def now_iso(): return datetime.now(timezone.utc).isoformat()
@contextmanager
def connect():
    """Yield a row-enabled SQLite connection and always close the file handle."""
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()
def init_db():
    with connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, symbol TEXT NOT NULL,
            chain TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL,
            notional REAL NOT NULL, fee REAL NOT NULL, slippage_cost REAL NOT NULL,
            realized_pnl REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'filled'
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS cash_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, amount REAL NOT NULL, note TEXT NOT NULL
        )""")
        for k,v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,json.dumps(v)))
        c.commit()
def settings():
    with connect() as c: rows=c.execute("SELECT key,value FROM settings").fetchall()
    out=DEFAULTS.copy()
    for r in rows:
        try: out[r["key"]]=json.loads(r["value"])
        except Exception: pass
    return out
def set_setting(k,v):
    with connect() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,json.dumps(v)))
        c.commit()
def audit(event, detail):
    with connect() as c:
        c.execute("INSERT INTO audit(ts,event,detail) VALUES(?,?,?)",(now_iso(),event,json.dumps(detail)))
        c.commit()
def market():
    """Return one selected quote source; never silently mix live and synthetic prices."""
    if settings().get("market_data_mode", "synthetic") == "live_public":
        symbols = [a["symbol"] for a in ASSETS if a["symbol"] != "USDC"]
        prices = live_provider.get_prices(symbols)
        output = []
        for asset in ASSETS:
            symbol = asset["symbol"]
            if symbol == "USDC":
                output.append({**asset, "price": 1.0, "change_pct": 0.0, "signal": "HOLD",
                               "signal_strength": 0.0, "data_status": "REFERENCE ONLY"})
                continue
            item = prices.get(symbol)
            fresh = item is not None and item.status == "fresh" and item.price is not None
            output.append({**asset, "price": float(item.price) if fresh else None,
                           "change_pct": None, "signal": "HOLD", "signal_strength": 0.0,
                           "data_status": "LIVE PUBLIC PRICE" if fresh else
                               ("UNAVAILABLE" if item is None or item.status == "unavailable" else "STALE")})
        return output
    tick = int(time.time() // 20)
    output = []
    for i, asset in enumerate(ASSETS):
        wave = math.sin((tick + i * 7) * 0.37) * asset["volatility"] + math.sin((tick + i) * 0.11) * asset["volatility"] * 0.35
        px = asset["price"] * (1 + wave)
        change = (px / asset["price"] - 1) * 100
        score = math.sin((tick + i * 3) * 0.31)
        signal = "BUY" if score > 0.72 and asset["symbol"] != "USDC" else ("SELL" if score < -0.72 and asset["symbol"] != "USDC" else "HOLD")
        output.append({**asset, "price": round(px, 6), "change_pct": round(change, 2),
                       "signal": signal, "signal_strength": round(abs(score), 2),
                       "data_status": "SYNTHETIC DEMO"})
    return output
def rows(sql, args=()):
    with connect() as c: return [dict(x) for x in c.execute(sql,args).fetchall()]
def open_positions():
    trades=rows("SELECT * FROM trades ORDER BY id")
    positions={}
    for t in trades:
        s=t["symbol"]
        if s not in positions: positions[s]={"symbol":s,"chain":t["chain"],"qty":0.0,"cost":0.0,"realized_pnl":0.0}
        p=positions[s]
        if t["side"]=="BUY":
            p["qty"]+=t["qty"]; p["cost"]+=t["notional"]+t["fee"]+t["slippage_cost"]
        else:
            if p["qty"]>0:
                avg=p["cost"]/p["qty"]
                sold=min(p["qty"],t["qty"])
                p["realized_pnl"]+=(t["price"]*sold-t["fee"]-t["slippage_cost"])-avg*sold
                p["qty"]-=sold; p["cost"]=max(0,p["cost"]-avg*sold)
    return [p for p in positions.values() if p["qty"]>1e-10]
def portfolio():
    s=settings(); mk={x["symbol"]:x for x in market()}; pos=open_positions()
    cash=s["virtual_cash"]
    # Manual wallet funding is virtual-only and recorded in a separate ledger.
    adjustments=rows("SELECT amount FROM cash_adjustments ORDER BY id")
    cash += sum(a["amount"] for a in adjustments)
    trades=rows("SELECT * FROM trades ORDER BY id")
    for t in trades:
        if t["side"]=="BUY": cash-=t["notional"]+t["fee"]+t["slippage_cost"]
        else: cash+=t["notional"]-t["fee"]-t["slippage_cost"]
    unreal=0; exposure=0
    valuation_complete = True
    unpriced_positions = []
    for p in pos:
        px = mk.get(p["symbol"], {}).get("price")
        if not isinstance(px, (int, float)) or not math.isfinite(float(px)) or px <= 0:
            valuation_complete = False
            unpriced_positions.append(p["symbol"])
            p["mark_price"] = None
            p["market_value"] = round(p["cost"], 2)  # display-only cost-basis fallback
            p["unrealized_pnl"] = None
            p["valuation_status"] = "COST_BASIS_FALLBACK"
        else:
            p["mark_price"] = float(px)
            p["market_value"] = p["qty"] * float(px)
            p["unrealized_pnl"] = p["market_value"] - p["cost"]
            p["valuation_status"] = "FRESH"
            unreal += p["unrealized_pnl"]
        exposure += p["market_value"]
    realized=sum(t["realized_pnl"] for t in trades if t["side"]=="SELL")
    fees=sum(t["fee"] for t in trades); slip=sum(t["slippage_cost"] for t in trades)
    equity=cash+exposure
    total_topups=sum(a["amount"] for a in adjustments)
    high_water = max(float(s.get("high_water_equity", 0) or 0), float(equity))
    if high_water > float(s.get("high_water_equity", 0) or 0):
        set_setting("high_water_equity", high_water)
    drawdown_pct = max(0.0, (high_water - equity) / high_water * 100) if high_water > 0 else 0.0
    return {"virtual_cash":round(cash,2),"equity":round(equity,2),"starting_virtual_cash":s["virtual_cash"],
            "virtual_topups":round(total_topups,2),"high_water_equity":round(high_water,2),
            "drawdown_pct":round(drawdown_pct,4),
            "exposure":round(exposure,2),"realized_pnl":round(realized,2),"unrealized_pnl":round(unreal,2),
            "net_pnl":round(equity-s["virtual_cash"]-total_topups,2),"fees_paid":round(fees,2),"slippage_cost":round(slip,2),
            "positions":pos,"position_count":len(pos),
            "valuation_complete":valuation_complete,"unpriced_positions":unpriced_positions}
class Order(BaseModel):
    symbol: str = Field(min_length=2, max_length=20, pattern=r"^[A-Z0-9]{2,20}$")
    side: str = Field(min_length=3, max_length=4)
    amount: float = Field(gt=0, allow_inf_nan=False, description="Virtual quote-currency amount")
    sell_percent: Optional[float] = Field(default=None, gt=0, le=100, allow_inf_nan=False)
class SettingsUpdate(BaseModel):
    fee_bps: Optional[float] = Field(default=None, ge=0, le=500, allow_inf_nan=False)
    slippage_bps: Optional[float] = Field(default=None, ge=0, le=500, allow_inf_nan=False)
    max_trade_pct: Optional[float] = Field(default=None, gt=0, le=100, allow_inf_nan=False)
    daily_loss_limit_pct: Optional[float] = Field(default=None, gt=0, le=100, allow_inf_nan=False)
    max_positions: Optional[int] = Field(default=None, ge=1, le=30)
    auto_monitor_enabled: Optional[bool] = None
    virtual_cash: Optional[float] = Field(default=None, ge=1, le=100000000, allow_inf_nan=False)
    max_drawdown_pct: Optional[float] = Field(default=None, gt=0, le=100, allow_inf_nan=False)
    max_exposure_pct: Optional[float] = Field(default=None, gt=0, le=100, allow_inf_nan=False)
    market_data_mode: Optional[str] = None
_auto_scheduler: PaperAutoScheduler | None = None

@app.on_event("startup")
def startup():
    global _auto_scheduler
    init_db()
    _auto_scheduler = PaperAutoScheduler(
        enabled=lambda: bool(settings().get("auto_monitor_enabled", False)),
        paused=lambda: bool(settings().get("paused", False)),
        run_cycle=lambda: auto_trade_run(AutoMonitorEvaluation()),
        audit=audit,
        interval_seconds=300,
    )
    _auto_scheduler.start()

@app.on_event("shutdown")
def shutdown():
    if _auto_scheduler is not None:
        _auto_scheduler.stop(timeout=10)
@app.get("/", response_class=HTMLResponse)
def home(): return (ROOT/"static"/"index.html").read_text(encoding="utf-8")
@app.get("/api/health")
def health():
    mode = settings().get("market_data_mode", "synthetic")
    return {
        "ok": True,
        "mode": "PAPER_ONLY",
        "data": "LIVE PUBLIC DATA CONFIGURED" if mode == "live_public" else "SYNTHETIC DEMO",
        "market_data_mode": mode,
        "live_trading": False,
    }
@app.get("/api/market")
def get_market():
    mode = settings().get("market_data_mode", "synthetic")
    return {"data_status": "SYNTHETIC DEMO" if mode == "synthetic" else "LIVE PUBLIC DATA — unavailable quotes are blocked",
            "provider": mode, "items": market()}

@app.get("/api/market/live")
def get_live_market(symbols: str = "SOL,ETH,ARB,BNB,JUP,AERO,STRK,PENGU,TIA,HYPE,TRUMP,USDC"):
    """Fetch free public prices. This endpoint does not feed orders or replace demo prices."""
    requested = [s.strip().upper() for s in symbols.split(",") if s.strip()][:30]
    if not requested:
        raise HTTPException(400, "Provide at least one supported symbol.")
    try:
        data = live_provider.get_prices(requested)
    except Exception as exc:
        audit("LIVE_MARKET_FETCH_FAILED", {"error_type": type(exc).__name__, "requested_count": len(requested)})
        raise HTTPException(503, "Public market prices are temporarily unavailable.") from exc
    items = [
        {"symbol": x.symbol, "price_inr": x.price, "currency": "INR",
         "provider": x.provider, "fetched_at": x.fetched_at, "status": x.status, "error": x.error}
        for x in data.values()
    ]
    return {
        "mode": "LIVE_PUBLIC_DATA_READ_ONLY",
        "new_entries_allowed": bool(items) and all(x["status"] == "fresh" and x["price_inr"] is not None for x in items),
        "items": items,
    }

class CandleInput(BaseModel):
    candles: list[dict] = Field(min_length=2, max_length=5000)

class BacktestInput(CandleInput):
    initial_cash: float = Field(default=100000, gt=0, le=1000000000, allow_inf_nan=False)
    fee_bps: float = Field(default=10, ge=0, le=500, allow_inf_nan=False)
    slippage_bps: float = Field(default=8, ge=0, le=500, allow_inf_nan=False)
    allow_shorts: bool = True
    position_fraction: float = Field(default=0.10, gt=0, le=1, allow_inf_nan=False)

@app.post("/api/strategy/signal")
def strategy_signal(data: CandleInput):
    try:
        return combined_signal(data.candles)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, f"Invalid candle data: {exc}")

@app.post("/api/backtest")
def backtest(data: BacktestInput):
    try:
        cfg = BacktestConfig(initial_cash=data.initial_cash, fee_bps=data.fee_bps, slippage_bps=data.slippage_bps,
                             allow_shorts=data.allow_shorts, position_fraction=data.position_fraction)
        return run_backtest(data.candles, combined_signal, cfg)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, f"Backtest could not run: {exc}")
@app.get("/api/settings")
def get_settings(): return settings()
@app.patch("/api/settings")
def update_settings(update: SettingsUpdate):
    data=update.model_dump(exclude_none=True)
    current = settings()
    if "market_data_mode" in data and data["market_data_mode"] not in ("synthetic", "live_public"):
        raise HTTPException(400, "market_data_mode must be 'synthetic' or 'live_public'.")
    effective_mode = data.get("market_data_mode", current.get("market_data_mode", "synthetic"))
    effective_auto = data.get("auto_monitor_enabled", current.get("auto_monitor_enabled", False))
    if effective_auto and effective_mode != "live_public":
        raise HTTPException(400, "Automated paper trading requires Public market prices mode; synthetic demo prices cannot be used for unattended cycles.")
    if data.get("market_data_mode") == "live_public":
        live_prices = live_provider.get_prices([a["symbol"] for a in ASSETS if a["symbol"] != "USDC"])
        required_symbols = ["SOL", "ETH", "ARB", "BNB"]
        if any(symbol not in live_prices or live_prices[symbol].price is None or live_prices[symbol].status != "fresh" for symbol in required_symbols):
            raise HTTPException(503, "Required strategy market data is incomplete or stale; provider mode was not changed.")
    for k,v in data.items():
        if k=="virtual_cash" and float(v) != float(current.get("virtual_cash", DEFAULTS["virtual_cash"])) and rows("SELECT id FROM trades LIMIT 1"):
            raise HTTPException(400,"Virtual starting cash cannot be changed after the first paper trade.")
        set_setting(k,v)
    audit("SETTINGS_UPDATED",data)
    return settings()
@app.post("/api/pause")
def pause():
    set_setting("paused",True); audit("TRADING_PAUSED",{"reason":"manual kill switch"})
    return {"paused":True}
@app.post("/api/resume")
def resume():
    set_setting("paused",False); audit("TRADING_RESUMED",{"mode":"paper only"})
    return {"paused":False}
@app.post("/api/reset")
def reset():
    """Clear simulation records only; never alter settings or backup files.

    Acquire locks in the same order as the paper-cycle path so reset cannot race
    an active automated cycle. Keep this destructive endpoint on localhost or
    behind a trusted authenticated reverse proxy.
    """
    with _auto_cycle_lock:
        with _order_lock:
            set_setting("auto_monitor_enabled", False)
            set_setting("paused", True)
            with connect() as c:
                c.execute("DELETE FROM trades")
                c.execute("DELETE FROM cash_adjustments")
                c.commit()
            set_setting("high_water_equity", float(settings()["virtual_cash"]))
            set_setting("risk_lockout_reason", "")
            set_setting("risk_lockout_date", "")
            set_setting("paused", False)
    audit("SIMULATION_RESET", {"message": "Paper trade history cleared; settings preserved; no backup file touched"})
    return {"ok": True}


@app.get("/api/portfolio")
def get_portfolio(): return portfolio()
class WalletFunding(BaseModel):
    amount: float = Field(gt=0, le=100000000, description="Virtual-only wallet top-up")
    note: str = Field(default="Manual virtual balance adjustment", max_length=160)
@app.post("/api/wallet/fund")
def fund_wallet(f: WalletFunding):
    # Explicitly a virtual ledger adjustment; this endpoint cannot move real funds.
    with connect() as c:
        c.execute("INSERT INTO cash_adjustments(ts,amount,note) VALUES(?,?,?)",(now_iso(),f.amount,f.note))
        c.commit()
    audit("VIRTUAL_WALLET_FUNDED",{"amount":f.amount,"note":f.note,"real_funds":False})
    return {"ok":True,"message":"Virtual paper balance updated. No real funds were moved.","portfolio":portfolio()}
@app.get("/api/wallet/adjustments")
def wallet_adjustments(): return rows("SELECT * FROM cash_adjustments ORDER BY id DESC LIMIT 100")
@app.get("/api/positions/{symbol}/sellable")
def sellable_position(symbol: str):
    p=portfolio()
    pos=next((x for x in p["positions"] if x["symbol"].upper()==symbol.upper()),None)
    if not pos:
        return {"symbol":symbol.upper(),"quantity":0,"market_value":0,"sellable":False}
    return {"symbol":pos["symbol"],"quantity":pos["qty"],"mark_price":pos["mark_price"],
            "market_value":pos["market_value"],"sellable":True}
@app.get("/api/trades")
def get_trades(): return rows("SELECT * FROM trades ORDER BY id DESC LIMIT 100")
@app.get("/api/audit")
def get_audit(): return rows("SELECT * FROM audit ORDER BY id DESC LIMIT 100")
@app.post("/api/orders")
def order(o: Order):
    # Serialize check-and-fill to prevent concurrent requests bypassing cash/exposure limits.
    with _order_lock:
        s = settings()
        if s["paused"]:
            raise HTTPException(400, "Paper trading is paused. Resume it to place simulated orders.")
        side = o.side.upper()
        if side not in ("BUY", "SELL"):
            raise HTTPException(400, "Side must be BUY or SELL.")
        if o.symbol != o.symbol.strip().upper():
            raise HTTPException(400, "Symbol must be uppercase without surrounding whitespace.")

        today = datetime.now(timezone.utc).date().isoformat()
        todays_sells = rows(
            "SELECT realized_pnl FROM trades WHERE side='SELL' AND substr(ts,1,10)=?",
            (today,),
        )
        realized_today = sum(t["realized_pnl"] for t in todays_sells)
        daily_loss_limit = s["virtual_cash"] * s["daily_loss_limit_pct"] / 100
        daily_locked = realized_today <= -daily_loss_limit
        p = portfolio()
        valuation_complete = bool(p.get("valuation_complete", True))
        drawdown_pct = float(p.get("drawdown_pct", 0))
        drawdown_limit = float(s.get("max_drawdown_pct", DEFAULTS["max_drawdown_pct"]))
        drawdown_locked = valuation_complete and drawdown_pct >= drawdown_limit

        if daily_locked:
            risk_reason = "daily_loss"
        elif not valuation_complete:
            risk_reason = "stale_valuation"
        elif drawdown_locked:
            risk_reason = "max_drawdown"
        else:
            risk_reason = ""

        previous_reason = str(s.get("risk_lockout_reason", "") or "")
        previous_date = str(s.get("risk_lockout_date", "") or "")
        if risk_reason:
            set_setting("risk_lockout_reason", risk_reason)
            set_setting("risk_lockout_date", today if risk_reason == "daily_loss" else "")
            if previous_reason != risk_reason or (risk_reason == "daily_loss" and previous_date != today):
                audit("RISK_CIRCUIT_BREAKER_TRIGGERED", {
                    "reason": risk_reason,
                    "realized_pnl_today": round(realized_today, 2),
                    "daily_loss_limit": round(daily_loss_limit, 2),
                    "drawdown_pct": round(drawdown_pct, 4),
                    "drawdown_limit_pct": drawdown_limit,
                    "valuation_complete": valuation_complete,
                })
            # Circuit breakers block additional risk, not position-reducing exits.
            if side == "BUY":
                if risk_reason == "daily_loss":
                    raise HTTPException(400, "Daily realized-loss limit reached. New paper entries are locked until the UTC day resets.")
                if risk_reason == "max_drawdown":
                    raise HTTPException(400, "Maximum portfolio drawdown reached. New paper entries are locked.")
                raise HTTPException(503, "Portfolio has positions without fresh quotes; new paper entries are blocked.")
        elif previous_reason in ("daily_loss", "max_drawdown", "stale_valuation"):
            set_setting("risk_lockout_reason", "")
            set_setting("risk_lockout_date", "")
            audit("RISK_CIRCUIT_BREAKER_CLEARED", {"previous_reason": previous_reason})

        mk = {x["symbol"]: x for x in market()}
        if o.symbol not in mk:
            raise HTTPException(400, "Unknown asset.")
        asset = mk[o.symbol]
        if asset.get("price") is None or not isinstance(asset.get("price"), (int, float)) or asset["price"] <= 0:
            raise HTTPException(503, "No fresh price is available for this asset; paper order rejected.")
        if s.get("market_data_mode", "synthetic") == "live_public" and not str(asset.get("data_status", "")).startswith("LIVE PUBLIC"):
            raise HTTPException(503, "Selected public price is stale or unavailable; paper order rejected.")
        if side == "BUY" and o.amount > p["equity"] * s["max_trade_pct"] / 100:
            raise HTTPException(400, f"Order exceeds max trade allocation ({s['max_trade_pct']}% of equity).")

        active = {x["symbol"]: x for x in p["positions"]}
        if side == "BUY" and o.symbol not in active and len(active) >= s["max_positions"]:
            raise HTTPException(400, "Maximum concurrent positions reached.")
        if side == "BUY":
            exposure_limit = float(p["equity"]) * float(s.get("max_exposure_pct", DEFAULTS["max_exposure_pct"])) / 100
            if float(p["exposure"]) + float(o.amount) > exposure_limit:
                raise HTTPException(400, f"Order exceeds portfolio exposure limit ({s.get('max_exposure_pct', DEFAULTS['max_exposure_pct'])}% of equity).")

        fee_rate = s["fee_bps"] / 10000
        slip_rate = s["slippage_bps"] / 10000
        mid = asset["price"]
        px = mid * (1 + slip_rate if side == "BUY" else 1 - slip_rate)
        qty = o.amount / px
        if side == "SELL" and o.sell_percent is not None:
            current_position = next((x for x in p["positions"] if x["symbol"] == o.symbol), None)
            if not current_position:
                raise HTTPException(400, "No open paper position for this asset.")
            qty = current_position["qty"] * o.sell_percent / 100

        notional = qty * mid
        fee = notional * fee_rate
        slip_cost = abs(px - mid) * qty
        if side == "BUY" and notional + fee + slip_cost > p["virtual_cash"]:
            raise HTTPException(400, "Insufficient virtual cash including fees and slippage.")
        if side == "SELL":
            available = active.get(o.symbol, {}).get("qty", 0)
            if qty > available * (1 + 1e-9):
                raise HTTPException(400, f"Sell exceeds holdings. Available: {available:.8f} {o.symbol}.")
        realized = 0.0
        if side == "SELL" and o.symbol in active:
            avg = active[o.symbol]["cost"] / active[o.symbol]["qty"] if active[o.symbol]["qty"] else 0
            realized = (notional - fee - slip_cost) - avg * qty
        with connect() as c:
            c.execute(
                """INSERT INTO trades(ts,symbol,chain,side,qty,price,notional,fee,slippage_cost,realized_pnl,status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (now_iso(), o.symbol, asset["chain"], side, qty, px, notional, fee, slip_cost, realized, "filled"),
            )
            c.commit()
        audit("PAPER_ORDER_FILLED", {
            "symbol": o.symbol, "side": side, "qty": qty, "execution_price": px,
            "fee": fee, "slippage_cost": slip_cost,
        })
        return {
            "ok": True, "message": "Paper order simulated; no real order was sent.",
            "trade": rows("SELECT * FROM trades ORDER BY id DESC LIMIT 1")[0],
            "portfolio": portfolio(),
        }

_candle_provider = BinancePublicCandleProvider()


def _build_signal_monitor() -> PaperSignalMonitor:
    cfg = MonitorConfig(
        interval_seconds=300,
        enabled=bool(settings().get("auto_monitor_enabled", False)),
    )
    return PaperSignalMonitor(
        candle_provider=_candle_provider.get_candles,
        audit=audit,
        config=cfg,
    )


@app.get("/api/auto-monitor/status")
def auto_monitor_status():
    s = settings()
    return {
        "enabled": bool(s.get("auto_monitor_enabled", False)),
        "mode": "PAPER_AUTO_TRADE" if s.get("auto_monitor_enabled", False) else "AUDIT_ONLY",
        "orders_submitted": False,
        "scheduler_running": bool(_auto_scheduler and _auto_scheduler.running),
        "interval_seconds": _auto_scheduler.interval_seconds if _auto_scheduler else 300,
        "last_cycle_at": _auto_scheduler.last_cycle_at if _auto_scheduler else None,
        "last_cycle_status": _auto_scheduler.last_cycle_status if _auto_scheduler else "not_started",
        "last_cycle_error": _auto_scheduler.last_cycle_error if _auto_scheduler else None,
        "candle_provider": "binance_public_klines",
        "message": (
            "Scheduler is active; paper cycles run every five minutes while enabled and unpaused."
            if _auto_scheduler and _auto_scheduler.running and s.get("auto_monitor_enabled", False)
            else "Scheduler is idle unless automated paper trading is enabled; all execution remains simulated."
        ),
    }


class AutoMonitorEvaluation(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=30)
    auto_select_trending: bool = True


@app.post("/api/auto-monitor/evaluate")
def auto_monitor_evaluate(request: AutoMonitorEvaluation):
    monitor = _build_signal_monitor()
    result = monitor.evaluate(request.symbols)
    audit("AUTO_MONITOR_MANUAL_EVALUATION", {
        "enabled": bool(settings().get("auto_monitor_enabled", False)),
        "symbols": request.symbols,
        "evaluated": result.get("evaluated", 0),
        "orders_submitted": False,
    })
    return result


@app.post("/api/auto-trade/run")
def auto_trade_run(request: AutoMonitorEvaluation):
    """One bounded automated paper-trading cycle; never sends external orders."""
    if not _auto_cycle_lock.acquire(blocking=False):
        raise HTTPException(409, "An automated paper cycle is already in progress.")
    try:
        s = settings()
        if not s.get("auto_monitor_enabled", False):
            raise HTTPException(400, "Automated paper trading is disabled. Enable it in settings first.")
        if s.get("paused", False):
            raise HTTPException(400, "Paper trading is paused.")
        if s.get("market_data_mode", "synthetic") != "live_public":
            raise HTTPException(400, "Automated paper trading requires Public market prices mode.")
        symbols = list(dict.fromkeys(x.strip().upper() for x in request.symbols if x.strip()))[:30]
        if request.auto_select_trending or not symbols:
            try:
                scan = universe_provider.scan(limit=100, min_quote_volume=2_000_000)
                supported = {a["symbol"] for a in ASSETS if a["symbol"] in COINGECKO_IDS and a["symbol"] != "USDC"}
                if scan.get("provider_status") == "stale_cache":
                    raise HTTPException(503, "Trending market scan is stale; paper cycle blocked.")
                symbols = [x["symbol"] for x in scan["items"] if x["symbol"] in supported][:8]
                if not symbols:
                    raise HTTPException(503, "No supported liquid trending symbols are available; paper cycle blocked.")
            except RuntimeError as exc:
                raise HTTPException(503, "Trending market scan unavailable; no paper cycle was run.") from exc
        monitor = _build_signal_monitor()
        result = monitor.evaluate(symbols)
        executed = []
        for item in result.get("results", []):
            symbol = item.get("symbol", "")
            signal = item.get("signal", "HOLD").upper()
            if signal not in ("BUY", "SELL") or symbol not in {a["symbol"] for a in ASSETS}:
                continue
            news_context = _news_veto(symbol)
            if signal == "BUY" and news_context.get("veto_buy"):
                executed.append({"symbol": symbol, "signal": signal, "status": "blocked_by_news",
                                 "reason": news_context["reason"], "news": news_context})
                continue
            p = portfolio()
            if signal == "BUY":
                amount = round(max(0.0, min(p["equity"] * s["max_trade_pct"] / 100, p["virtual_cash"])), 2)
                if amount <= 0:
                    executed.append({"symbol": symbol, "signal": signal, "status": "skipped", "reason": "No virtual cash available."})
                    continue
            else:
                pos = next((p0 for p0 in p["positions"] if p0["symbol"] == symbol), None)
                if not pos:
                    executed.append({"symbol": symbol, "signal": signal, "status": "skipped", "reason": "No open paper position."})
                    continue
                amount = round(pos["market_value"], 2)
            try:
                fill = order(Order(symbol=symbol, side=signal, amount=max(amount, 0.01), sell_percent=100 if signal == "SELL" else None))
                executed.append({"symbol": symbol, "signal": signal, "status": "paper_filled", "trade_id": fill["trade"]["id"]})
            except HTTPException as exc:
                executed.append({"symbol": symbol, "signal": signal, "status": "blocked_by_risk", "reason": str(exc.detail)})
        payload = {
            "mode": "PAPER_ONLY", "live_trading": False, "orders_submitted": False,
            "evaluated": result.get("evaluated", 0), "results": result.get("results", []),
            "paper_actions": executed,
        }
        audit("AUTO_PAPER_CYCLE_COMPLETED", {
            "symbols": symbols, "orders_submitted": False,
            "paper_fills": sum(x["status"] == "paper_filled" for x in executed),
            "results": executed,
        })
        return payload
    finally:
        _auto_cycle_lock.release()


@app.get("/api/ai/status")
def ai_status():
    configured = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    return {
        "enabled": configured,
        "provider": "openai_compatible",
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "key_source": "environment",
        "key_exposed": False,
        "can_execute_orders": False,
        "message": "AI is advisory only. Configure OPENAI_API_KEY locally to enable analysis." if not configured else "AI analysis configured; all trading decisions remain subject to deterministic paper-risk checks.",
    }


class AIAnalysisInput(BaseModel):
    symbols: list[str] = Field(default=["SOL", "ETH", "ARB", "BNB"], min_length=1, max_length=12)


@app.post("/api/ai/analyze")
def ai_analyze(request: AIAnalysisInput):
    """Opt-in remote AI commentary. Output is never parsed as a trade/order instruction."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(503, "AI is not configured. Set OPENAI_API_KEY in the local environment and restart the app.")
    selected = list(dict.fromkeys(s.strip().upper() for s in request.symbols if s.strip()))[:12]
    known = {a["symbol"] for a in ASSETS}
    if not selected or any(s not in known for s in selected):
        raise HTTPException(400, "Select known watchlist symbols only.")
    snapshots = []
    prices = {x["symbol"]: x for x in market()}
    for symbol in selected:
        row = prices.get(symbol, {})
        snapshots.append({
            "symbol": symbol,
            "price": row.get("price"),
            "data_status": row.get("data_status"),
            "signal": row.get("signal", "HOLD"),
            "signal_strength": row.get("signal_strength", 0),
        })
    body = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": "You are a cautious market-research assistant for a simulation-only paper trading application. Give concise risk-aware analysis of the provided market snapshot. If data_status says SYNTHETIC DEMO, explicitly treat it as simulated data, not a live market. Distinguish missing/stale data. Do not promise returns. Never issue executable orders, tool calls, code, or instructions to bypass risk controls. State that analysis is advisory and not investment advice."},
            {"role": "user", "content": json.dumps({"mode": "PAPER_ONLY", "live_trading": False, "watchlist": snapshots}, separators=(",", ":"))},
        ],
    }
    url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    if url.endswith("/v1"):
        url += "/chat/completions"
    elif not url.endswith("/chat/completions"):
        url += "/chat/completions"
    if not url.startswith("https://"):
        raise HTTPException(500, "OPENAI_BASE_URL must use HTTPS.")
    req = Request(url, data=json.dumps(body).encode("utf-8"), headers={
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "multichain-paper-agent/1.0",
    }, method="POST")
    try:
        with urlopen(req, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
        analysis = result["choices"][0]["message"]["content"]
        if not isinstance(analysis, str) or not analysis.strip():
            raise ValueError("empty AI content")
    except HTTPException:
        raise
    except Exception as exc:
        audit("AI_ANALYSIS_FAILED", {"error_type": type(exc).__name__})
        raise HTTPException(502, "AI provider request failed. Check server configuration and try again.") from exc
    audit("AI_ANALYSIS_COMPLETED", {"provider": "openai_compatible", "model": body["model"], "symbols": selected, "orders_submitted": False})
    return {"analysis": analysis, "provider": "openai_compatible", "model": body["model"],
            "advisory_only": True, "live_trading": False, "orders_submitted": False}



@app.get("/api/market/detail/{symbol}")
def market_detail(symbol: str, interval: str = "5m"):
    """Read-only live ticker, completed candles and explainable signal for a USDT spot pair."""
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{2,20}", symbol):
        raise HTTPException(400, "Invalid market symbol.")
    if interval not in {"1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"}:
        raise HTTPException(400, "Unsupported candle interval.")
    pair = symbol + "USDT"
    url = "https://data-api.binance.vision/api/v3/ticker/24hr?" + urlencode({"symbol": pair})
    try:
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "multichain-paper-agent/1.0"})
        with urlopen(req, timeout=8) as response:
            ticker = json.loads(response.read().decode("utf-8"))
        if not isinstance(ticker, dict) or ticker.get("symbol") != pair:
            raise ValueError("Ticker response did not match requested USDT pair.")
        candle_provider = _candle_provider if interval == _candle_provider.interval else BinancePublicCandleProvider(interval=interval, limit=150)
        candles = candle_provider.get_candles(symbol)
        signal = combined_signal(candles)
        return {
            "symbol": symbol, "pair": pair, "provider": "Binance public spot market data",
            "fetched_at": datetime.now(timezone.utc).isoformat(), "interval": interval,
            "ticker": {
                "price": float(ticker["lastPrice"]),
                "change_24h_pct": float(ticker["priceChangePercent"]),
                "quote_volume_24h_usdt": float(ticker["quoteVolume"]),
                "trades_24h": int(ticker["count"]),
                "high_24h": float(ticker["highPrice"]), "low_24h": float(ticker["lowPrice"]),
            },
            "candles": candles, "signal": signal, "trade_plan": build_trade_plan(candles, signal),
            "warning": "Signal is rule-based research, not a profitability guarantee. No order is submitted.",
        }
    except HTTPException:
        raise
    except Exception as exc:
        audit("MARKET_DETAIL_FAILED", {"symbol": symbol, "interval": interval, "error_type": type(exc).__name__})
        raise HTTPException(503, "Live ticker or candle data unavailable; signal withheld.") from exc


@app.get("/api/intelligence/candidates")
def intelligence_candidates(limit: int = 8):
    """Rank a bounded set of liquid USDT pairs using live ticker data and completed-candle signals."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not 1 <= limit <= 12:
        raise HTTPException(400, "limit must be between 1 and 12.")
    try:
        scan = universe_provider.scan(limit=100, min_quote_volume=500_000)
    except RuntimeError as exc:
        raise HTTPException(503, "Public USDT market scan unavailable; no candidates ranked.") from exc
    ranked = scan.get("items", [])
    shortlist = ranked[:min(12, len(ranked))]
    results = []

    def evaluate(item):
        symbol = item["symbol"]
        candles = _candle_provider.get_candles(symbol)
        signal = combined_signal(candles)
        # Trend score alone cannot trigger a BUY; require a candle-based BUY signal.
        eligible = signal.get("signal") == "BUY" and signal.get("regime") != "high_volatility"
        combined_rank = max(0.0, min(1.0, 0.55 * max(0.0, float(item.get("trend_score", 0))) +
                                     0.45 * max(0.0, float(signal.get("score", 0)))))
        return {
            "symbol": symbol, "pair": item["pair"], "price_usdt": item["price_usdt"],
            "change_24h_pct": item["change_24h_pct"],
            "quote_volume_24h_usdt": item["quote_volume_24h_usdt"],
            "trend_score": item["trend_score"], "signal": signal.get("signal", "HOLD"),
            "signal_score": signal.get("score", 0), "candidate_score": round(combined_rank, 4),
            "regime": signal.get("regime", "unknown"), "eligible_candidate": eligible,
            "reason": signal.get("reason", "No signal reason available."),
            "candles_completed": len(candles), "trade_plan": build_trade_plan(candles, signal),
        }

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(evaluate, item): item for item in shortlist}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception:
                # A failed/stale candle feed means no recommendation for that symbol.
                continue
    results.sort(key=lambda x: (x["eligible_candidate"], x["candidate_score"]), reverse=True)
    return {
        "provider": "Binance public spot ticker + completed candles",
        "provider_status": scan.get("provider_status", "unknown"),
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "evaluated": len(results), "requested_limit": limit,
        "items": results[:limit],
        "warning": "Rank is a research heuristic, not a probability or guarantee of profit. Only candle-confirmed BUY signals are eligible candidates; paper risk controls still apply.",
    }


@app.get("/api/intelligence/universe")
def intelligence_universe(limit: int = 30, min_quote_volume_usdt: float = 2_000_000):
    """Read-only ranked public Binance USDT spot universe."""
    if not 1 <= limit <= 100:
        raise HTTPException(400, "limit must be between 1 and 100.")
    if not math.isfinite(min_quote_volume_usdt) or min_quote_volume_usdt < 0:
        raise HTTPException(400, "min_quote_volume_usdt must be finite and non-negative.")
    try:
        return universe_provider.scan(limit=limit, min_quote_volume=min_quote_volume_usdt)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/intelligence/news")
def intelligence_news(limit: int = 40):
    """Read-only public RSS headlines with transparent weak sentiment labels."""
    if not 1 <= limit <= 100:
        raise HTTPException(400, "limit must be between 1 and 100.")
    return news_provider.fetch(limit=limit)


@app.post("/api/research/walk-forward/{symbol}")
def research_symbol_walk_forward(symbol: str):
    """Fetch recent completed public candles and run a cost-aware chronological report."""
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{2,20}", symbol):
        raise HTTPException(400, "Invalid symbol.")
    try:
        candles = _candle_provider.get_candles(symbol)
        if len(candles) < 120:
            raise ValueError("At least 120 completed candles are required.")
        report = walk_forward_report(candles)
        report["symbol"] = symbol
        report["data_source"] = "Binance public completed spot candles"
        report["candle_count"] = len(candles)
        return report
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, f"Backtest could not run: {exc}") from exc
    except Exception as exc:
        audit("WALK_FORWARD_FAILED", {"symbol": symbol, "error_type": type(exc).__name__})
        raise HTTPException(503, "Public candle history unavailable; no backtest result was produced.") from exc


@app.post("/api/research/walk-forward")
def research_walk_forward(data: BacktestInput):
    """Chronological train/validation/test report including simulated trading costs."""
    try:
        return walk_forward_report(
            data.candles, initial_cash=data.initial_cash, fee_bps=data.fee_bps,
            slippage_bps=data.slippage_bps, position_fraction=data.position_fraction,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, f"Walk-forward evaluation could not run: {exc}") from exc


def _news_veto(symbol: str) -> dict:
    """Negative news may veto an entry; positive headlines never force a BUY."""
    assets = {a["symbol"]: a for a in ASSETS}
    asset = assets.get(symbol, {"name": symbol})
    terms = {symbol.lower(), str(asset.get("name", symbol)).lower()}
    matched = []
    for article in news_provider.fetch(limit=60).get("items", []):
        text = (article.get("title", "") + " " + article.get("summary", "")).lower()
        if any(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text) for term in terms if len(term) >= 3):
            matched.append(article)
    if not matched:
        return {"veto_buy": False, "matched_articles": 0, "sentiment": "unknown",
                "reason": "No sufficiently direct recent news match; no sentiment assumption applied."}
    scored = [(float(x.get("sentiment", {}).get("score", 0)), float(x.get("sentiment", {}).get("confidence", 0))) for x in matched]
    confidence = max((conf for _, conf in scored), default=0)
    score = sum(value for value, _ in scored) / len(scored)
    # A single high-confidence negative direct mention is enough to veto an entry;
    # positive generic headlines must not wash out a specific exploit/hack story.
    veto = any(value < -0.15 and conf >= 0.5 for value, conf in scored)
    return {"veto_buy": veto, "matched_articles": len(matched), "sentiment_score": round(score, 3),
            "confidence": confidence, "sentiment": "negative" if score < -0.15 else "positive" if score > 0.15 else "neutral",
            "reason": "Negative recent headlines veto new BUY entries." if veto else "News is advisory; no negative-news veto triggered."}


@app.get("/api/metrics")
def metrics():
    ts=rows("SELECT * FROM trades ORDER BY id")
    sells=[t for t in ts if t["side"]=="SELL"]
    pnls=[t["realized_pnl"] for t in sells]
    wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<0]
    gross_win=sum(wins); gross_loss=abs(sum(losses))
    # Conservative proxy: cumulative realized P&L sequence; this is not a full historical mark-to-market equity curve.
    cum=peak=dd=0
    for p in pnls:
        cum+=p; peak=max(peak,cum); dd=max(dd,peak-cum)
    return {"closed_sell_orders":len(sells),"win_rate_pct":round(len(wins)/len(pnls)*100,2) if pnls else 0,
            "profit_factor":round(gross_win/gross_loss,2) if gross_loss else (None if gross_win==0 else "∞"),
            "max_drawdown_realized_proxy":round(dd,2),"fees":round(sum(t["fee"] for t in ts),2),
            "slippage":round(sum(t["slippage_cost"] for t in ts),2),
            "note":"Metrics are based on simulated fills. Drawdown is a realized-P&L proxy, not a full historical equity-curve drawdown."}
