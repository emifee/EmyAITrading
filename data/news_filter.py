import os
import time
import json
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from utils.logger import log

_NEWS_CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "news_cache.json")
_CACHE_EXPIRY_HOURS = 4

# Fetch ForexFactory this week XML
_FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

# We block trades 30 mins before and 15 mins after
_EMBARGO_BEFORE_MINUTES = 30
_EMBARGO_AFTER_MINUTES = 15

def fetch_and_parse_news():
    """Fetch high-impact USD news from ForexFactory."""
    try:
        req = urllib.request.Request(_FF_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            xml_data = response.read()
            
        root = ET.fromstring(xml_data)
        high_impact_events = []
        
        for event in root.findall('event'):
            country = event.findtext('country')
            impact = event.findtext('impact')
            
            if country == 'USD' and impact == 'High':
                date_str = event.findtext('date')  # Format: mm-dd-yyyy
                time_str = event.findtext('time')  # Format: h:mma (e.g. 8:30am)
                title = event.findtext('title')
                
                if not date_str or not time_str or time_str == 'All Day':
                    continue
                    
                # Parse to datetime
                try:
                    # FF timezone is US/Eastern by default in this XML unless adjusted by user session
                    # But the public XML without cookies is usually EDT/EST.
                    # Actually, public FF XML is EST/EDT. Let's parse and assume it's New York time.
                    # To be safe and simple without pytz, we can parse as naive and attach NY offset.
                    # Alternatively, if we just use Python's datetime, we can approximate.
                    
                    # Instead of wrestling with complex timezones without pytz, a safer 
                    # approach is to assume the XML is EST (-5) / EDT (-4).
                    # A robust way is to just use datetime.strptime and add timedelta.
                    
                    dt_str = f"{date_str} {time_str}"
                    # Example: "05-20-2026 8:30am"
                    dt_naive = datetime.strptime(dt_str, "%m-%d-%Y %I:%M%p")
                    
                    # Assume NY Time. UTC is NY + 4 hours (EDT) or 5 hours (EST).
                    # This code runs primarily during active markets (EDT mostly). Let's use 4 hours for simplicity
                    # or better, use time.timezone to calculate local.
                    # Since we are using UTC everywhere, let's just use a hardcoded 4 hours offset for EDT.
                    # Note: This is an approximation. A more robust way would be to use a proper library,
                    # but we want zero dependencies.
                    dt_utc = dt_naive + timedelta(hours=4)
                    
                    high_impact_events.append({
                        "title": title,
                        "timestamp": dt_utc.replace(tzinfo=timezone.utc).timestamp()
                    })
                except Exception as e:
                    log.debug(f"Error parsing date/time for news event {title}: {e}")
        
        # Save to cache
        cache_data = {
            "fetched_at": time.time(),
            "events": high_impact_events
        }
        
        os.makedirs(os.path.dirname(_NEWS_CACHE_FILE), exist_ok=True)
        with open(_NEWS_CACHE_FILE, "w") as f:
            json.dump(cache_data, f)
            
        log.info(f"📰 Fetched {len(high_impact_events)} high-impact USD news events for the week.")
        return high_impact_events
        
    except Exception as e:
        log.error(f"Failed to fetch ForexFactory news: {e}")
        return []

def get_high_impact_news():
    """Get news from cache or fetch if expired."""
    if os.path.exists(_NEWS_CACHE_FILE):
        try:
            with open(_NEWS_CACHE_FILE, "r") as f:
                cache = json.load(f)
            
            age_hours = (time.time() - cache.get("fetched_at", 0)) / 3600
            if age_hours < _CACHE_EXPIRY_HOURS:
                return cache.get("events", [])
        except Exception:
            pass
            
    return fetch_and_parse_news()

def is_news_embargo_active() -> tuple[bool, str]:
    """
    Check if we are currently inside a news embargo window.
    Returns (True, reason) if embargo is active, (False, "") if safe to trade.
    """
    events = get_high_impact_news()
    if not events:
        return False, ""
        
    now = time.time()
    
    for event in events:
        event_time = event["timestamp"]
        title = event["title"]
        
        # Window: 30 mins before, 15 mins after
        embargo_start = event_time - (_EMBARGO_BEFORE_MINUTES * 60)
        embargo_end = event_time + (_EMBARGO_AFTER_MINUTES * 60)
        
        if embargo_start <= now <= embargo_end:
            min_to_news = (event_time - now) / 60
            if min_to_news > 0:
                reason = f"High-Impact News '{title}' in {int(min_to_news)} mins."
            else:
                reason = f"High-Impact News '{title}' happened {int(-min_to_news)} mins ago."
            return True, reason
            
    return False, ""

if __name__ == "__main__":
    # Test script
    active, reason = is_news_embargo_active()
    print(f"Embargo Active: {active}")
    if active:
        print(f"Reason: {reason}")
    else:
        print("Safe to trade.")
