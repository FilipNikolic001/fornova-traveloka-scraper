"""
Traveloka Hotel Room Rates Scraper
=================================

A resilient, request-first Python scraper designed to extract room rates, taxes, and occupancy 
details from Traveloka's internal JSON API. It uses TLS/HTTP2 session impersonation (via curl_cffi) 
for the primary high-performance request flow, and integrates a browser-assisted network 
interception fallback to recover gracefully from high-threshold bot challenges.

Author: Filip
"""

import os
import sys
import json
import time
import re
import uuid
import argparse
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote, quote
from typing import Union, List, Dict, Any

# Drop-in WAF-bypass library (curl-impersonate JA3/JA4 fingerprinting)
try:
    from curl_cffi import requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests
    HAS_CURL_CFFI = False


# ---------------------------------------------------------------------------
# Constants & Configuration
# ---------------------------------------------------------------------------

TARGET_URL = (
    "https://www.traveloka.com/en-th/hotel/detail?"
    "spec=01-06-2026.02-06-2026.1.1.HOTEL.9000001153383."
    "novotel%20hua%20hin%20cha-am%20beach%20resort%20&%20spa.2"
)

ROOMS_API_URL = "https://www.traveloka.com/api/v2/hotel/search/rooms"
SESSION_FILE = "session_cache.json"


# ---------------------------------------------------------------------------
# WAF Exception
# ---------------------------------------------------------------------------

class WAFBlockException(Exception):
    """Raised when requests are blocked by WAF."""
    pass


# ---------------------------------------------------------------------------
# URL Spec Parsing Logic
# ---------------------------------------------------------------------------

def parse_spec_from_url(url: str) -> Dict[str, Any]:
    """
    Parse the Traveloka 'spec' query parameter defensively, or extract from SEO path.
    """
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        spec_raw = query.get("spec", [""])
        
        if not spec_raw or not spec_raw[0]:
            # Fallback: Try to extract hotel ID from the end of an SEO URL
            # e.g. https://www.traveloka.com/en-en/hotel/indonesia/the-apurva-kempinski-bali-3000020019601
            path_parts = parsed.path.rstrip("/").split("/")
            last_part = path_parts[-1] if path_parts else ""
            
            # Usually the hotel ID is the last numeric sequence after a dash
            match = re.search(r'-(\d+)$', last_part)
            if not match and last_part.isdigit():
                match = re.search(r'^(\d+)$', last_part)
                
            if match:
                hotel_id = match.group(1)
                slug = last_part[:match.start()] if last_part[:match.start()] else "hotel"
                
                # Default to tomorrow for check-in to ensure valid dates
                from datetime import timedelta
                now = datetime.now()
                ci = now + timedelta(days=1)
                co = ci + timedelta(days=1)
                
                print(f"[*] 'spec' not found. Using hotel ID {hotel_id} with default dates ({ci.strftime('%d-%m-%Y')}).")
                
                return {
                    "checkin": ci.strftime("%d-%m-%Y"),
                    "checkout": co.strftime("%d-%m-%Y"),
                    "rooms": 1,
                    "adults": 1,
                    "type": "HOTEL",
                    "hotel_id": hotel_id,
                    "slug": slug,
                    "slug_suffix": "",
                }
            raise ValueError("The 'spec' query parameter is missing and the URL path does not contain a valid hotel ID.")
            
        spec_decoded = unquote(spec_raw[0])
        parts = spec_decoded.split(".")
        
        if len(parts) < 6:
            raise ValueError(
                f"Malformed spec parameter. Expected at least 6 dot-separated fields, got {len(parts)}: '{spec_decoded}'"
            )
            
        # Extract slug and slug suffix if present (e.g. parts[6] represents slug)
        slug = parts[6] if len(parts) > 6 else ""
        slug_suffix = parts[7] if len(parts) > 7 else ""
            
        return {
            "checkin": parts[0],
            "checkout": parts[1],
            "rooms": int(parts[2]) if parts[2].isdigit() else 1,
            "adults": int(parts[3]) if parts[3].isdigit() else 1,
            "type": parts[4],
            "hotel_id": parts[5],
            "slug": slug,
            "slug_suffix": slug_suffix,
        }
    except Exception as e:
        raise ValueError(f"Failed to parse spec from URL: {e}")


def build_deep_link(hotel_id: str, checkin: str, checkout: str, slug: str = "", slug_suffix: str = "", room_id: str = "") -> str:
    """Generate the specific rooms/rates deep link URL programmatically preserving canonical layout."""
    base = "https://www.traveloka.com/en-th/hotel/detail"
    spec_str = f"{checkin}.{checkout}.1.1.HOTEL.{hotel_id}"
    if slug:
        spec_str += f".{quote(slug)}"
    if slug_suffix:
        spec_str += f".{quote(slug_suffix)}"
        
    link = f"{base}?spec={spec_str}"
    if room_id:
        link += f"&roomId={room_id}"
    return link


# ---------------------------------------------------------------------------
# Browser-Assisted Recovery Interceptor (Fallback Mechanism)
# ---------------------------------------------------------------------------

def harvest_data_via_browser(url: str) -> Dict[str, Any]:
    """
    Optional Recovery Fallback: Launches a headful Chromium browser using Playwright 
    to navigate to the target page, allowing dynamic browser-network interception of 
    the authentic 'api/v2/hotel/search/rooms' JSON API response directly from the 
    underlying socket.
    
    This acts as a resilient recovery mechanism when direct API requests are blocked
    by strict bot-detection rate limits or security cookies.
    """
    print("[*] Launching browser-assisted recovery session to intercept API...")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "Playwright is not installed. Automated browser bypass is unavailable.\n"
            "Please run: pip install playwright && playwright install"
        )

    captured = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context_options = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "viewport": {"width": 1280, "height": 720}
        }
        if os.path.exists(SESSION_FILE):
            context_options["storage_state"] = SESSION_FILE

        context = browser.new_context(**context_options)
        
        # Hide automation signature
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        page = context.new_page()
        
        # Define response interception
        def on_response(response):
            if "api/v2/hotel/search/rooms" in response.url and response.request.method == "POST":
                if response.status == 200:
                    try:
                        captured["body"] = response.json()
                        print("[+] Successfully intercepted authentic JSON API response (Status 200 OK)!")
                        # Save backup debug file
                        with open("debug_response.json", "w", encoding="utf-8") as f_debug:
                            json.dump(captured["body"], f_debug, indent=2, ensure_ascii=False)
                    except Exception as e:
                        print(f"[!] Error parsing intercepted API JSON: {e}")
                else:
                    print(f"[-] Intercepted API request but status was: {response.status} (likely WAF Challenge)")

        page.on("response", on_response)
        
        print(f"\n[*] Navigating browser to target URL...")
        print("[!] Note: Please solve any on-screen verification checks if prompted.")
        
        page.goto(url, wait_until="load", timeout=60000)
        
        # Wait up to 60 seconds for rooms to render on screen (indicated by "Choose" buttons)
        print("[*] Waiting for content to render...")
        try:
            page.wait_for_selector('text="Choose"', timeout=60000)
            print("[+] Target UI elements detected.")
        except Exception:
            print("[!] Timeout waiting for target UI elements (rendering may be incomplete).")
        
        # Settle session
        time.sleep(3)
        print("[*] Saving browser session state for future fast-path runs...")
        context.storage_state(path=SESSION_FILE)
        browser.close()
 
    if "body" in captured:
        return captured["body"]
    else:
        raise WAFBlockException("Browser recovery timeout: Failed to intercept rooms API response.")


# ---------------------------------------------------------------------------
# Scraper Class
# ---------------------------------------------------------------------------

class TravelokaScraper:
    def __init__(self, url: str):
        self.url = url
        self.spec = parse_spec_from_url(url)
        
        # Spoof Chrome TLS handshake fingerprint using curl_cffi if available
        if HAS_CURL_CFFI:
            self.session = requests.Session(impersonate="chrome120")
        else:
            self.session = requests.Session()
            
        if os.path.exists(SESSION_FILE):
            try:
                with open(SESSION_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                    for cookie in state.get("cookies", []):
                        domain = cookie.get("domain", "").lstrip(".")
                        self.session.cookies.set(cookie["name"], cookie["value"], domain=domain, path=cookie.get("path", "/"))
                print("[*] Loaded cached session cookies successfully.")
            except Exception as e:
                print(f"[-] Failed to load session cache: {e}")
            
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://www.traveloka.com",
            "Referer": self.url,
            "x-route-prefix": "en-th",
            "x-client-interface": "desktop",
            "x-domain": "accomRoom",
            "www-app-version": "",  # Populated dynamically via self-healing pre-flight GET
            "tv-language": "en_TH",
            "tv-country": "TH",
            "tv-currency": "THB",
        }

    def scrape_live_rates(self) -> Dict[str, Any]:
        """
        Main requests-based scraper endpoint.
        Uses standard requests (or curl_cffi) to perform the live API fetch.
        """
        ci_dt = datetime.strptime(self.spec["checkin"], "%d-%m-%Y")
        co_dt = datetime.strptime(self.spec["checkout"], "%d-%m-%Y")

        # Dynamic App Version Extraction: Refresh www-app-version header from landing page to prevent expiration
        try:
            print("[*] Retrieving latest frontend app-version from landing page...")
            get_resp = self.session.get(self.url, timeout=10)
            match = re.search(r'release_webacd_[a-zA-Z0-9_\-]+', get_resp.text)
            if match:
                extracted_ver = match.group(0)
                self.headers["www-app-version"] = extracted_ver
                print(f"[+] Dynamically updated www-app-version to: {extracted_ver}")
            else:
                print("[-] Could not parse app-version from landing page. Using fallback release header.")
        except Exception as ver_err:
            print(f"[-] Silent warning: Dynamic app-version retrieval failed ({ver_err}). Using fallback.")

        payload = {
            "fields": [],
            "data": {
                "contexts": {
                    "hotelDetailURL": self.url,
                    "bookingId": None,
                    "sourceIdentifier": "HOTEL_DETAIL",
                    "shouldDisplayAllRooms": False,
                    "marketingContextCapsule": {
                        "page_full_url": self.url,
                        "client_user_agent": self.headers["User-Agent"]
                    }
                },
                "prevSearchId": "undefined",
                "numInfants": 0,
                "ccGuaranteeOptions": {
                    "ccInfoPreferences": ["CC_TOKEN", "CC_FULL_INFO"],
                    "ccGuaranteeRequirementOptions": ["CC_GUARANTEE"]
                },
                "rateTypes": ["PAY_NOW", "PAY_AT_PROPERTY"],
                "isJustLogin": False,
                "isReschedule": False,
                "preview": False,
                "monitoringSpec": {
                    "referrer": "",
                    "lastKeyword": self.spec.get("slug", "").replace("%20", " ").replace("+", " ")
                },
                "hotelId": self.spec["hotel_id"],
                "currency": "THB",
                "labelContext": {},
                "isExtraBedIncluded": True,
                "hasPromoLabel": False,
                "supportedRoomHighlightTypes": ["ROOM"],
                "checkInDate": {
                    "day": str(ci_dt.day),
                    "month": str(ci_dt.month),
                    "year": str(ci_dt.year)
                },
                "checkOutDate": {
                    "day": str(co_dt.day),
                    "month": str(co_dt.month),
                    "year": str(co_dt.year)
                },
                "numOfNights": (co_dt - ci_dt).days,
                "numAdults": self.spec["adults"],
                "numRooms": self.spec["rooms"],
                "numChildren": 0,
                "childAges": [],
                # Dynamic request UUID generation to fulfill production standards
                "tid": str(uuid.uuid4())
            },
            "clientInterface": "desktop"
        }

        print(f"[*] Executing direct API POST request using requests...")
        response = self.session.post(
            ROOMS_API_URL, 
            headers=self.headers, 
            json=payload, 
            timeout=20
        )
            
        print(f"[*] Response Status Code: {response.status_code}")
        
        # Verify AWS WAF challenges
        if "x-amzn-waf-action" in response.headers:
            action = response.headers["x-amzn-waf-action"]
            if action == "captcha":
                raise WAFBlockException("Direct requests blocked by AWS WAF (Captcha Challenge).")

        if response.status_code == 200:
            return response.json()
        elif response.status_code in (202, 403, 405):
            raise WAFBlockException(f"Direct request blocked by WAF/CloudFront (HTTP {response.status_code}).")
        else:
            response.raise_for_status()
            return response.json()

    def parse_rates(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Defensively parses authentic Traveloka room search response JSON into the requested format.
        """
        rates = []
        if not isinstance(data, dict):
            return rates
            
        entries = data.get("data", {}).get("recommendedEntries", [])
        if not entries or not isinstance(entries, list):
            # Alternate schema: rooms nested under roomSearchResults
            rs = data.get("data", {}).get("roomSearchResults", {})
            entries = rs.get("recommendedEntries", rs.get("rooms", []))

        if not isinstance(entries, list):
            return rates

        # Compute dynamic stay length (number of nights) to correctly compute per-stay rates
        try:
            ci_dt = datetime.strptime(self.spec.get("checkin", ""), "%d-%m-%Y")
            co_dt = datetime.strptime(self.spec.get("checkout", ""), "%d-%m-%Y")
            num_nights = max((co_dt - ci_dt).days, 1)
        except Exception:
            num_nights = 1

        for entry in entries:
            if not isinstance(entry, dict):
                continue
                
            room_name = entry.get("name", entry.get("roomName", ""))
            room_id = entry.get("hotelRoomId", entry.get("roomId", ""))
            
            inventories = entry.get("hotelRoomInventoryList", entry.get("roomOptions", entry.get("rates", [])))
            if not isinstance(inventories, list):
                continue
                
            for inv in inventories:
                if not isinstance(inv, dict):
                    continue
                    
                is_refundable = bool(inv.get("isRefundable", False))
                is_breakfast = bool(inv.get("isBreakfastIncluded", False))
                breakfast_str = "With Breakfast" if is_breakfast else "Without Breakfast"
                
                # Defensive occupancy mapping: maxOccupancy acts as the authoritative boundary for the room rate capacity.
                # MinOccupancy is typically 1. Mapping maxOccupancy maps cleanly to "Number of Guests" for maximum guest capacity.
                try:
                    guests_raw = inv.get("maxOccupancy", 2)
                    guests = int(guests_raw) if guests_raw is not None else 2
                except (ValueError, TypeError):
                    guests = 2
                
                # Determine Rate Name
                rate_name = inv.get("inventoryName")
                if not rate_name:
                    refund_str = "Refundable" if is_refundable else "Non Refundable"
                    rate_name = f"{breakfast_str}, {refund_str}"
                    
                rate_display = inv.get("rateDisplay", {})
                if not isinstance(rate_display, dict):
                    rate_display = {}
                    
                try:
                    num_decimal = int(rate_display.get("numOfDecimalPoint", 2))
                except (ValueError, TypeError):
                    num_decimal = 2
                
                base_fare = rate_display.get("baseFare", {}) or {}
                taxes_fare = rate_display.get("taxes", {}) or {}
                total_fare = rate_display.get("totalFare", {}) or {}
                
                # Defensive pricing arithmetic (Price is always the active discounted rate if a promotion is active)
                try:
                    price_val = float(base_fare.get("amount", 0))
                    price = price_val / (10 ** num_decimal)
                except (ValueError, TypeError, ZeroDivisionError):
                    price = 0.0
                    
                currency = base_fare.get("currency", "THB") if isinstance(base_fare, dict) else "THB"
                
                try:
                    taxes_val = float(taxes_fare.get("amount", 0))
                    total_taxes = taxes_val / (10 ** num_decimal)
                except (ValueError, TypeError, ZeroDivisionError):
                    total_taxes = 0.0
                    
                try:
                    total_val = float(total_fare.get("amount", 0))
                    total_price = total_val / (10 ** num_decimal)
                except (ValueError, TypeError, ZeroDivisionError):
                    total_price = 0.0
                
                # Construct Rate Object
                rate_obj = {
                    "Room_name": room_name,
                    "Rate_name": rate_name,
                    "Number of Guests": guests,
                    "Cancellation Policy": "Refundable" if is_refundable else "Non Refundable",
                    "Breakfast": breakfast_str,
                    "Price": price,  # Acts as the active rate. Slower pre-sale base is stored inside original_price if active.
                    "Currency": currency,
                    "Total taxes": total_taxes,
                    "Total prices": total_price,
                }
                
                # Apply original price (discount) logic defensively
                orig_rate = inv.get("originalRateDisplay")
                if isinstance(orig_rate, dict):
                    orig_base = orig_rate.get("baseFare", {}) or {}
                    try:
                        orig_num_dec = int(orig_rate.get("numOfDecimalPoint", 2))
                        orig_price_val = float(orig_base.get("amount", 0))
                        orig_price = orig_price_val / (10 ** orig_num_dec)
                        if orig_price > price:
                            rate_obj["original_price"] = orig_price
                    except (ValueError, TypeError, ZeroDivisionError):
                        pass
                        
                # Correct per stay price calculations (scaled dynamically by stay duration)
                # Note: net_price_per_stay and shown_price_per_stay are identical in this Traveloka Desktop flow
                # because the desktop web UI displays pre-tax base rates as the primary "shown price" to users.
                # Taxes are calculated and added as a separate markup at the checkout stage.
                rate_obj["net_price_per_stay"] = round(price * num_nights, 2)
                # shown_price_per_stay equals net_price_per_stay on Traveloka Desktop:
                # the UI displays pre-tax base rates as the primary price; taxes are appended at checkout.
                rate_obj["shown_price_per_stay"] = round(price * num_nights, 2)
                rate_obj["total_price_per_stay"] = round(total_price * num_nights, 2)
                
                # Programmatically construct deep link safely, preserving dynamic slug
                hotel_id = self.spec.get("hotel_id", "")
                checkin = self.spec.get("checkin", "")
                checkout = self.spec.get("checkout", "")
                slug = self.spec.get("slug", "")
                slug_suffix = self.spec.get("slug_suffix", "")
                rate_obj["deep_link"] = build_deep_link(hotel_id, checkin, checkout, slug, slug_suffix, room_id)
                
                rates.append(rate_obj)
                
        return rates


# ---------------------------------------------------------------------------
# Main Runner
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Traveloka Resilient Hotel Room Rates Scraper")
    parser.add_argument(
        "url", 
        nargs="?", 
        default=TARGET_URL, 
        help="Optional target Traveloka URL. If omitted, uses the default built-in URL."
    )
    args = parser.parse_args()

    print("=" * 75)
    print(" Traveloka Resilient Hotel Room Rates Scraper (Request-First Hybrid Pipeline)")
    print("=" * 75)
    
    scraper = TravelokaScraper(args.url)
    
    try:
        # Step 1: Attempt direct API query via HTTP Request Session
        live_data = scraper.scrape_live_rates()
        print("[+] Direct HTTP requests session succeeded! Parsing JSON payload...")
        rates = scraper.parse_rates(live_data)
        
    except WAFBlockException as direct_err:
        # Step 2: Fallback to browser-network interception upon encounters with high-threshold bot challenges
        print(f"\n[!] Direct API request redirected or restricted: {direct_err}")
        print("[*] Initiating browser-assisted recovery fallback...")
        
        try:
            live_data = harvest_data_via_browser(args.url)
            print("[+] Browser interception recovered successfully! Parsing dynamic JSON payload...")
            rates = scraper.parse_rates(live_data)
            
        except WAFBlockException as bypass_err:
            print(f"\n[!] Recovery fallback failed (Interception Timeout): {bypass_err}")
            print("\n[-] Extraction Error: Browser failed to intercept rooms API within 60s timeout.")
            print("    Please check network connectivity or resolve CAPTCHAs manually.")
            sys.exit(1)
        except Exception as bypass_err:
            print(f"\n[!] Recovery fallback failed due to unexpected error: {bypass_err}")
            print("\n[-] Extraction Error: The target endpoint cannot be accessed directly or via browser fallback.")
            sys.exit(1)

    # 3. Output JSON Result
    output = {"rates": rates}
    output_filename = "rates_output.json"
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4, ensure_ascii=False)
        
    print("\n" + "=" * 75)
    print(f"[+] Successfully structured {len(rates)} live rates to: {output_filename}")
    print("=" * 75)
    # print(json.dumps(output, indent=4, ensure_ascii=False))


if __name__ == "__main__":
    main()
