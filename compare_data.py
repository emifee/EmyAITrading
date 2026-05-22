import os
import sys
from twisted.internet import reactor, defer
from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from dotenv import load_dotenv

load_dotenv()

from collections import deque
class FixedTcpProtocol(TcpProtocol):
    def connectionMade(self):
        self._send_queue = deque([])
        self._send_task = None
        self._lastSendMessageTime = None
        super().connectionMade()

client_live = Client(EndPoints.PROTOBUF_LIVE_HOST, EndPoints.PROTOBUF_PORT, FixedTcpProtocol)
client_demo = Client(EndPoints.PROTOBUF_DEMO_HOST, EndPoints.PROTOBUF_PORT, FixedTcpProtocol)

live_bid = 0.0
live_ask = 0.0
demo_bid = 0.0
demo_ask = 0.0

@defer.inlineCallbacks
def auth_and_sub(c, account_id, token, is_live):
    req = ProtoOAApplicationAuthReq()
    req.clientId = os.getenv('CTRADER_CLIENT_ID')
    req.clientSecret = os.getenv('CTRADER_CLIENT_SECRET')
    yield c.send(req)

    req_acc = ProtoOAAccountAuthReq()
    req_acc.ctidTraderAccountId = account_id
    req_acc.accessToken = token
    yield c.send(req_acc)

    req_sym = ProtoOASymbolsListReq()
    req_sym.ctidTraderAccountId = account_id
    resp_sym = yield c.send(req_sym)
    msg_sym = Protobuf.extract(resp_sym)

    sid = None
    for sym in msg_sym.symbol:
        if sym.symbolName == 'XAUUSD':
            sid = sym.symbolId
            break
            
    print(f"[{'LIVE' if is_live else 'DEMO'}] XAUUSD ID: {sid}")
    
    req_sub = ProtoOASubscribeSpotsReq()
    req_sub.ctidTraderAccountId = account_id
    req_sub.symbolId.append(sid)
    yield c.send(req_sub)
    print(f"[{'LIVE' if is_live else 'DEMO'}] Subscribed to XAUUSD Ticks")

@defer.inlineCallbacks
def start(c):
    try:
        print("Authenticating Live...")
        yield auth_and_sub(client_live, int(os.getenv('CTRADER_LIVE_ACCOUNT_ID')), os.getenv('CTRADER_LIVE_ACCESS_TOKEN'), True)
        
        print("Authenticating Demo...")
        yield auth_and_sub(client_demo, int(os.getenv('CTRADER_DEMO_ACCOUNT_ID')), os.getenv('CTRADER_DEMO_ACCESS_TOKEN'), False)
        
        print("\n--- Listening for XAUUSD Price Updates for 20 seconds ---\n")
        reactor.callLater(20, stop_all)
    except Exception as e:
        print(f"Error: {e}")
        stop_all()

def stop_all():
    print("\n--- Done ---")
    if reactor.running:
        reactor.stop()

def print_comparison():
    diff_bid = round(abs(live_bid - demo_bid), 3) if live_bid and demo_bid else 0
    diff_ask = round(abs(live_ask - demo_ask), 3) if live_ask and demo_ask else 0
    
    print(f"LIVE | Bid: {live_bid:,.3f}  Ask: {live_ask:,.3f}")
    print(f"DEMO | Bid: {demo_bid:,.3f}  Ask: {demo_ask:,.3f}")
    print(f"DIFF | Bid Δ: {diff_bid:.3f}   Ask Δ: {diff_ask:.3f}")
    print("-" * 40)

def on_msg_live(c, msg):
    global live_bid, live_ask
    m = Protobuf.extract(msg)
    if type(m).__name__ == "ProtoOASpotEvent":
        updated = False
        if hasattr(m, "bid") and m.bid:
            live_bid = m.bid / 100000.0
            updated = True
        if hasattr(m, "ask") and m.ask:
            live_ask = m.ask / 100000.0
            updated = True
        if updated:
            print_comparison()

def on_msg_demo(c, msg):
    global demo_bid, demo_ask
    m = Protobuf.extract(msg)
    if type(m).__name__ == "ProtoOASpotEvent":
        updated = False
        if hasattr(m, "bid") and m.bid:
            demo_bid = m.bid / 100000.0
            updated = True
        if hasattr(m, "ask") and m.ask:
            demo_ask = m.ask / 100000.0
            updated = True
        if updated:
            print_comparison()

client_live.setConnectedCallback(start)
client_live.setMessageReceivedCallback(on_msg_live)

# client_demo will connect immediately, but we wait for Live's callback to start auth.
client_demo.setConnectedCallback(lambda c: None)
client_demo.setMessageReceivedCallback(on_msg_demo)

client_live.startService()
client_demo.startService()

reactor.run()
