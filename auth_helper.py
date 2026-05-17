"""
auth_helper.py — OAuth2 helper for cTrader Open API.

Run this script to get your access token:
    python auth_helper.py

Steps:
1. Registers your app at https://openapi.ctrader.com
2. Opens a browser for you to authorize
3. Captures the token and prints it for your .env file
"""

import webbrowser
import http.server
import urllib.parse
import requests
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

# ─── Configuration ────────────────────────────────────────────
CLIENT_ID = os.getenv("CTRADER_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CTRADER_CLIENT_SECRET", "")
REDIRECT_URI = "http://localhost:5000/callback"
AUTH_URL = "https://openapi.ctrader.com/apps/auth"
TOKEN_URL = "https://openapi.ctrader.com/apps/token"

authorization_code = None


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler to capture the OAuth callback."""

    def do_GET(self):
        global authorization_code

        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            authorization_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
                <html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#1a1a2e;color:#fff">
                <h1>&#10004; Authorization Successful!</h1>
                <p>You can close this window and return to the terminal.</p>
                </body></html>
            """)
        else:
            error = params.get("error", ["Unknown error"])[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<h1>Error: {error}</h1>".encode())

    def log_message(self, format, *args):
        pass  # Suppress HTTP server logs


def main():
    print("=" * 55)
    print("🔑 cTrader OAuth2 Authorization Helper")
    print("=" * 55)

    if not CLIENT_ID or "your_" in CLIENT_ID:
        print("\n❌ CTRADER_CLIENT_ID not set in .env!")
        print("\nSteps to get your Client ID:")
        print("1. Go to https://openapi.ctrader.com")
        print("2. Sign in with your cTrader ID")
        print("3. Create a new application")
        print(f"4. Set Redirect URI to: {REDIRECT_URI}")
        print("5. Copy Client ID and Client Secret to .env")
        return

    if not CLIENT_SECRET or "your_" in CLIENT_SECRET:
        print("\n❌ CTRADER_CLIENT_SECRET not set in .env!")
        return

    # Step 1: Open authorization URL
    auth_params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "trading",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

    print(f"\n📡 Opening authorization URL in your browser...")
    print(f"   If it doesn't open, visit:\n   {auth_url}\n")
    webbrowser.open(auth_url)

    # Step 2: Start local server to capture callback
    print("⏳ Waiting for authorization callback on localhost:5000...")
    server = http.server.HTTPServer(("localhost", 5000), CallbackHandler)
    server.handle_request()  # Handle one request then stop

    if not authorization_code:
        print("\n❌ No authorization code received!")
        return

    print(f"\n✅ Authorization code received!")

    # Step 3: Exchange code for tokens
    print("📡 Exchanging code for access token...")

    token_params = {
        "grant_type": "authorization_code",
        "code": authorization_code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
    }

    try:
        resp = requests.get(TOKEN_URL, params=token_params)
        data = resp.json()

        if "accessToken" in data:
            access_token = data["accessToken"]
            refresh_token = data.get("refreshToken", "")

            print(f"\n{'=' * 55}")
            print("🎉 SUCCESS! Add these to your .env file:")
            print(f"{'=' * 55}")
            print(f"\nCTRADER_ACCESS_TOKEN={access_token}")
            if refresh_token:
                print(f"CTRADER_REFRESH_TOKEN={refresh_token}")
            print(f"\n{'=' * 55}")

        else:
            print(f"\n❌ Token exchange failed: {data}")

    except Exception as e:
        print(f"\n❌ Error: {e}")


if __name__ == "__main__":
    main()
