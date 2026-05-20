import sys, os
from dotenv import load_dotenv
load_dotenv()
from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from twisted.internet import reactor, defer
from data.tick_aggregator import tick_agg
from utils.ctrader_helpers import get_trendbars

CLIENT_ID = os.getenv("CTRADER_CLIENT_ID")
CLIENT_SECRET = os.getenv("CTRADER_CLIENT_SECRET")
client = Client(EndPoints.PROTOBUF_LIVE_HOST, EndPoints.PROTOBUF_PORT, TcpProtocol)

@defer.inlineCallbacks
def on_connected(c):
    try:
        req = ProtoOAApplicationAuthReq()
        req.clientId = CLIENT_ID
        req.clientSecret = CLIENT_SECRET
        yield client.send(req)
        
        print("\n📊 Fetching recent XAUUSD market data...")
        raw_candles = yield get_trendbars(client, "XAUUSD", period_minutes=15, count=25)
        if raw_candles:
            tick_agg.load_historical(raw_candles, timeframe=15)
            df = tick_agg.get_candles(15)
            
            print(f"\n   Last 10 candles (XAUUSD, 15m):")
            for b in list(df.itertuples())[-10:]:
                range_size = b.high - b.low
                print(f"   {b.timestamp} | O: ${b.open:,.2f} H: ${b.high:,.2f} L: ${b.low:,.2f} C: ${b.close:,.2f} | Range: ${range_size:.2f}")
                
            total_high = df['high'][-10:].max()
            total_low = df['low'][-10:].max()
            print(f"\n   Total Range over last 2.5 hours: ${abs(df['high'][-10:].max() - df['low'][-10:].min()):.2f}")
            
    except Exception as e:
        print(f"Exception: {e}")
    reactor.stop()

client.setConnectedCallback(on_connected)
client.startService()
reactor.run()
