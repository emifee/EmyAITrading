"""
trade_journal.py — SQLite trade journal for profitability tracking.

Logs every trade with entry/exit details, P&L, and Claude's reasoning.
Provides stats queries for win rate, total P&L, avg R:R, etc.
"""

import sqlite3
import os
from datetime import datetime, timezone, timedelta
from utils.logger import log

# Database path — stored alongside main.py
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "trade_journal.db")


def _get_conn():
    """Get a thread-safe SQLite connection."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_journal():
    """Create the trades table if it doesn't exist."""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            symbol TEXT NOT NULL DEFAULT 'XAUUSD',
            side TEXT NOT NULL,
            volume REAL NOT NULL,
            entry_price REAL NOT NULL,
            stop_loss REAL,
            take_profit REAL,
            exit_price REAL,
            exit_reason TEXT,
            pnl_dollars REAL DEFAULT 0,
            pnl_pips REAL DEFAULT 0,
            risk_reward REAL,
            confidence INTEGER,
            session_grade TEXT,
            sweep_detected INTEGER DEFAULT 0,
            claude_reason TEXT,
            position_id TEXT,
            status TEXT DEFAULT 'OPEN',
            regime TEXT DEFAULT 'UNKNOWN'
        )
    """)
    
    # Simple migration: try to add the column if it doesn't exist
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN regime TEXT DEFAULT 'UNKNOWN'")
    except Exception:
        pass  # Column already exists
        
    conn.commit()
    conn.close()
    log.info(f"📓 Trade journal initialized ({DB_PATH})")


def log_trade_open(side, volume, entry_price, stop_loss, take_profit,
                    confidence=0, session_grade="", sweep_detected=False,
                    reason="", position_id="", regime="UNKNOWN"):
    """Log a new trade when opened."""
    conn = _get_conn()
    conn.execute("""
        INSERT INTO trades (
            opened_at, side, volume, entry_price, stop_loss, take_profit,
            confidence, session_grade, sweep_detected, claude_reason,
            position_id, status, regime
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        side, volume, entry_price, stop_loss, take_profit,
        confidence, session_grade, 1 if sweep_detected else 0,
        reason, str(position_id), regime
    ))
    conn.commit()
    trade_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    log.info(f"📓 Trade #{trade_id} logged: {side} @ ${entry_price:,.2f}")
    return trade_id


def log_trade_close(position_id, exit_price, exit_reason="TP/SL"):
    """Log a trade close with P&L calculation."""
    conn = _get_conn()

    # Try to find by position_id first
    trade = conn.execute(
        "SELECT * FROM trades WHERE position_id = ? AND status = 'OPEN' ORDER BY id DESC LIMIT 1",
        (str(position_id),)
    ).fetchone()

    # Fallback: find the most recent open trade
    if not trade:
        trade = conn.execute(
            "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    if not trade:
        conn.close()
        log.warning(f"No open trade found for position {position_id}")
        return

    entry = trade["entry_price"]
    side = trade["side"]
    volume = trade["volume"]

    # Calculate P&L
    if side == "BUY":
        pnl_pips = exit_price - entry
    else:
        pnl_pips = entry - exit_price

    pnl_dollars = round(pnl_pips * volume, 2)

    # Calculate actual R:R achieved
    sl = trade["stop_loss"]
    risk = abs(entry - sl) if sl else 0
    rr = round(pnl_pips / risk, 2) if risk > 0 else 0

    conn.execute("""
        UPDATE trades SET
            closed_at = ?,
            exit_price = ?,
            exit_reason = ?,
            pnl_dollars = ?,
            pnl_pips = ?,
            risk_reward = ?,
            status = 'CLOSED'
        WHERE id = ?
    """, (
        datetime.now(timezone.utc).isoformat(),
        exit_price, exit_reason, pnl_dollars, round(pnl_pips, 2), rr,
        trade["id"],
    ))
    conn.commit()
    conn.close()

    emoji = "🟢" if pnl_dollars >= 0 else "🔴"
    log.info(f"📓 Trade #{trade['id']} closed: {emoji} ${pnl_dollars:,.2f} ({exit_reason})")
    return pnl_dollars


def get_open_trades():
    """Get all open trades."""
    conn = _get_conn()
    trades = conn.execute(
        "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [dict(t) for t in trades]


def get_recent_trades(limit=10):
    """Get the most recent closed trades."""
    conn = _get_conn()
    trades = conn.execute(
        "SELECT * FROM trades WHERE status = 'CLOSED' ORDER BY closed_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(t) for t in trades]


def get_stats():
    """Get comprehensive trading statistics."""
    conn = _get_conn()

    # All closed trades
    closed = conn.execute(
        "SELECT * FROM trades WHERE status = 'CLOSED'"
    ).fetchall()

    if not closed:
        conn.close()
        return {
            "total_trades": 0, "open_trades": 0,
            "wins": 0, "losses": 0, "win_rate": 0,
            "total_pnl": 0, "avg_pnl": 0,
            "best_trade": 0, "worst_trade": 0,
            "avg_rr": 0, "total_volume": 0,
        }

    # Count open trades
    open_count = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE status = 'OPEN'"
    ).fetchone()[0]

    pnls = [t["pnl_dollars"] for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    rrs = [t["risk_reward"] for t in closed if t["risk_reward"] is not None]

    # Today's trades
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_trades = conn.execute(
        "SELECT pnl_dollars FROM trades WHERE status = 'CLOSED' AND closed_at LIKE ?",
        (f"{today}%",)
    ).fetchall()
    today_pnl = sum(t["pnl_dollars"] for t in today_trades)

    conn.close()

    stats = {
        "total_trades": len(closed),
        "open_trades": open_count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "total_pnl": round(sum(pnls), 2),
        "today_pnl": round(today_pnl, 2),
        "today_trades": len(today_trades),
        "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0,
        "best_trade": round(max(pnls), 2) if pnls else 0,
        "worst_trade": round(min(pnls), 2) if pnls else 0,
        "avg_rr": round(sum(rrs) / len(rrs), 2) if rrs else 0,
        "avg_winner": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loser": round(sum(losses) / len(losses), 2) if losses else 0,
    }

    return stats


def get_time_reports():
    """Get performance reports grouped by Today, This Week, and This Month."""
    conn = _get_conn()
    closed = conn.execute("SELECT * FROM trades WHERE status = 'CLOSED'").fetchall()
    conn.close()

    reports = {
        "today": {"trades": 0, "wins": 0, "pnl": 0.0, "win_rate": 0.0},
        "week": {"trades": 0, "wins": 0, "pnl": 0.0, "win_rate": 0.0},
        "month": {"trades": 0, "wins": 0, "pnl": 0.0, "win_rate": 0.0},
    }

    if not closed:
        return reports

    now = datetime.now(timezone.utc)
    today_date = now.date()
    start_of_week = today_date - timedelta(days=today_date.weekday())
    start_of_month = today_date.replace(day=1)

    for row in closed:
        closed_at_str = row["closed_at"]
        if not closed_at_str:
            continue
            
        try:
            trade_date = datetime.fromisoformat(closed_at_str.replace('Z', '+00:00')).date()
        except ValueError:
            continue

        pnl = row["pnl_dollars"]
        is_win = 1 if pnl > 0 else 0

        if trade_date == today_date:
            reports["today"]["trades"] += 1
            reports["today"]["wins"] += is_win
            reports["today"]["pnl"] += pnl
            
        if trade_date >= start_of_week:
            reports["week"]["trades"] += 1
            reports["week"]["wins"] += is_win
            reports["week"]["pnl"] += pnl
            
        if trade_date >= start_of_month:
            reports["month"]["trades"] += 1
            reports["month"]["wins"] += is_win
            reports["month"]["pnl"] += pnl

    for key in reports:
        trades = reports[key]["trades"]
        reports[key]["win_rate"] = round((reports[key]["wins"] / trades * 100), 1) if trades > 0 else 0.0
        reports[key]["pnl"] = round(reports[key]["pnl"], 2)

    return reports


def get_loss_patterns():
    """Analyze losing trades to find common failure patterns.
    
    Returns a dict with:
    - side_stats: win rate by BUY vs SELL
    - session_stats: win rate by session grade
    - avg_hold_time: average trade duration for wins vs losses
    - losing_reasons: common reasons from Claude on losing trades
    - streak: current win/loss streak
    """
    conn = _get_conn()
    closed = conn.execute(
        "SELECT * FROM trades WHERE status = 'CLOSED' ORDER BY closed_at DESC"
    ).fetchall()
    conn.close()

    if not closed:
        return None
        
    closed = [dict(t) for t in closed]

    patterns = {}

    # ─── Win rate by side (BUY vs SELL) ───────────────────
    buy_trades = [t for t in closed if t["side"] == "BUY"]
    sell_trades = [t for t in closed if t["side"] == "SELL"]
    
    buy_wins = len([t for t in buy_trades if t["pnl_dollars"] > 0])
    sell_wins = len([t for t in sell_trades if t["pnl_dollars"] > 0])
    
    patterns["side_analysis"] = {
        "buy_total": len(buy_trades),
        "buy_wins": buy_wins,
        "buy_wr": round(buy_wins / len(buy_trades) * 100, 1) if buy_trades else 0,
        "buy_pnl": round(sum(t["pnl_dollars"] for t in buy_trades), 2),
        "sell_total": len(sell_trades),
        "sell_wins": sell_wins,
        "sell_wr": round(sell_wins / len(sell_trades) * 100, 1) if sell_trades else 0,
        "sell_pnl": round(sum(t["pnl_dollars"] for t in sell_trades), 2),
    }

    # ─── Win rate by session grade ────────────────────────
    session_stats = {}
    for grade in ["A", "B", "C"]:
        grade_trades = [t for t in closed if t.get("session_grade") == grade]
        if grade_trades:
            grade_wins = len([t for t in grade_trades if t["pnl_dollars"] > 0])
            session_stats[grade] = {
                "total": len(grade_trades),
                "wins": grade_wins,
                "wr": round(grade_wins / len(grade_trades) * 100, 1),
                "pnl": round(sum(t["pnl_dollars"] for t in grade_trades), 2),
            }
    patterns["session_stats"] = session_stats

    # ─── Current streak ───────────────────────────────────
    streak = 0
    streak_type = None
    for t in closed:
        is_win = t["pnl_dollars"] > 0
        if streak_type is None:
            streak_type = is_win
            streak = 1
        elif is_win == streak_type:
            streak += 1
        else:
            break
    patterns["streak"] = f"{'W' if streak_type else 'L'}{streak}"

    # ─── Losing trade reasons ─────────────────────────────
    losers = [t for t in closed if t["pnl_dollars"] < 0]
    losing_reasons = []
    for t in losers[-5:]:  # Last 5 losers
        reason = t.get("claude_reason") or t.get("exit_reason") or "unknown"
        losing_reasons.append({
            "side": t["side"],
            "pnl": t["pnl_dollars"],
            "session": t.get("session_grade", "?"),
            "reason": reason[:150],
        })
    patterns["losing_reasons"] = losing_reasons

    # ─── Biggest lesson summary ───────────────────────────
    sa = patterns["side_analysis"]
    lessons = []
    if sa["buy_total"] > 0 and sa["sell_total"] > 0:
        if sa["buy_wr"] > sa["sell_wr"] + 20:
            lessons.append(f"BUY trades win {sa['buy_wr']}% vs SELL {sa['sell_wr']}% — FAVOR BUYS")
        elif sa["sell_wr"] > sa["buy_wr"] + 20:
            lessons.append(f"SELL trades win {sa['sell_wr']}% vs BUY {sa['buy_wr']}% — FAVOR SELLS")
    
    if sa["sell_total"] >= 3 and sa["sell_wr"] < 40:
        lessons.append(f"SELL trades are losing ({sa['sell_wr']}% WR, ${sa['sell_pnl']}) — AVOID SELLING unless A-grade setup")
    if sa["buy_total"] >= 3 and sa["buy_wr"] < 40:
        lessons.append(f"BUY trades are losing ({sa['buy_wr']}% WR, ${sa['buy_pnl']}) — AVOID BUYING unless A-grade setup")

    patterns["lessons"] = lessons

    return patterns


# Initialize on import
init_journal()
