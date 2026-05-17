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
        df = pd.read_sql_query("SELECT * FROM trades", conn)
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

    with open(REPORT_PATH, "w") as f:
        f.write(f"📊 EMY AI — BIG DATA EDGE REPORT\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Total Trades Analyzed: {len(df)}\n")
        f.write(f"Overall Win Rate: {(df['is_win'].mean() * 100):.1f}%\n")
        f.write("="*50 + "\n\n")

        # 1. Edge by Market Regime
        f.write("📈 1. WIN RATE BY MARKET REGIME\n")
        f.write("-" * 30 + "\n")
        regime_stats = df.groupby('regime').agg(
            trades=('is_win', 'count'),
            win_rate=('is_win', 'mean'),
            avg_pnl=('pnl_dollars', 'mean')
        ).reset_index()
        
        for _, row in regime_stats.iterrows():
            f.write(f"Regime: {row['regime']:<15} | Trades: {row['trades']:<3} | Win Rate: {(row['win_rate']*100):.1f}% | Avg PnL: ${row['avg_pnl']:.2f}\n")
            if row['trades'] >= 5 and row['win_rate'] < 0.40:
                f.write(f"   ⚠️ WARNING: You are bleeding money in {row['regime']}. Consider blocking this regime.\n")
        f.write("\n")

        # 2. Edge by Hour of Day
        f.write("⏰ 2. WIN RATE BY HOUR OF DAY (UTC)\n")
        f.write("-" * 30 + "\n")
        hour_stats = df.groupby('hour').agg(
            trades=('is_win', 'count'),
            win_rate=('is_win', 'mean'),
            total_pnl=('pnl_dollars', 'sum')
        ).reset_index()
        
        for _, row in hour_stats.iterrows():
            f.write(f"Hour {int(row['hour']):02d}:00 UTC | Trades: {row['trades']:<3} | Win Rate: {(row['win_rate']*100):.1f}% | Total PnL: ${row['total_pnl']:.2f}\n")
            if row['trades'] >= 3 and row['win_rate'] < 0.33:
                f.write(f"   🛑 DEAD ZONE DETECTED: Avoid trading at {int(row['hour']):02d}:00 UTC.\n")
        f.write("\n")

        # 3. Edge by Side (Buy vs Sell)
        f.write("🔄 3. WIN RATE BY DIRECTION (BUY vs SELL)\n")
        f.write("-" * 30 + "\n")
        side_stats = df.groupby('side').agg(
            trades=('is_win', 'count'),
            win_rate=('is_win', 'mean'),
            avg_pnl=('pnl_dollars', 'mean')
        ).reset_index()
        
        for _, row in side_stats.iterrows():
            f.write(f"Side: {row['side']:<5} | Trades: {row['trades']:<3} | Win Rate: {(row['win_rate']*100):.1f}%\n")
        f.write("\n")

    print(f"✅ ML Edge Analysis complete! Report saved to {REPORT_PATH}")

if __name__ == "__main__":
    print("🧠 Booting Emy AI Big Data Engine...")
    analyze_edges()
