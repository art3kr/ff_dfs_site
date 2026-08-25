"""
scrapers/debug_rotowire.py
--------------------------
Diagnose what RotoWire returns under different request strategies.

Usage:
    python scrapers/debug_rotowire.py --slate-id 4170
"""

import os, sys, json, argparse, time
import requests, urllib3
urllib3.disable_warnings()
from dotenv import load_dotenv
load_dotenv()

RW_PHPSESSID = os.environ.get("RW_PHPSESSID", "")
RW_TSD       = os.environ.get("RW_TSD", "")

PAGE_URL = "https://www.rotowire.com/daily/nfl/value-report.php?site=DraftKings&slateID={slate_id}"
DATA_URL = ("https://www.rotowire.com/daily/tables/value-report-nfl.php"
            "?siteID=2&slateID={slate_id}&projSource=RotoWire&oshipSource=RotoWire")

def cookie_str():
    parts = []
    if RW_PHPSESSID:
        parts.append(f"PHPSESSID={RW_PHPSESSID}")
    if RW_TSD:
        parts.append(f"rw_tsd={RW_TSD}")
    return "; ".join(parts)

def test_strategy(slate_id: int, strategy: str):
    print(f"\n{'='*60}")
    print(f"Strategy: {strategy}")
    print(f"{'='*60}")

    session = requests.Session()

    if strategy == "direct":
        # What we do now — hit the JSON endpoint directly
        headers = {
            'User-Agent':       'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
            'Accept':           'application/json, text/javascript, */*; q=0.01',
            'Accept-Language':  'en-US,en;q=0.9',
            'Referer':          'https://www.rotowire.com/daily/nfl/value-report.php',
            'X-Requested-With': 'XMLHttpRequest',
            'Cookie':           cookie_str(),
        }
        r = session.get(DATA_URL.format(slate_id=slate_id),
                        headers=headers, verify=False, timeout=15)

    elif strategy == "page_first":
        # Visit the HTML page first (like a real browser), then hit the API
        page_headers = {
            'User-Agent':                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
            'Accept':                    'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language':           'en-US,en;q=0.9',
            'Accept-Encoding':           'gzip, deflate, br',
            'Connection':                'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest':            'document',
            'Sec-Fetch-Mode':            'navigate',
            'Sec-Fetch-Site':            'none',
            'Sec-Fetch-User':            '?1',
            'Cache-Control':             'max-age=0',
            'Cookie':                    cookie_str(),
        }
        print("  Step 1: Visiting HTML page...")
        page_r = session.get(PAGE_URL.format(slate_id=slate_id),
                             headers=page_headers, verify=False, timeout=20)
        print(f"  Page status: {page_r.status_code}, length: {len(page_r.text)}")

        # Collect any new cookies the page set
        new_cookies = "; ".join(f"{c.name}={c.value}" for c in session.cookies)
        all_cookies = cookie_str()
        if new_cookies:
            all_cookies = all_cookies + "; " + new_cookies if all_cookies else new_cookies
            print(f"  New cookies from page: {new_cookies[:100]}")

        time.sleep(2)  # pause like a real browser would

        print("  Step 2: Hitting JSON API...")
        api_headers = {
            'User-Agent':       'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
            'Accept':           'application/json, text/javascript, */*; q=0.01',
            'Accept-Language':  'en-US,en;q=0.9',
            'Accept-Encoding':  'gzip, deflate, br',
            'Referer':          PAGE_URL.format(slate_id=slate_id),
            'X-Requested-With': 'XMLHttpRequest',
            'Sec-Fetch-Dest':   'empty',
            'Sec-Fetch-Mode':   'cors',
            'Sec-Fetch-Site':   'same-origin',
            'Cookie':           all_cookies,
        }
        r = session.get(DATA_URL.format(slate_id=slate_id),
                        headers=api_headers, verify=False, timeout=15)

    # Print results
    print(f"\n  API Status:       {r.status_code}")
    print(f"  Content-Type:     {r.headers.get('content-type','?')}")
    print(f"  Raw length:       {len(r.text)} chars")
    try:
        data = r.json()
        if isinstance(data, list):
            print(f"  Row count:        {len(data)}  ← this is the key number")
            if data:
                print(f"  First player:     {data[0].get('player','?')} | "
                      f"salary={data[0].get('salary','?')} | "
                      f"team={data[0].get('team','?')}")
        else:
            print(f"  Response:         {str(data)[:200]}")
    except:
        print(f"  Not JSON. Raw:    {r.text[:300]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--slate-id", type=int, required=True)
    args = parser.parse_args()

    print(f"Testing slate ID: {args.slate_id}")
    print(f"Cookies loaded: PHPSESSID={'yes' if RW_PHPSESSID else 'NO'}, "
          f"rw_tsd={'yes' if RW_TSD else 'NO'}")

    test_strategy(args.slate_id, "direct")
    test_strategy(args.slate_id, "page_first")
