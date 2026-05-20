import sqlite3
import pandas as pd
from datetime import datetime

# Connect to database
conn = sqlite3.connect('trade_journal.db')

# Query trades for today (May 18, 2026)
query = """
SELECT * FROM trades 
WHERE (opened_at LIKE '2026-05-18%' OR closed_at LIKE '2026-05-18%')
"""
df = pd.read_sql_query(query, conn)
conn.close()

if df.empty:
    print("No trades found for today (2026-05-18).")
else:
    # Filter only closed trades for PnL calculation
    closed_df = df[df['status'] == 'CLOSED']
    
    total_trades = len(df)
    closed_trades = len(closed_df)
    open_trades = total_trades - closed_trades
    
    wins = len(closed_df[closed_df['pnl_dollars'] > 0])
    losses = len(closed_df[closed_df['pnl_dollars'] < 0])
    breakevens = len(closed_df[closed_df['pnl_dollars'] == 0])
    
    win_rate = (wins / closed_trades * 100) if closed_trades > 0 else 0
    
    total_pnl = closed_df['pnl_dollars'].sum()
    gross_profit = closed_df[closed_df['pnl_dollars'] > 0]['pnl_dollars'].sum()
    gross_loss = closed_df[closed_df['pnl_dollars'] < 0]['pnl_dollars'].sum()
    
    best_trade = closed_df['pnl_dollars'].max() if closed_trades > 0 else 0
    worst_trade = closed_df['pnl_dollars'].min() if closed_trades > 0 else 0
    
    print(f"📊 **Monday (May 18) Performance Analysis** 📊")
    print(f"Total Trades Executed: {total_trades} ({closed_trades} closed, {open_trades} currently open)")
    print(f"Win Rate: {win_rate:.1f}% ({wins} Wins, {losses} Losses, {breakevens} Breakeven)")
    print(f"Net P&L: ${total_pnl:,.2f}")
    print(f"Gross Profit: ${gross_profit:,.2f}")
    print(f"Gross Loss: ${gross_loss:,.2f}")
    print(f"Best Trade: ${best_trade:,.2f}")
    print(f"Worst Trade: ${worst_trade:,.2f}")
    
    print("\n📝 **Trade Breakdown:**")
    for _, row in df.iterrows():
        status_icon = "🟢" if row['pnl_dollars'] > 0 else "🔴" if row['pnl_dollars'] < 0 else "⚪"
        if row['status'] == 'OPEN':
            status_icon = "🔄"
        pnl_str = f"${row['pnl_dollars']:,.2f}" if row['status'] == 'CLOSED' else "OPEN"
        print(f"{status_icon} {row['side']} @ {row['entry_price']} -> Exit: {row['exit_price']} | P&L: {pnl_str} | Reason: {row['exit_reason']}")

