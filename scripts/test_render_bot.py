#!/usr/bin/env python3
import os
import sys
import json
import urllib.request
import urllib.error

def fetch_json(url, data=None):
    req = urllib.request.Request(url)
    if data:
        req.data = json.dumps(data).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_data = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_data = e.reason
        return e.code, err_data
    except Exception as e:
        return None, str(e)

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    public_url = os.getenv("PUBLIC_BASE_URL")

    print("============================================================")
    print("🔬 RENDER HOSTING INTEGRATION & BOT DIAGNOSTIC TEST")
    print("============================================================\n")

    if not token:
        print("❌ Error: TELEGRAM_BOT_TOKEN is not set.")
        print("Please export your Telegram Bot Token first:")
        print("  export TELEGRAM_BOT_TOKEN=\"your_bot_token\"")
        sys.exit(1)

    # 1. Test Telegram Connection & Bot Info
    print("1. Fetching Bot details from Telegram...")
    status, bot_info = fetch_json(f"https://api.telegram.org/bot{token}/getMe")
    if status != 200 or not bot_info.get("ok"):
        print(f"❌ Failed to connect to Telegram API. Response: {bot_info}")
        sys.exit(1)
    
    result = bot_info["result"]
    print(f"✅ Connected to Telegram successfully!")
    print(f"   Bot Username: @{result.get('username')}")
    print(f"   Bot Name: {result.get('first_name')}\n")

    # 2. Check Webhook Info
    print("2. Fetching Webhook configuration from Telegram...")
    status, webhook_info = fetch_json(f"https://api.telegram.org/bot{token}/getWebhookInfo")
    if status != 200 or not webhook_info.get("ok"):
        print(f"❌ Failed to get webhook info. Response: {webhook_info}")
        sys.exit(1)

    wh = webhook_info["result"]
    configured_url = wh.get("url")
    print(f"✅ Webhook Status:")
    print(f"   Configured URL: {configured_url or 'None (Polling mode)'}")
    print(f"   Pending Updates Count: {wh.get('pending_update_count', 0)}")
    
    if wh.get("last_error_date"):
        import datetime
        err_time = datetime.datetime.fromtimestamp(wh["last_error_date"]).strftime('%Y-%m-%d %H:%M:%S')
        print(f"   ⚠️ Last Error Date: {err_time}")
        print(f"   ⚠️ Last Error Message: {wh.get('last_error_message')}")
    else:
        print(f"   ✅ No recent webhook errors reported by Telegram.")
    print()

    # 3. Test Render Public Base URL Health check
    if not public_url:
        if configured_url:
            # Try to derive public base url from webhook url
            public_url = configured_url.split("/telegram/webhook")[0]
            print(f"ℹ️ PUBLIC_BASE_URL environment variable was not set. Derived from Telegram webhook: {public_url}")
        else:
            print("❌ Warning: PUBLIC_BASE_URL environment variable not set, and no webhook configured on Telegram.")
            print("Cannot ping healthz endpoint. Export it with:")
            print("  export PUBLIC_BASE_URL=\"https://your-service.onrender.com\"")
            sys.exit(0)

    print(f"3. Pinging Render healthz endpoint: {public_url}/healthz")
    status, health_res = fetch_json(f"{public_url}/healthz")
    if status == 200:
        print(f"✅ Render Web Service is LIVE and healthy! Response: {health_res}")
    else:
        print(f"❌ Render Web Service health check failed.")
        print(f"   Status Code: {status}")
        print(f"   Response: {health_res}")
        print("   Tips:")
        print("   - Check Render dashboard deployment logs to see if Python started successfully.")
        print("   - Verify that your Render service is not spinning down/sleeping (Free tier spins down after inactivity).")
    
    print("\n============================================================")

if __name__ == "__main__":
    main()
