"""
macro_feed.py — Fetches live macroeconomic news from ForexFactory's free XML feed.
"""

import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import logging

log = logging.getLogger("macro_feed")

# ─── URL ──────────────────────────────────────────────────────────
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

def get_live_macro_news(max_events=5):
    """
    Fetches the latest high/medium impact economic news for USD and CNY
    from ForexFactory's free weekly XML feed.
    """
    try:
        req = urllib.request.Request(
            FF_URL, 
            headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            xml_data = response.read()

        root = ET.fromstring(xml_data)
        
        events = []
        for event in root.findall('event'):
            country = event.find('country').text
            impact = event.find('impact').text
            title = event.find('title').text
            date_str = event.find('date').text
            time_str = event.find('time').text
            forecast = event.find('forecast').text if event.find('forecast') is not None else "N/A"
            previous = event.find('previous').text if event.find('previous') is not None else "N/A"
            
            # Filter for USD (and CNY because China drives Gold physical demand)
            if country in ['USD', 'CNY'] and impact in ['High', 'Medium']:
                events.append({
                    "title": title,
                    "country": country,
                    "impact": impact,
                    "date": date_str,
                    "time": time_str,
                    "forecast": forecast,
                    "previous": previous
                })
        
        # Sort by impact (High first) then just take top N
        # (The XML is already chronological)
        high_impact = [e for e in events if e['impact'] == 'High']
        medium_impact = [e for e in events if e['impact'] == 'Medium']
        
        combined = (high_impact + medium_impact)[:max_events]
        
        if not combined:
            return "No high/medium impact US or China news scheduled for this week."
            
        news_text = ""
        for i, e in enumerate(combined):
            news_text += f"{i+1}. [{e['impact']}] {e['country']} {e['title']} (Scheduled: {e['date']} {e['time']})\n"
            news_text += f"   Forecast: {e['forecast']} | Previous: {e['previous']}\n"
            
        return news_text.strip()

    except Exception as e:
        log.warning(f"Failed to fetch macroeconomic news: {e}")
        return "Macroeconomic news feed currently unavailable."
