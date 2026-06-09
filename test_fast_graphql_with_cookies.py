import sys
import os
import asyncio
import json
import re
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path("/home/shabana/Public/m/anti-detection FB automation")
sys.path.insert(0, str(PROJECT_ROOT))

# Setup environment variables
os.environ['FB_AUTOMATION_ENABLE_PRIVATE_HTTP'] = 'true'
os.environ['REDIS_URL'] = 'redis://mock_local_redis'

from fb_automation.models import IdentityContext
from fb_automation.tokens import TokenVault
from fb_automation.identity import IdentityRegistry
from fb_automation.fast_graphql_poster import create_facebook_posts_fast
from fb_automation.stealth_connector import get_stealth_connector
from tests.fakes import FakeRedis

# Mock the redis client in fast_graphql_poster
import fb_automation.fast_graphql_poster as fgp
fake_redis = FakeRedis()
async def mock_get_redis():
    return fake_redis
fgp._get_redis_client = mock_get_redis

# Mock the redis client in other files if needed
import fb_automation.browser_fallback as bf
bf._get_redis_client = mock_get_redis

# Fetch configurations from environment or use default
COOKIE_STRING = os.environ.get("FB_RAW_COOKIE_STRING", "").strip()
TARGET_PAGE_ID = os.environ.get("TARGET_PAGE_ID", "").strip()
TEST_CAPTION = os.environ.get("TEST_CAPTION", "").strip()

def parse_cookies(cookie_string: str):
    cookies = []
    for pair in cookie_string.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": ".facebook.com",
            "path": "/",
        })
    return cookies

async def extract_tokens_via_http(cookie_string: str) -> dict:
    session = get_stealth_connector().get_http_session(proxy_url=None)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie_string,
        "Upgrade-Insecure-Requests": "1"
    }
    
    print("Sending request to Facebook home page...")
    loop = asyncio.get_running_loop()
    def _fetch():
        return session.get("https://www.facebook.com/", headers=headers, timeout=15)
        
    resp = await loop.run_in_executor(None, _fetch)
    print(f"Response status: {resp.status_code}")
    
    html = resp.text
    
    # Extract fb_dtsg
    fb_dtsg = ""
    dtsg_match = re.search(r'["\']token["\']\s*:\s*["\'](NA[a-zA-Z0-9_\-]+)["\']', html)
    if dtsg_match:
        fb_dtsg = dtsg_match.group(1)
    else:
        dtsg_match2 = re.search(r'name="fb_dtsg" value="([^"]+)"', html)
        if dtsg_match2:
            fb_dtsg = dtsg_match2.group(1)
            
    # Extract lsd
    lsd = ""
    lsd_match = re.search(r'["\']LSD["\']\s*,\s*\[\s*\]\s*,\s*\{\s*["\']token["\']\s*:\s*["\']([^"\']+)["\']', html)
    if lsd_match:
        lsd = lsd_match.group(1)
        
    # Extract revision
    revision = ""
    rev_match = re.search(r'["\']client_revision["\']\s*:\s*(\d+)', html)
    if rev_match:
        revision = rev_match.group(1)
    else:
        rev_match2 = re.search(r'["\']server_revision["\']\s*:\s*(\d+)', html)
        if rev_match2:
            revision = rev_match2.group(1)
            
    # Extract c_user
    user_id = next((c["value"] for c in parse_cookies(cookie_string) if c["name"] == "c_user"), "")
        
    # Extract doc_id using the exact page patterns
    doc_id = ""
    patterns = (
        r'ComposerStoryCreateMutation(?:\.graphql)?[\s\S]{0,3000}?"doc_id"\s*:\s*"?(\d{8,})"?',
        r'"doc_id"\s*:\s*"?(\d{8,})"?[\s\S]{0,3000}?ComposerStoryCreateMutation',
        r'ComposerStoryCreateMutation(?:\.graphql)?[\s\S]{0,3000}?__dr"\s*:\s*"(\d{8,})"',
        r'ComposerStoryCreateMutation\.graphql[\s\S]{0,1200}?(?:e\.exports=|module\.exports=)?"?(\d{8,})"?',
        r'CometComposerStoryCreateMutation(?:\.graphql)?[\s\S]{0,3000}?"?(\d{8,})"?',
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            doc_id = match.group(1)
            break
            
    return {
        "fb_dtsg": fb_dtsg,
        "lsd": lsd,
        "user_id": user_id,
        "revision": revision,
        "doc_id": doc_id,
        "cookie_header": cookie_string
    }

async def discover_pages_via_http(cookie_string: str) -> list:
    session = get_stealth_connector().get_http_session(proxy_url=None)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie_string,
        "Upgrade-Insecure-Requests": "1"
    }
    
    print("Fetching managed pages list...")
    loop = asyncio.get_running_loop()
    def _fetch():
        return session.get("https://www.facebook.com/pages/?category=your_pages", headers=headers, timeout=15)
        
    resp = await loop.run_in_executor(None, _fetch)
    html = resp.text
    
    # Extract page IDs using regexes
    page_ids = list(set(
        re.findall(r'"pageID"\s*:\s*"(\d+)"', html) +
        re.findall(r'"page_id"\s*:\s*"(\d+)"', html) +
        re.findall(r'"delegate_page_id"\s*:\s*"(\d+)"', html)
    ))
    
    # Let's extract page names as well if possible
    pages = []
    for pid in page_ids:
        # Try to find name in json payload
        name_match = re.search(rf'"pageID"\s*:\s*"{pid}"\s*,\s*"name"\s*:\s*"([^"]+)"', html)
        name = name_match.group(1) if name_match else f"Page {pid}"
        pages.append({"id": pid, "name": name})
        
    return pages

async def main():
    if not COOKIE_STRING:
        print("Set FB_RAW_COOKIE_STRING with a valid Facebook cookie string before running this live helper.")
        return

    cookies_list = parse_cookies(COOKIE_STRING)
    cookies_json = json.dumps(cookies_list)
    # Extract c_user from the active cookies
    account_id = next((c["value"] for c in cookies_list if c["name"] == "c_user"), "unknown")
    
    if account_id == "unknown":
        print("Error: Could not find c_user inside the provided cookie string!")
        return

    # 1. Initialize identity
    identity = IdentityContext(
        account_id=account_id,
        proxy_url="", # direct
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        chrome_version="126.0.0.0",
        platform="Windows",
        locale="en-US"
    )
    
    registry = IdentityRegistry(fake_redis)
    await registry.register(identity)
    print(f"Registered identity for account {account_id} in Mock Redis.")

    # 2. Extract tokens and doc_id using HTTP
    print("Extracting Facebook tokens and doc_id via pure HTTP GET...")
    tokens = await extract_tokens_via_http(COOKIE_STRING)
    
    if not tokens.get("fb_dtsg") or not tokens.get("lsd"):
        print("Failed to extract tokens via HTTP. Page returned no credentials. Cookie string might be invalid or expired.")
        return
        
    print(f"Token extraction successful! User ID: {tokens.get('user_id')}")
    print(f"Revision: {tokens.get('revision')}")
    print(f"fb_dtsg (prefix): {tokens.get('fb_dtsg')[:15]}...")
    
    # Store tokens in fake token vault
    vault = TokenVault(fake_redis)
    await vault.set(account_id, tokens)
    
    # Cache doc_id in Redis
    doc_id = tokens.get("doc_id") or "7711610262198779"
    await fake_redis.set("fb_graphql_doc_id", doc_id.encode('utf-8'))
    print(f"Cached GraphQL doc_id: {doc_id}")

    # 3. Target Page setup
    if TARGET_PAGE_ID:
        page_id = TARGET_PAGE_ID
        page_name = f"Custom Page {page_id}"
        print(f"Using manual target page ID: {page_id}")
    else:
        print("Discovering managed pages via HTTP...")
        pages = await discover_pages_via_http(COOKIE_STRING)
            
        if not pages:
            print("No managed pages discovered via HTTP. Using profile user_id as target.")
            pages = [{"id": account_id, "name": f"Profile Direct {account_id}"}]
        else:
            print(f"Discovered {len(pages)} pages:")
            for page in pages:
                print(f"  • ID: {page['id']} | Name: {page['name']}")

        # Let's target the first page found
        target_page = pages[0]
        page_id = target_page["id"]
        page_name = target_page["name"]
        print(f"Targeting page: {page_name} (ID: {page_id})")

    # 4. Perform fast GraphQL post
    caption = TEST_CAPTION or f"Fast GraphQL posting tier test! Cased Chrome headers & TLS fingerprinting check. Post time: {asyncio.get_running_loop().time()}"
    posts = [{
        "page_id_or_url": page_id,
        "page_name": page_name,
        "post_type": "text",
        "caption": caption
    }]
    
    async def mock_fallback(fallback_posts):
        print("Fallback to browser invoked! GraphQL post failed.")
        return [{"page": page_name, "success": False, "result": "browser fallback executed"}]
        
    print("Executing post via the fast GraphQL posting tier...")
    results = await create_facebook_posts_fast(
        cookies_json=cookies_json,
        posts=posts,
        browser_fallback=mock_fallback
    )
    
    print("=== POSTING RESULTS ===")
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
