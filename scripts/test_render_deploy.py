#!/usr/bin/env python3
import os
import sys
import time
import json
import urllib.request
import urllib.error

def make_request(url, headers, data=None, method="GET"):
    req = urllib.request.Request(url, headers=headers, method=method)
    if data is not None:
        req.data = json.dumps(data).encode("utf-8")
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_data = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_data = e.reason
        print(f"HTTP Error {e.code}: {err_data}")
        return e.code, err_data
    except Exception as e:
        print(f"Error: {e}")
        return None, str(e)

def main():
    api_key = os.getenv("RENDER_API_KEY")
    service_id = os.getenv("RENDER_SERVICE_ID")

    if not api_key or not service_id:
        print("Error: RENDER_API_KEY and RENDER_SERVICE_ID environment variables must be set.")
        print("Usage:")
        print("  export RENDER_API_KEY=\"your_api_key\"")
        print("  export RENDER_SERVICE_ID=\"srv-...\"")
        print("  python3 scripts/test_render_deploy.py")
        sys.exit(1)

    base_url = f"https://api.render.com/v1/services/{service_id}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    # 1. Trigger Deploy
    print(f"Triggering deploy for service {service_id}...")
    status, res = make_request(f"{base_url}/deploys", headers, data={}, method="POST")
    if status != 201:
        print("Failed to trigger deploy.")
        sys.exit(1)

    deploy_id = res.get("id")
    print(f"Deploy triggered successfully! Deploy ID: {deploy_id}")

    # 2. Poll Deploy Status
    print("Monitoring deploy status (polling every 10s)...")
    while True:
        status, res = make_request(f"{base_url}/deploys/{deploy_id}", headers)
        if status != 200:
            print("Failed to fetch deploy status.")
            time.sleep(10)
            continue

        deploy_status = res.get("status")
        print(f"Status: {deploy_status}")

        if deploy_status == "live":
            print("\n🎉 Deploy Successful! The app is live.")
            break
        elif deploy_status in ["build_failed", "update_failed", "canceled"]:
            print(f"\n❌ Deploy Failed with status: {deploy_status}")
            sys.exit(1)

        time.sleep(10)

if __name__ == "__main__":
    main()
