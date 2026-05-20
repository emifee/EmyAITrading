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

client = Client(EndPoints.PROTOBUF_LIVE_HOST, EndPoints.PROTOBUF_PORT, TcpProtocol)

@defer.inlineCallbacks
def on_connected(c):
    try:
        req = ProtoOAApplicationAuthReq()
        req.clientId = CLIENT_ID
        req.clientSecret = CLIENT_SECRET
        yield client.send(req)
        
        req_accts = ProtoOAGetAccountListByAccessTokenReq()
        req_accts.accessToken = ACCESS_TOKEN
        resp = yield client.send(req_accts)
        msg = Protobuf.extract(resp)
        print("="*50)
        if hasattr(msg, "ctidTraderAccount"):
            for a in msg.ctidTraderAccount:
                print(f"Account: {a.ctidTraderAccountId} | Live: {a.isLive}")
        elif hasattr(msg, "errorCode"):
            print(f"Error: {msg.errorCode} - {getattr(msg, 'description', '')}")
        print("="*50)
    except Exception as e:
        print(f"Exception: {e}")
    reactor.stop()

client.setConnectedCallback(on_connected)
client.startService()
reactor.run()
