# Traveloka Resilient Hotel Room Rates Scraper

**Fornova Junior Scraping Expert — Take-Home Assignment Submission**

A structured, resilient, and request-first Python scraper designed to dynamically extract room rates, taxes, cancellation policies, and occupancy configurations from Traveloka's hotel detail pages in real-time. 

Rather than relying on fragile HTML parsing, this solution is built on a reverse-engineered API extraction workflow that directly queries Traveloka's internal JSON endpoints.

---

## 📐 Design Decisions

During the architectural planning of this solution, several key engineering decisions were made to optimize performance, minimize overhead, and improve recovery rates against bot-detection systems:

1. **Structured API Extraction Over HTML Parsing:** 
   HTML parsing was intentionally avoided. Traveloka renders hotel room details dynamically on the client side. Scraping raw HTML would require maintaining heavy browser instances constantly or risking parser breakage upon minor UI updates. Targeting the underlying JSON API guarantees clean, high-fidelity structured data directly from the source.
   
2. **Request-First High Performance:** 
   The primary extraction engine is entirely lightweight, utilizing direct HTTP POST requests with a custom session. If `curl_cffi` is available in the environment, it automatically impersonates modern browser TLS handshakes (using identical Chrome JA3/JA4 fingerprints) and HTTP/2 profiles to drastically reduce bot-detection triggers with minimal CPU and memory overhead.

3. **Browser-Assisted Network Interception Fallback:** 
   Automated browsers are resource-intensive and slow. Therefore, a headful Chromium browser (via Playwright) is integrated strictly as a **secondary recovery fallback mechanism**. It is only invoked if the request-first tier encounters restrictive network-level barriers (e.g., HTTP 202/403/405 challenges). By loading the page in a real browser and hooking directly into the network sockets to capture the authentic `200 OK` API response, it recovers the data gracefully even under challenging network conditions.

4. **Schema-Aware Defensive Parsing:** 
   The parsing layer is built defensively to tolerate response schema variations. All numeric conversions, occupancy integers, and currency fields are guarded with default fallbacks and strict try-except blocks, ensuring the script processes the room data and outputs structured JSON without crashing if optional API keys are missing.

5. **Self-Healing Versioning Synchronization:**
   Internal backend gateways are protected by validating frontend client version headers (`www-app-version`). Hardcoded versions inevitably expire, causing requests to be rejected. The scraper includes a self-healing regex module that executes a high-speed pre-flight GET request to extract the currently active release token from the landing page's HTML structure, dynamically updating request sessions in real-time for production-grade longevity.

---

## 🔍 Network Flow & Reverse Engineering Analysis

To establish a highly resilient direct connection to the Traveloka data layer, the target page's network traffic was reverse-engineered using DevTools. Below is the detailed breakdown of the communication sequence:

### 1. The Bootstrap Phase (GET Request)
A standard browser begins by sending a `GET` request to the hotel details landing page:
```
GET https://www.traveloka.com/en-th/hotel/detail?spec=20-02-2025.21-02-2025.1.1.HOTEL.9000001153383.novotel%20hua%20hin%20cha-am%20beach%20resort%20&%20spa.2
```
During this phase, Traveloka's gateway sets vital initial tracking cookies (e.g., security parameters, language settings) and validates the client's connection.

### 2. Deconstruction of the `spec` Query Parameter
The URL relies on a dot-separated query parameter named `spec` that contains the complete contextual requirements for the search. The script parses this parameter dynamically to prevent hardcoding:

| Dot Index | Format | Example Value | Decoded Meaning |
| :--- | :--- | :--- | :--- |
| **parts[0]** | `DD-MM-YYYY` | `20-02-2025` | Check-in Date |
| **parts[1]** | `DD-MM-YYYY` | `21-02-2025` | Check-out Date |
| **parts[2]** | `Integer` | `1` | Number of Rooms Required |
| **parts[3]** | `Integer` | `1` | Number of Adult Guests |
| **parts[4]** | `String` | `HOTEL` | Accommodation Category |
| **parts[5]** | `String` | `9000001153383` | **Target Hotel ID** (Traveloka's Internal Entity Key) |
| **parts[6]** | `String` | `novotel%20hua%20hin...` | **Dynamic Hotel Slug / Name** (Parsed and preserved in dynamic deep links) |

### 3. Dynamic Rooms Target API (POST Request)
The browser subsequently fetches active room options by initiating a `POST` request to the internal rooms gateway:
* **Endpoint:** `https://www.traveloka.com/api/v2/hotel/search/rooms`
* **Method:** `POST`

#### Mandatory Gateway Headers
The Traveloka API gatekeeper performs strict signature and validation checks. If any of the following custom headers are missing or mismatched, the gateway drops the request with a `400 Bad Request` or `403 Forbidden` response:

* `x-route-prefix`: `en-th` (Informs the gateway of the localization context).
* `x-client-interface`: `desktop` (Matches the rendering layer layout).
* `x-domain`: `accomRoom` (Routes the query to the correct room inventory microservice).
* `www-app-version`: `release_webacd_...` (**Self-Healing Dynamic Header:** Automatically matched and synchronized via pre-flight regex scraping of the bootstrap landing HTML).
* `tv-language` / `tv-country` / `tv-currency`: `en_TH` / `TH` / `THB` (Explicitly binds the pricing response to local currencies and local tax codes).

#### Dynamic Request Payload
The POST request payload represents a highly structured configuration of the search context. Key dynamically mapped segments include:
* **checkInDate / checkOutDate:** Extracted from the `spec` param, split into individual integer-string mappings:
  ```json
  "checkInDate": { "day": "20", "month": "2", "year": "2025" }
  ```
* **numOfNights:** Computed dynamically via datetime subtraction between the checkout and checkin dates.
* **hotelId:** Sourced from `parts[5]` to query the target hotel.
* **tid:** A dynamic session transaction UUID (`uuid.uuid4()`) dynamically generated per query to satisfy production telemetry standards.

---

## 📊 Data Extraction & Response JSON Schema Mapping

Once a successful `200 OK` response is captured (either via direct HTTP session or browser-assisted interception), the dynamic parser processes the JSON structure defensively. Below is the mapping table showing exactly how Traveloka's API response structure maps to our final exported JSON schema:

| Target Schema Key | Target JSON Path in API Response | Rationale & Extraction Logic |
| :--- | :--- | :--- |
| **Room_name** | `entry.name` or `entry.roomName` | Sourced from the root room categories array under `recommendedEntries` or `roomSearchResults.rooms`. |
| **Rate_name** | `inv.inventoryName` | Sourced from the individual room rate configurations array (`hotelRoomInventoryList`). If absent, defaults to a descriptive string combining breakfast and refundability. |
| **Number of Guests** | `inv.maxOccupancy` | **Guest Capacity Mapping:** Sourced defensively via the room's maximum guest capacity limit (`maxOccupancy`), which represents the authoritative capacity allowed for the room rate (minOccupancy is typically 1). |
| **Cancellation Policy**| `inv.isRefundable` | Evaluated as boolean; maps cleanly to string values `"Refundable"` or `"Non Refundable"`. |
| **Breakfast** | `inv.isBreakfastIncluded` | Evaluated as boolean; maps cleanly to `"With Breakfast"` or `"Without Breakfast"`. |
| **Price** | `inv.rateDisplay.baseFare.amount` | **Dynamic Decimal Scaling / Discount Behavior:** The active rate per night. The raw API stores amounts as cent integers (e.g. `209033`). The parser divides dynamically by $10^{\text{numOfDecimalPoint}}$ to scale to `2090.33`. *Note: If a promotion is active, this key represents the discounted rate, and the original base rate is exported separately.* |
| **Currency** | `inv.rateDisplay.baseFare.currency` | Sourced dynamically from the fare object (e.g., `"THB"`). |
| **Total taxes** | `inv.rateDisplay.taxes.amount` | Scaled dynamically using the same `numOfDecimalPoint` division logic. |
| **Total prices** | `inv.rateDisplay.totalFare.amount` | Represents the complete final amount per night, scaled dynamically. |
| **original_price** | `inv.originalRateDisplay.baseFare.amount` | Optional pre-sale original base rate (if a discount is active). Extracted, scaled, and included only when the original rate exceeds the active price. |
| **net_price_per_stay** | Computed Per Stay Metric | Sourced dynamically by computing `price * num_nights` (rounded to 2 decimal places) representing the total room price before taxes for the duration of the stay. |
| **shown_price_per_stay** | Computed Per Stay Metric | Sourced dynamically by computing `price * num_nights` (rounded to 2 decimal places) representing the displayed room price before taxes for the stay. *Note: Identical to net_price_per_stay because Traveloka Desktop displays pre-tax base rates as primary pricing elements before calculating and adding tax markup at the checkout stage.* |
| **total_price_per_stay** | Computed Per Stay Metric | Sourced dynamically by computing `total_price * num_nights` (rounded to 2 decimal places) representing the total final pricing (with taxes included) for the entire stay. |
| **deep_link** | Programmatic Generation | Dynamically constructed by appending the specific room's ID (`roomId`) and preserving the hotel slug (`slug` extracted from the URL query) to build canonical checkout pathways: `https://www.traveloka.com/en-th/hotel/detail?spec={checkin}.{checkout}.1.1.HOTEL.{hotelId}.{slug}&roomId={roomId}`. |

---

## 🚀 Execution & Setup

### 1. Install Required Dependencies
The scraper relies on standard Python networking libraries. To enable browser-assisted recovery support, Playwright is included:
```bash
pip install requests curl_cffi playwright
playwright install chromium
```

### 2. Run the Scraper
Execute the script directly from the terminal:
```bash
python traveloka_scraper.py
```

### 3. Pipeline Runtime Behavior
* The script initiates **Step 1 (Direct Requests)** using the high-performance `curl_cffi` TLS/HTTP2 session. If the connection succeeds, the data is captured instantly, and the program terminates.
* If the gateway restricts direct access (returning WAF challenge headers or HTTP blocks), the script transitions to **Step 2 (Browser-Assisted Fallback)**. A headful browser window is displayed to allow the session to settle and let UI resources load.
* Once the browser successfully retrieves the `200 OK` room response, the socket-level interceptor automatically captures the raw JSON payload, closes the browser instance, and outputs the formatted room rates to `rates_output.json`.
