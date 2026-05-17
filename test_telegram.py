"""Quick test to verify Telegram bot connection."""
import sys
import os
import asyncio

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

import config

async def test_telegram():
    print("=" * 50)
    print("🤖 Telegram Bot Connection Test")
    print("=" * 50)

    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID

    if not token:
        print("❌ TELEGRAM_BOT_TOKEN is empty!")
        return False

    if not chat_id:
        print("❌ TELEGRAM_CHAT_ID is empty!")
        return False

    print(f"✅ Token found: {token[:10]}...{token[-5:]}")
    print(f"✅ Chat ID found: {chat_id}")

    # Test 1: Get bot info
    print("\n📡 Testing bot connection...")
    from telegram import Bot
    bot = Bot(token=token)

    try:
        me = await bot.get_me()
        print(f"✅ Bot connected!")
        print(f"   Name: {me.first_name}")
        print(f"   Username: @{me.username}")
        print(f"   ID: {me.id}")
    except Exception as e:
        print(f"❌ Bot connection failed: {e}")
        return False

    # Test 2: Send a test message
    print(f"\n📨 Sending test message to chat {chat_id}...")
    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=(
                "🤖 *Emy AI Trading Bot — Test Message*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ Connection successful!\n"
                "✅ Messages can be sent\n\n"
                "Your bot is ready. Use /start after launching `python main.py`"
            ),
            parse_mode="Markdown",
        )
        print(f"✅ Message sent! (message_id: {msg.message_id})")
        print("\n🎉 Check your Telegram — you should see the message!")
        return True

    except Exception as e:
        print(f"❌ Failed to send message: {e}")
        if "chat not found" in str(e).lower():
            print("\n💡 TIP: Make sure you've started a chat with the bot first!")
            print("   1. Open Telegram")
            print("   2. Search for your bot by username")
            print("   3. Click 'Start' or send any message")
            print("   4. Then re-run this test")
        return False


if __name__ == "__main__":
    result = asyncio.run(test_telegram())
    print("\n" + "=" * 50)
    print("RESULT:", "✅ ALL GOOD!" if result else "❌ FIX ISSUES ABOVE")
    print("=" * 50)
