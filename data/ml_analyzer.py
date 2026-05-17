"""
ml_analyzer.py — The "Big Data" Machine Learning Loop.
Extracts the SQLite trade journal, runs statistical analysis to find hidden edges, 
and generates an edge_report.txt to help us hardcode "No Trade Zones".
"""

import sqlite3
import pandas as pd
import os
from datetime import datetime

# Path to DB
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "trade_journal.db")
REPORT_PATH = os.path.join(os.path.dirname(__file__), "edge_report.txt")

def analyze_edges():
    """
    Connects to the SQLite DB, converts it to Pandas, and performs 
    deep statistical grouping to find when the bot wins and loses.
    """
    if not os.path.exists(DB_PATH):
        print("❌ No trade_journal.db found yet. Cannot run ML analysis.")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        # Use a sliding window of the last 100 trades so data is always fresh and dynamic
        df = pd. pd.read_sql_query("SELECT * FROM trades ORDER BY opened_at DESC LIMIT 100", conn)
        conn.close()
    except Exception as e:
        print(f"Failed to load DB: {e}")
        return

    if len(df) < 10:
        print("⚠️ Not enough trades to find a statistical edge (need at least 10).")
        return

    # Convert timestamps
    df['opened_at'] = pd.to_datetime(df['opened_at'])
    df['hour'] = df['opened_at'].dt.hour
    df['day_of_week'] = df['opened_at'].dt.day_name()
    
    # Define a win vs loss
    df['is_win'] = df['pnl_dollars'] > 0

    report_lines = []
    report_lines.append(f"📊 EMY AI — BIG DATA EDGE REPORT (Last {len(df)} Trades)")
    report_lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report_lines.append(f"Overall Win Rate: {(df['is_win'].mean() * 100):.1f}%")
    report_lines.append("="*50 + "\n")

    # 1. Edge by Market Regime
    report_lines.append("📈 1. WIN RATE BY MARKET REGIME")
    report_lines.append("-" * 30)
    regime_stats = df.groupby('regime').agg(
        trades=('is_win', 'count'),
        win_rate=('is_win', 'mean'),
        avg_pnl=('pnl_dollars', 'mean')
    ).reset_index()
    
    for _, row in regime_stats.iterrows():
        report_lines.append(f"Regime: {row['regime']:<15} | Trades: {row['trades']:<3} | Win Rate: {(row['win_rate']*100):.1f}% | Avg PnL: ${row['avg_pnl']:.2f}")
        if row['trades'] >= 5 and row['win_rate'] < 0.40:
            report_lines.append(f"   ⚠️ WARNING: You are bleeding money in {row['regime']}. Consider blocking this regime.")
    report_lines.append("")

    # 2. Edge by Hour of Day
    report_lines.append("⏰ 2. WIN RATE BY HOUR OF DAY (UTC)")
    report_lines.append("-" * 30)
    hour_stats = df.groupby('hour').agg(
        trades=('is_win', 'count'),
        win_rate=('is_win', 'mean'),
        total_pnl=('pnl_dollars', 'sum')
    ).reset_index()
    
    for _, row in hour_stats.iterrows():
        report_lines.append(f"Hour {int(row['hour']):02d}:00 UTC | Trades: {row['trades']:<3} | Win Rate: {(row['win_rate']*100):.1f}% | Total PnL: ${row['total_pnl']:.2f}")
        if row['trades'] >= 3 and row['win_rate'] < 0.33:
            report_lines.append(f"   🛑 DEAD ZONE DETECTED: Avoid trading at {int(row['hour']):02d}:00 UTC.")
    report_lines.append("")

    # 3. Edge by Side (Buy vs Sell)
    report_lines.append("🔄 3. WIN RATE BY DIRECTION (BUY vs SELL)")
    report_lines.append("-" * 30)
    side_stats = df.groupby('side').agg(
        trades=('is_win', 'count'),
        win_rate=('is_win', 'mean'),
        avg_pnl=('pnl_dollars', 'mean')
    ).reset_index()
    
    for _, row in side_stats.iterrows():
        report_lines.append(f"Side: {row['side']:<5} | Trades: {row['trades']:<3} | Win Rate: {(row['win_rate']*100):.1f}%")
    report_lines.append("")

    report_str = "\n".join(report_lines)

    # Save to file for human readability
    with open(REPORT_PATH, "w") as f:
        f.write(report_str)

    return report_str

if __name__ == "__main__":
    print("🧠 Booting Emy AI Big Data Engine...")
    analyze_edges()
