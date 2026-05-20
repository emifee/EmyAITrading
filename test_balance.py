import sys, os
from dotenv import load_dotenv
load_dotenv()
from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from twisted.internet import reactor, defer

CLIENT_ID = os.getenv("CTRADER_CLIENT_ID")
CLIENT_SECRET = os.getenv("CTRADER_CLIENT_SECRET")
ACCESS_TOKEN = os.getenv("CTRADER_ACCESS_TOKEN")
TARGET_ACCOUNT = 47359026

client = Client(EndPoints.PROTOBUF_LIVE_HOST, EndPoints.PROTOBUF_PORT, TcpProtocol)

@defer.inlineCallbacks
def on_connected(c):
    try:
        req = ProtoOAApplicationAuthReq()
        req.clientId = CLIENT_ID
        req.clientSecret = CLIENT_SECRET
        yield client.send(req)
        
        req2 = ProtoOAAccountAuthReq()
        req2.ctidTraderAccountId = TARGET_ACCOUNT
        req2.accessToken = ACCESS_TOKEN
        yield client.send(req2)
        
        req3 = ProtoOATraderReq()
        req3.ctidTraderAccountId = TARGET_ACCOUNT
        resp3 = yield client.send(req3)
        msg3 = Protobuf.extract(resp3)
        
        if hasattr(msg3, "trader"):
            print(f"==================================================")
            print(f"Account CTID: {TARGET_ACCOUNT}")
            print(f"Broker Name: {msg3.trader.brokerName}")
            print(f"Balance: ${(msg3.trader.balance / 100.0):,.2f}")
            print(f"==================================================")
        else:
            print("Error retrieving trader info.")
            
    except Exception as e:
        print(f"Exception: {e}")
    reactor.stop()

client.setConnectedCallback(on_connected)
client.startService()
reactor.run()
