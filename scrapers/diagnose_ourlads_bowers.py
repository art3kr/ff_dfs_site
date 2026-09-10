"""
scrapers/diagnose_ourlads_bowers.py
--------------------------------------------------
Targeted check: Brock Bowers (TE, Las Vegas Raiders) is confirmed
currently injured — searching his specific row directly, rather than
guessing at what color/class Ourlads uses for injury status from an
arbitrary team's page that might not have any currently-injured
starters on it (the BUF diagnostic run found color classes lc_gold/
lc_purple that turned out to mean something else entirely — a trade
indicator and a rookie draft year, not injury status).

Tries a few team-code variations since Ourlads' code for a relocated
franchise (Oakland -> Las Vegas) might not match the modern "LV"
abbreviation used elsewhere in this project.

Usage:
    python scrapers/diagnose_ourlads_bowers.py
"""

import requests
from bs4 import BeautifulSoup

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}

TEAM_CODE_CANDIDATES = ["LV", "LVR", "OAK"]


def main():
    for code in TEAM_CODE_CANDIDATES:
        url = f"https://www.ourlads.com/nfldepthcharts/depthchart/{code}"
        print(f"Trying {code}: {url}")
        r = requests.get(url, headers=HEADERS, timeout=15)
        print(f"  Status: {r.status_code}, length: {len(r.text)}")

        if r.status_code == 200 and 'Bowers' in r.text:
            print(f"  Found 'Bowers' on this page — using {code}\n")
            soup = BeautifulSoup(r.content, 'html.parser', from_encoding='utf-8')

            # Find the specific <a> tag or row containing his name
            bowers_link = soup.find('a', string=lambda s: s and 'Bowers' in s)
            if bowers_link:
                print(f"{'='*70}")
                print("Bowers' own <a> tag:")
                print(f"{'='*70}")
                print(str(bowers_link))

                print(f"\n{'='*70}")
                print("Full <tr> row containing his link:")
                print(f"{'='*70}")
                row = bowers_link.find_parent('tr')
                if row:
                    print(str(row))
            else:
                # Fallback: dump raw text context around wherever
                # "Bowers" appears, in case it's not inside a plain <a>
                idx = r.text.find('Bowers')
                print(f"{'='*70}")
                print("Raw HTML context around 'Bowers' (name not in a plain <a> tag):")
                print(f"{'='*70}")
                print(r.text[max(0, idx-500):idx+500])
            return

        print(f"  No 'Bowers' found on this page, trying next code...\n")

    print("Could not find Bowers under any tried team code — the actual "
          "code Ourlads uses for this team may be something else entirely.")


if __name__ == "__main__":
    main()
