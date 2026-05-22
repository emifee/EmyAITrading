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

import time

# Cache variables to prevent HTTP 429 Too Many Requests
_last_fetch_time = 0
_cached_news = None
_CACHE_DURATION_SECONDS = 4 * 60 * 60  # Cache for 4 hours

def get_live_macro_news(max_events=5):
    """
    Fetches the latest high/medium impact economic news for USD and CNY
    from ForexFactory's free weekly XML feed. Uses a 4-hour cache.
    """
    global _last_fetch_time, _cached_news
    
    # Return cached data if valid
    if time.time() - _last_fetch_time < _CACHE_DURATION_SECONDS and _cached_news is not None:
        return _cached_news

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
            _cached_news = "No high/medium impact US or China news scheduled for this week."
            _last_fetch_time = time.time()
            return _cached_news
            
        news_text = ""
        for i, e in enumerate(combined):
            news_text += f"{i+1}. [{e['impact']}] {e['country']} {e['title']} (Scheduled: {e['date']} {e['time']})\n"
            news_text += f"   Forecast: {e['forecast']} | Previous: {e['previous']}\n"
            
        _cached_news = news_text.strip()
        _last_fetch_time = time.time()
        return _cached_news

    except Exception as e:
        if "429" in str(e):
            log.info("Macro news feed rate limited (HTTP 429). Will retry in 4 hours.")
        else:
            log.info(f"Macro news feed temporarily unavailable: {e}")
            
        _cached_news = "Macroeconomic news feed currently unavailable."
        _last_fetch_time = time.time()
        return _cached_news
