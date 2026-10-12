from __future__ import annotations
import json, math, sqlite3, time
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional
from threading import RLock
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from app.market_data import CoinGeckoFreeProvider
from app.strategy import combined_signal
from app.backtest import BacktestConfig, run_backtest
from app.auto_monitor import MonitorConfig, PaperSignalMonitor
from app.candle_data import BinancePublicCandleProvider
from app.auto_scheduler import PaperAutoScheduler

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT.parent / "paper_agent.db"
app = FastAPI(title="Multi-Chain Paper Trading Agent", version="0.2.0")
live_provider = CoinGeckoFreeProvider()

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
    "market_data_mode": "synthetic"
}
_order_lock = RLock()
def now_iso(): return datetime.now(timezone.utc).isoformat()
def connect():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c
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
    for p in pos:
        px=mk.get(p["symbol"],{}).get("price",0)
        p["mark_price"]=px; p["market_value"]=p["qty"]*px
        p["unrealized_pnl"]=p["market_value"]-p["cost"]
        unreal+=p["unrealized_pnl"]; exposure+=p["market_value"]
    realized=sum(t["realized_pnl"] for t in trades if t["side"]=="SELL")
    fees=sum(t["fee"] for t in trades); slip=sum(t["slippage_cost"] for t in trades)
    equity=cash+exposure
    total_topups=sum(a["amount"] for a in adjustments)
    return {"virtual_cash":round(cash,2),"equity":round(equity,2),"starting_virtual_cash":s["virtual_cash"],
            "virtual_topups":round(total_topups,2),
            "exposure":round(exposure,2),"realized_pnl":round(realized,2),"unrealized_pnl":round(unreal,2),
            "net_pnl":round(equity-s["virtual_cash"]-total_topups,2),"fees_paid":round(fees,2),"slippage_cost":round(slip,2),
            "positions":pos,"position_count":len(pos)}
class Order(BaseModel):
    symbol: str
    side: str
    amount: float = Field(gt=0, description="Virtual quote-currency amount")
    sell_percent: Optional[float] = Field(default=None, gt=0, le=100)
class SettingsUpdate(BaseModel):
    fee_bps: Optional[float] = Field(default=None, ge=0, le=500)
    slippage_bps: Optional[float] = Field(default=None, ge=0, le=500)
    max_trade_pct: Optional[float] = Field(default=None, gt=0, le=100)
    daily_loss_limit_pct: Optional[float] = Field(default=None, gt=0, le=100)
    max_positions: Optional[int] = Field(default=None, ge=1, le=30)
    auto_monitor_enabled: Optional[bool] = None
    virtual_cash: Optional[float] = Field(default=None, ge=1000, le=100000000)
    max_drawdown_pct: Optional[float] = Field(default=None, gt=0, le=100)
    max_exposure_pct: Optional[float] = Field(default=None, gt=0, le=100)
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
def health(): return {"ok":True,"mode":"PAPER_ONLY","data":"SYNTHETIC DEMO","live_trading":False}
@app.get("/api/market")
def get_market():
    mode = settings().get("market_data_mode", "synthetic")
    return {"data_status": "SYNTHETIC DEMO" if mode == "synthetic" else "LIVE PUBLIC DATA — unavailable quotes are blocked",
            "provider": mode, "items": market()}

@app.get("/api/market/live")
def get_live_market(symbols: str = "SOL,ETH,ARB,BNB,JUP,AERO,STRK,PENGU,TIA,HYPE,TRUMP,USDC"):
    """Fetch free public prices. This endpoint does not feed orders or replace demo prices."""
    requested = [s.strip().upper() for s in symbols.split(",") if s.strip()][:30]
    data = live_provider.get_prices(requested)
    return {"mode":"LIVE_PUBLIC_DATA_READ_ONLY", "new_entries_allowed": all(x.status == "fresh" for x in data.values()) and bool(data),
            "items":[{"symbol":x.symbol,"price_usd":x.price,"provider":x.provider,"fetched_at":x.fetched_at,"status":x.status,"error":x.error} for x in data.values()]}

class CandleInput(BaseModel):
    candles: list[dict] = Field(min_length=2, max_length=5000)

class BacktestInput(CandleInput):
    initial_cash: float = Field(default=100000, gt=0, le=1000000000)
    fee_bps: float = Field(default=10, ge=0, le=500)
    slippage_bps: float = Field(default=8, ge=0, le=500)
    allow_shorts: bool = True
    position_fraction: float = Field(default=0.10, gt=0, le=1)

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
    if "market_data_mode" in data and data["market_data_mode"] not in ("synthetic", "live_public"):
        raise HTTPException(400, "market_data_mode must be 'synthetic' or 'live_public'.")
    if data.get("market_data_mode") == "live_public":
        live_prices = live_provider.get_prices([a["symbol"] for a in ASSETS if a["symbol"] != "USDC"])
        if not live_prices or any(p.price is None or p.status != "fresh" for p in live_prices.values()):
            raise HTTPException(503, "Public market data is incomplete or stale; provider mode was not changed.")
    for k,v in data.items():
        if k=="virtual_cash" and rows("SELECT id FROM trades LIMIT 1"):
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
    with connect() as c:
        c.execute("DELETE FROM trades"); c.execute("DELETE FROM audit"); c.execute("DELETE FROM cash_adjustments"); c.commit()
    set_setting("paused",False); audit("SIMULATION_RESET",{"message":"Trade history cleared; virtual cash reset"})
    return {"ok":True}
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
        if realized_today <= -daily_loss_limit:
            set_setting("paused", True)
            audit("DAILY_LOSS_LIMIT_TRIGGERED", {
                "realized_pnl_today": realized_today, "loss_limit": daily_loss_limit,
            })
            raise HTTPException(400, "Daily realized-loss limit reached. Paper trading has been paused.")

        p = portfolio()
        initial_equity = float(s["virtual_cash"]) + float(p.get("virtual_topups", 0))
        drawdown_pct = max(0.0, (initial_equity - float(p["equity"])) / initial_equity * 100) if initial_equity > 0 else 0.0
        drawdown_limit = float(s.get("max_drawdown_pct", DEFAULTS["max_drawdown_pct"]))
        if drawdown_pct >= drawdown_limit:
            set_setting("paused", True)
            audit("MAX_DRAWDOWN_TRIGGERED", {
                "drawdown_pct": round(drawdown_pct, 4), "limit_pct": drawdown_limit,
            })
            raise HTTPException(400, "Maximum portfolio drawdown reached. Paper trading has been paused.")

        mk = {x["symbol"]: x for x in market()}
        if o.symbol not in mk:
            raise HTTPException(400, "Unknown asset.")
        asset = mk[o.symbol]
        if o.amount > p["equity"] * s["max_trade_pct"] / 100:
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
        "interval_seconds": 300,
        "mode": "PAPER_AUTO_TRADE" if s.get("auto_monitor_enabled", False) else "AUDIT_ONLY",
        "orders_submitted": False,
        "scheduler_running": bool(_auto_scheduler and _auto_scheduler.running),
        "interval_seconds": _auto_scheduler.interval_seconds if _auto_scheduler else 300,
        "candle_provider": "binance_public_klines",
        "message": (
            "Scheduler is active; paper cycles run every five minutes while enabled and unpaused."
            if _auto_scheduler and _auto_scheduler.running and s.get("auto_monitor_enabled", False)
            else "Scheduler is idle unless automated paper trading is enabled; all execution remains simulated."
        ),
    }


class AutoMonitorEvaluation(BaseModel):
    symbols: list[str] = Field(default=["SOL", "ETH", "ARB", "BNB"], min_length=1, max_length=30)


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
    s = settings()
    if not s.get("auto_monitor_enabled", False):
        raise HTTPException(400, "Automated paper trading is disabled. Enable it in settings first.")
    if s.get("paused", False):
        raise HTTPException(400, "Paper trading is paused.")
    symbols = list(dict.fromkeys(x.strip().upper() for x in request.symbols if x.strip()))[:30]
    monitor = _build_signal_monitor()
    result = monitor.evaluate(symbols)
    executed = []
    for item in result.get("results", []):
        symbol = item.get("symbol", "")
        signal = item.get("signal", "HOLD").upper()
        if signal not in ("BUY", "SELL") or symbol not in {a["symbol"] for a in ASSETS}:
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
    audit("AUTO_PAPER_CYCLE_COMPLETED", {"symbols": symbols, "orders_submitted": False, "paper_fills": sum(x["status"] == "paper_filled" for x in executed), "results": executed})
    return {"mode": "PAPER_ONLY", "live_trading": False, "orders_submitted": False, "evaluated": result.get("evaluated", 0), "results": result.get("results", []), "paper_actions": executed}


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
