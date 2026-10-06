import os
import socket

# ============================================================
# NETWORK: PREFER IPv4 FOR OUTGOING CONNECTIONS
# ============================================================
# On some networks (for example phone hotspots) IPv6 addresses are returned
# for AWS hosts but do not actually work. Python then waits for each IPv6
# address to time out before trying IPv4, which made API Gateway calls take
# ~20s and S3 uploads appear to hang. Looking up IPv4 addresses first avoids
# that wait. This only changes how host names are resolved; it does not
# change any AWS service, URL, bucket or request.
#
# To turn it off, set the environment variable SPENDSENSE_FORCE_IPV4=0.

if os.getenv("SPENDSENSE_FORCE_IPV4", "1") != "0":

    _original_getaddrinfo = socket.getaddrinfo

    def _ipv4_first_getaddrinfo(
        host, port, family=0, type=0, proto=0, flags=0
    ):
        if family in (0, socket.AF_UNSPEC):
            try:
                return _original_getaddrinfo(
                    host, port, socket.AF_INET, type, proto, flags
                )
            except socket.gaierror:
                pass  # no IPv4 address: fall back to the normal lookup

        return _original_getaddrinfo(host, port, family, type, proto, flags)

    socket.getaddrinfo = _ipv4_first_getaddrinfo


from flask import Flask, render_template, request, redirect, url_for, flash, g, has_request_context
from werkzeug.utils import secure_filename
from botocore.exceptions import BotoCoreError, ClientError
from botocore.config import Config
import boto3
import os
import uuid
import json
import re
import tempfile
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pypdf import PdfReader
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# ============================================================
# APP CONFIGURATION
# ============================================================

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "spendsense-dev-key")


# ============================================================
# FILE UPLOAD CONFIGURATION
# ============================================================

UPLOAD_FOLDER = os.path.join(
    app.root_path,
    "static",
    "uploads"
)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


# ============================================================
# AWS CONFIGURATION
# ============================================================

AWS_PROFILE = os.getenv("AWS_PROFILE") or "spendsense"
AWS_REGION = os.getenv("AWS_REGION") or "ap-south-1"

S3_BUCKET = os.getenv(
    "S3_BUCKET",
    "spendsense-invoices-megh-2026"
)

API_URL = os.getenv(
    "API_URL",
    "https://8ngwlz4334.execute-api.ap-south-1.amazonaws.com/expenses"
)


# ============================================================
# AWS SESSION
# ============================================================

# Render / cloud deployment:
# If AWS credentials are supplied as environment variables,
# boto3 automatically uses them.

if (
    os.getenv("AWS_ACCESS_KEY_ID")
    and os.getenv("AWS_SECRET_ACCESS_KEY")
):

    aws_session = boto3.Session(
        region_name=AWS_REGION
    )

else:

    # Local development: use the configured AWS CLI profile.
    # If it is unavailable, fall back to boto3's normal credential chain.
    try:
        aws_session = boto3.Session(
            profile_name=AWS_PROFILE,
            region_name=AWS_REGION
        )
    except Exception as error:
        print(
            f"AWS profile '{AWS_PROFILE}' unavailable; "
            f"using default boto3 credentials: {error}"
        )
        aws_session = boto3.Session(
            region_name=AWS_REGION
        )


# ============================================================
# AWS S3 CLIENT
# ============================================================

s3 = aws_session.client(
    "s3",
    region_name=AWS_REGION
)

# Separate client used ONLY for the background "read the PDF back from S3"
# enrichment step. It has short timeouts and no retries so a slow/blocked
# connection cannot freeze a page for minutes. The upload client above
# (s3) is unchanged.
s3_enrichment = aws_session.client(
    "s3",
    region_name=AWS_REGION,
    config=Config(
        connect_timeout=4,
        read_timeout=8,
        retries={"max_attempts": 1},
    )
)

# S3 PDF reading happens in background threads (never inside a page
# request), so pages open immediately and amounts for not-yet-read
# invoices appear on the next refresh.
_enrichment_executor = ThreadPoolExecutor(max_workers=2)
_enrichment_lock = threading.Lock()
api_invoice_pending = set()


# ============================================================
# API GATEWAY → LAMBDA → DYNAMODB
# ============================================================

def _fetch_expenses_from_api_now():
    """
    Fetch invoice/expense records from:

    API Gateway → Lambda → DynamoDB

    Returns None if the API is unavailable (the caller keeps the last
    good result instead of showing nothing).
    """

    started = time.time()

    try:

        api_request = Request(
            API_URL,
            headers={
                "Accept": "application/json"
            },
            method="GET"
        )

        with urlopen(api_request, timeout=10) as response:

            raw_data = response.read().decode("utf-8")

            data = json.loads(raw_data)

        print(f"API call took {time.time() - started:.1f}s")

        if data.get("success"):

            return data.get("expenses", [])

        print(
            "API returned an unsuccessful response:",
            data
        )

        return None

    except HTTPError as error:

        print(
            f"API HTTP error: "
            f"{error.code} - {error.reason}"
        )

        return None

    except URLError as error:

        print(
            f"API connection error: "
            f"{error.reason}"
        )

        return None

    except Exception as error:

        print(
            f"Unexpected API error: {error}"
        )

        return None


# ------------------------------------------------------------
# NON-BLOCKING ACCESS TO THE API (stale-while-revalidate)
# ------------------------------------------------------------
# The API Gateway call can be slow (20s+ on some networks / cold Lambdas).
# Pages must not wait for it on every click, so:
#   * the last successful response is kept in memory and served instantly
#   * when it is older than API_CACHE_FRESH_SECONDS a refresh runs in a
#     background thread
#   * a failed refresh never wipes the last good data
#   * a refresh is also started when the app starts
# The API itself, its URL and its response format are unchanged.

API_CACHE_FRESH_SECONDS = 30
API_FIRST_LOAD_WAIT_SECONDS = 2

_api_cache = {"data": None, "fetched_at": 0.0, "refreshing": False}
_api_cache_lock = threading.Lock()
_api_first_response = threading.Event()


def _refresh_api_cache():
    try:
        result = _fetch_expenses_from_api_now()

        if result is not None:
            with _api_cache_lock:
                _api_cache["data"] = result
                _api_cache["fetched_at"] = time.time()
    finally:
        with _api_cache_lock:
            _api_cache["refreshing"] = False
        _api_first_response.set()


def _start_api_refresh_if_needed():
    with _api_cache_lock:
        stale = (
            _api_cache["data"] is None
            or time.time() - _api_cache["fetched_at"] > API_CACHE_FRESH_SECONDS
        )

        if not stale or _api_cache["refreshing"]:
            return

        _api_cache["refreshing"] = True

    threading.Thread(target=_refresh_api_cache, daemon=True).start()


def get_expenses_from_api():
    """
    Fetch invoice/expense records from:

    API Gateway -> Lambda -> DynamoDB

    Returns instantly from the in-memory copy of the last good response.
    Returns an empty list only if no response has ever been received.
    """
    _start_api_refresh_if_needed()

    if _api_cache["data"] is None:
        # Very first load: give the in-flight request a short moment.
        _api_first_response.wait(timeout=API_FIRST_LOAD_WAIT_SECONDS)

    with _api_cache_lock:
        return list(_api_cache["data"] or [])


# ============================================================
# MAKE API CONFIG AVAILABLE TO TEMPLATES
# ============================================================

@app.context_processor
def inject_api_config():

    return {
        "api_url": API_URL
    }


# ============================================================
# SETTINGS
# ============================================================

# Settings are stored in a small local JSON file next to app.py (the same
# approach already used for uploaded_invoices_cache.json). This does NOT
# touch S3 / API Gateway / Lambda / DynamoDB.
#
# The file is re-read on every request (and cached in flask.g for that
# request) so that several gunicorn workers always see the latest values.

DEFAULT_SETTINGS = {

    "name": "Ananya",

    "currency": "INR (₹)",

    "notifications": True,

    # 0 means "no budget set"
    "monthly_budget": 0,

    # How many rows the dashboard "Recent Transactions" table shows
    "recent_count": 5,
}

CURRENCY_SYMBOLS = {
    "INR (₹)": "₹",
    "USD ($)": "$",
    "EUR (€)": "€",
}

RECENT_COUNT_CHOICES = (5, 10, 20)

SETTINGS_FILE = os.path.join(
    app.root_path,
    "user_settings.json"
)


def sanitize_settings(raw):
    """Return a complete, validated settings dict (bad values -> defaults)."""
    raw = raw if isinstance(raw, dict) else {}
    clean = dict(DEFAULT_SETTINGS)

    name = str(raw.get("name", "") or "").strip()[:40]
    if name:
        clean["name"] = name

    if raw.get("currency") in CURRENCY_SYMBOLS:
        clean["currency"] = raw["currency"]

    if isinstance(raw.get("notifications"), bool):
        clean["notifications"] = raw["notifications"]

    try:
        budget = float(raw.get("monthly_budget", 0) or 0)
        if 0 <= budget <= 1_000_000_000:
            clean["monthly_budget"] = round(budget, 2)
    except (TypeError, ValueError):
        pass

    try:
        recent_count = int(raw.get("recent_count", 5))
        if recent_count in RECENT_COUNT_CHOICES:
            clean["recent_count"] = recent_count
    except (TypeError, ValueError):
        pass

    return clean


def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as settings_file:
                return sanitize_settings(json.load(settings_file))
    except Exception as error:
        print(f"Settings load warning: {error}")
    return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    """Write settings atomically. Returns True only if the file was written."""
    temp_path = SETTINGS_FILE + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as settings_file:
            json.dump(settings, settings_file, ensure_ascii=False, indent=2)
        os.replace(temp_path, SETTINGS_FILE)
        return True
    except Exception as error:
        print(f"Settings save error: {error}")
        try:
            os.remove(temp_path)
        except OSError:
            pass
        return False


def get_settings():
    """Settings for the current request (read from disk once per request)."""
    if "app_settings" not in g:
        g.app_settings = load_settings()
    return g.app_settings


def get_currency_symbol():
    return CURRENCY_SYMBOLS.get(get_settings().get("currency"), "₹")


@app.before_request
def _start_timer():
    g.request_started = time.time()


@app.after_request
def _log_request_time(response):
    started = g.get("request_started")
    if started and not request.path.startswith("/static"):
        print(f"{request.method} {request.path} took {time.time() - started:.2f}s")
    return response


@app.context_processor
def inject_settings():
    """Make the saved settings available to every template."""
    return {
        "app_settings": get_settings(),
        "currency_symbol": get_currency_symbol(),
    }


@app.template_filter("money")
def money_filter(value, decimals=0):
    """{{ 1234.5|money }} -> ₹1,235   |   {{ 1234.5|money(2) }} -> ₹1,234.50"""
    try:
        amount = _to_float(value)
    except Exception:
        amount = 0.0

    return f"{get_currency_symbol()}{amount:,.{int(decimals)}f}"


@app.template_filter("money_short")
def money_short_filter(value):
    """Compact amount for chart labels: 12400 -> ₹12.4k"""
    try:
        amount = _to_float(value)
    except Exception:
        amount = 0.0

    magnitude = abs(amount)

    if magnitude >= 1_000_000:
        text = f"{amount / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    elif magnitude >= 1_000:
        text = f"{amount / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    else:
        text = f"{amount:.0f}"

    return f"{get_currency_symbol()}{text}"


# ============================================================
# DEMO TRANSACTIONS
# ============================================================

transactions = [

    {
        "date": "21 Sep 2026",
        "merchant": "The Urban Cafe",
        "category": "Food & Dining",
        "amount": 1123.50,
        "status": "Processed"
    },

    {
        "date": "18 Sep 2026",
        "merchant": "Swiggy",
        "category": "Food & Dining",
        "amount": 847.00,
        "status": "Processed"
    },

    {
        "date": "15 Sep 2026",
        "merchant": "Uber",
        "category": "Travel",
        "amount": 420.00,
        "status": "Processed"
    },

    {
        "date": "12 Sep 2026",
        "merchant": "Amazon",
        "category": "Shopping",
        "amount": 1299.00,
        "status": "Processed"
    },

    {
        "date": "10 Sep 2026",
        "merchant": "Reliance Fresh",
        "category": "Groceries",
        "amount": 2450.00,
        "status": "Processed"
    },

    {
        "date": "5 Sep 2026",
        "merchant": "Airtel",
        "category": "Utilities",
        "amount": 999.00,
        "status": "Processed"
    },

    {
        "date": "2 Sep 2026",
        "merchant": "Zomato",
        "category": "Food & Dining",
        "amount": 650.00,
        "status": "Processed"
    }

]


# ============================================================
# LOCAL UPLOADED INVOICE CACHE
# ============================================================
# This keeps the extracted result available immediately after an
# upload, without changing the existing AWS architecture.
#
# Cloud services remain:
#   1. Amazon S3       -> stores the original invoice
#   2. API Gateway     -> existing application API
#   3. Lambda/DynamoDB -> existing invoice/expense records
#
# PDF text extraction is intentionally local because Textract is
# not being used in this project.
uploaded_invoices = []

# Persist locally parsed uploads across Flask restarts during local development.
# Cloud records remain stored in S3/API Gateway/Lambda/DynamoDB and are reloaded
# from the AWS API on every request.
UPLOAD_CACHE_FILE = os.path.join(
    app.root_path,
    "uploaded_invoices_cache.json"
)

def load_uploaded_invoice_cache():
    try:
        if os.path.exists(UPLOAD_CACHE_FILE):
            with open(UPLOAD_CACHE_FILE, "r", encoding="utf-8") as cache_file:
                data = json.load(cache_file)
                if isinstance(data, list):
                    return data
    except Exception as error:
        print(f"Upload cache load warning: {error}")
    return []

def save_uploaded_invoice_cache():
    try:
        with open(UPLOAD_CACHE_FILE, "w", encoding="utf-8") as cache_file:
            json.dump(uploaded_invoices, cache_file, ensure_ascii=False, indent=2)
    except Exception as error:
        print(f"Upload cache save warning: {error}")

uploaded_invoices.extend(load_uploaded_invoice_cache())

# Parsed metadata cache for AWS invoices. DynamoDB currently stores
# S3/file metadata, while the PDF itself contains the bill amount,
# merchant, date and other fields. We parse text PDFs locally and
# cache the result so Analytics/Categories can use the same data
# without changing the 3-service AWS architecture.
api_invoice_enrichment_cache = {}

# If downloading/parsing an invoice from S3 fails, remember that for a few
# minutes so every page load does not wait on the same failing download
# again (this is what made pages hang on a slow/failing connection).
api_invoice_failure_cache = {}
ENRICHMENT_RETRY_SECONDS = 300


def extract_invoice_text(pdf_path):
    """
    Extract selectable text from a PDF using pypdf.

    This does NOT replace S3/API Gateway/Lambda/DynamoDB.
    It is only a lightweight local parser for text-based PDFs.
    Scanned/image-only PDFs may return little or no text.
    """
    try:
        reader = PdfReader(pdf_path)
        pages = []

        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception as error:
                print(f"PDF page extraction warning: {error}")

        return "\n".join(pages).strip()

    except Exception as error:
        print(f"PDF extraction error: {error}")
        return ""


def _clean_money(value):
    """Convert common currency strings into a float."""
    if not value:
        return None

    cleaned = re.sub(r"[^\d.,]", "", value)

    # Handle 1,234.50 and 1234.50
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        # Treat a single comma followed by 1-2 digits as decimal.
        if re.search(r",\d{1,2}$", cleaned):
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")

    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_invoice_fields(text, filename):
    """
    Extract common invoice fields from selectable-text PDFs.
    Keeps the existing AWS/S3 workflow unchanged.
    """
    result = {
        "invoice_name": filename,
        "invoice_number": "Not detected",
        "merchant": "Not detected",
        "date": "Not detected",
        "amount": 0.0,
        "category": "Other",
        "extracted_text": text,
    }

    if not text:
        return result

    # Normalize PDF text while preserving useful line boundaries.
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in text.splitlines()
        if line.strip()
    ]

    normalized_text = "\n".join(lines)

    # ---------------------------------------------------------
    # INVOICE NUMBER
    # ---------------------------------------------------------
    invoice_patterns = [
        r"\binvoice\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9./_-]*)",
        r"\binvoice\s*[:\-]\s*([A-Za-z0-9][A-Za-z0-9./_-]*)",
        r"\bbill\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9./_-]*)",
        r"\bbill\s*[:\-]\s*([A-Za-z0-9][A-Za-z0-9./_-]*)",
    ]

    for pattern in invoice_patterns:
        match = re.search(pattern, normalized_text, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()

            # Don't accidentally use a generic word such as
            # "Invoice" or "Bill" as the invoice number.
            if candidate.lower() not in {
                "invoice",
                "bill",
                "number",
                "no",
            }:
                result["invoice_number"] = candidate
                break

    # ---------------------------------------------------------
    # DATE
    # ---------------------------------------------------------
    date_patterns = [
        r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b",
        r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})\b",
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{2,4})\b",
    ]

    for pattern in date_patterns:
        match = re.search(pattern, normalized_text, re.IGNORECASE)
        if match:
            result["date"] = match.group(1).strip()
            break

      # ---------------------------------------------------------
    # TOTAL AMOUNT
    # ---------------------------------------------------------
    # PDF extraction can place the label and amount on separate
    # lines, for example:
    #
    # Grand Total
    # Rs. 6,602.10
    #
    # So we check both the current line and the next few lines.

    total_keywords = [
        "grand total",
        "total amount",
        "amount due",
        "net total",
        "balance due",
        "total",
    ]

    total_candidates = []

    for index, line in enumerate(lines):
        low_line = line.lower()

        if not any(keyword in low_line for keyword in total_keywords):
            continue

        # Check the current line plus the next 3 lines.
        nearby_lines = lines[index:index + 4]
        nearby_text = " ".join(nearby_lines)

        amount_matches = re.findall(
            r"(?:₹|rs\.?|inr|\$)?\s*"
            r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
            nearby_text,
            re.IGNORECASE,
        )

        for value in amount_matches:
            cleaned = _clean_money(value)

            if cleaned is not None and cleaned > 0:
                total_candidates.append(cleaned)

    if total_candidates:
        # The last amount associated with the total section
        # is normally the final invoice total.
        result["amount"] = total_candidates[-1]

    # ---------------------------------------------------------
    # MERCHANT
    # ---------------------------------------------------------
    merchant_candidates = []

    for line in lines[:15]:
        low = line.lower()

        if any(word in low for word in [
            "invoice",
            "bill",
            "date",
            "gst",
            "tax",
            "phone",
            "email",
            "address",
            "total",
            "amount",
            "receipt",
            "customer",
            "payment",
        ]):
            continue

        if re.search(r"\d{6,}", line):
            continue

        if 2 <= len(line) <= 80:
            merchant_candidates.append(line)

    if merchant_candidates:
        result["merchant"] = merchant_candidates[0]

    # ---------------------------------------------------------
    # CATEGORY
    # ---------------------------------------------------------
    low_text = normalized_text.lower()

    if any(x in low_text for x in [
        "restaurant",
        "cafe",
        "food",
        "meal",
        "dining",
        "pizza",
        "burger",
    ]):
        result["category"] = "Food & Dining"

    elif any(x in low_text for x in [
        "uber",
        "ola",
        "taxi",
        "transport",
        "cab",
    ]):
        result["category"] = "Travel"

    elif any(x in low_text for x in [
        "amazon",
        "shopping",
        "store",
        "retail",
        "electronics",
        "keyboard",
        "mouse",
        "laptop",
        "usb",
    ]):
        result["category"] = "Shopping"

    elif any(x in low_text for x in [
        "grocery",
        "groceries",
        "supermarket",
    ]):
        result["category"] = "Groceries"

    return result

def build_uploaded_invoice_record(
    original_filename,
    local_path,
    s3_key,
    content_type,
):
    """Create the dashboard-friendly record for an uploaded invoice."""
    extracted_text = extract_invoice_text(local_path)
    extracted = extract_invoice_fields(
        extracted_text,
        original_filename,
    )

    return {
        "invoice_name": original_filename,
        "invoice_number": extracted["invoice_number"],
        "merchant": extracted["merchant"],
        "date": extracted["date"],
        "amount": extracted["amount"],
        # Keep "total" too because the invoice-detail template can
        # display this field directly.
        "total": extracted["amount"],
        "category": extracted["category"],
        "s3_bucket": S3_BUCKET,
        "s3_key": s3_key,
        "file_size": os.path.getsize(local_path),
        "content_type": content_type or "application/octet-stream",
        "status": "processed" if extracted_text else "uploaded - text not detected",
        "extracted_text": extracted_text,
        "source": "local_pdf_parser",
    }



# ============================================================
# DATA NORMALIZATION / COMBINATION
# ============================================================

def _to_float(value, default=0.0):
    """Safely convert an amount-like value to float."""
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = re.sub(r"[^0-9.\-]", "", value.replace(",", ""))
        return float(value)
    except (TypeError, ValueError):
        return default


def infer_category(text):
    """Infer a spending category from invoice text/name/merchant."""
    low = (text or "").lower()

    if any(x in low for x in [
        "restaurant", "cafe", "café", "food", "meal", "dining",
        "pizza", "burger", "swiggy", "zomato", "bakery"
    ]):
        return "Food & Dining"

    if any(x in low for x in [
        "grocery", "groceries", "supermarket", "reliance fresh",
        "dmart", "d-mart", "vegetables", "provisions"
    ]):
        return "Groceries"

    if any(x in low for x in [
        "uber", "ola", "taxi", "cab", "transport", "metro",
        "railway", "flight", "airlines", "travel", "bus"
    ]):
        return "Travel"

    if any(x in low for x in [
        "amazon", "flipkart", "shopping", "retail", "clothing",
        "fashion", "mall", "store"
    ]):
        return "Shopping"

    if any(x in low for x in [
        "electricity", "electric", "water bill", "internet",
        "broadband", "airtel", "jio", "vodafone", "utility",
        "utilities", "recharge"
    ]):
        return "Utilities"

    if any(x in low for x in [
        "movie", "cinema", "netflix", "spotify", "entertainment",
        "gaming", "bookmyshow"
    ]):
        return "Entertainment"

    return "Other"


def normalize_transaction(record):
    """Convert any expense/invoice record into one common structure."""
    record = record or {}

    invoice_name = (
        record.get("invoice_name")
        or record.get("filename")
        or ""
    )

    merchant = (
        record.get("merchant")
        or record.get("vendor")
        or record.get("shop_name")
        or invoice_name
        or "AWS Invoice"
    )

    amount = _to_float(
        record.get(
            "amount",
            record.get("total", record.get("total_amount", 0))
        )
    )

    date = (
        record.get("date")
        or record.get("invoice_date")
        or record.get("uploaded_at")
        or "Processed"
    )

    searchable_text = " ".join([
        str(invoice_name),
        str(merchant),
        str(record.get("extracted_text", "")),
        str(record.get("description", "")),
        str(record.get("items", "")),
        str(record.get("category", "")),
    ])

    category = record.get("category")

    if not category or str(category).strip().lower() in {
        "uncategorized", "unknown", "not detected", "other"
    }:
        inferred = infer_category(searchable_text)
        if inferred != "Other" or not category:
            category = inferred

    return {
        "date": date,
        "uploaded_at": record.get("uploaded_at", ""),
        "merchant": merchant,
        "category": category,
        "amount": amount,
        "total": amount,
        "status": record.get("status", "Processed"),
        "invoice_name": invoice_name,
        "invoice_number": record.get("invoice_number", "Not detected"),
        "s3_key": record.get("s3_key", ""),
        "s3_bucket": record.get("s3_bucket", S3_BUCKET),
        "file_size": record.get("file_size", 0),
        "content_type": record.get(
            "content_type", "application/octet-stream"
        ),
        "source": record.get("source", "aws_api"),
    }


def _enrich_api_invoice_now(record):
    """
    (Runs in a background thread - see enrich_api_invoice below.)

    Enrich an API/DynamoDB invoice record when the backend only has
    S3/file metadata.

    For text-based PDFs, download the PDF from S3 and run the same
    local pypdf parser used for freshly uploaded invoices. This gives
    Analytics, Categories and the dashboard a real amount/category
    while keeping:
        Amazon S3
        API Gateway
        Lambda + DynamoDB
    as the cloud architecture.

    Image/scanned invoices are left unchanged because Textract/OCR
    is intentionally not part of this project.
    """
    record = dict(record or {})

    key = record.get("s3_key") or record.get("expense_id") or ""
    if key.startswith("invoices/") is False:
        return record

    # If a usable amount is already present, only add total for
    # template compatibility.
    existing_amount = _to_float(
        record.get(
            "amount",
            record.get("total", record.get("total_amount", 0)),
        )
    )

    if existing_amount > 0:
        record["amount"] = existing_amount
        record["total"] = existing_amount
        return record

    failed_at = api_invoice_failure_cache.get(key)
    if failed_at and time.time() - failed_at < ENRICHMENT_RETRY_SECONDS:
        return record

    if key in api_invoice_enrichment_cache:
        cached = api_invoice_enrichment_cache[key]
        merged = dict(record)
        merged.update(cached)
        return merged

    filename = record.get("invoice_name") or os.path.basename(key)

    # Only attempt local parsing for PDF objects. There is no OCR
    # available for PNG/JPG in this project.
    extension = os.path.splitext(filename)[1].lower()
    if extension != ".pdf":
        return record

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".pdf",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name

        print(f"Enriching AWS PDF from S3: {key}")

        s3_enrichment.download_file(
            record.get("s3_bucket") or S3_BUCKET,
            key,
            temp_path,
        )

        extracted_text = extract_invoice_text(temp_path)
        extracted = extract_invoice_fields(
            extracted_text,
            filename,
        )

        # Only enrich fields that the PDF parser actually found.
        enrichment = {
            "extracted_text": extracted_text,
            "source": "aws_api_s3_local_pdf_parser",
        }

        if extracted.get("invoice_number") != "Not detected":
            enrichment["invoice_number"] = extracted["invoice_number"]

        if extracted.get("merchant") != "Not detected":
            enrichment["merchant"] = extracted["merchant"]

        if extracted.get("date") != "Not detected":
            enrichment["date"] = extracted["date"]

        parsed_amount = _to_float(extracted.get("amount", 0))
        if parsed_amount > 0:
            enrichment["amount"] = parsed_amount
            enrichment["total"] = parsed_amount

        parsed_category = extracted.get("category")
        if parsed_category and parsed_category != "Other":
            enrichment["category"] = parsed_category

        api_invoice_enrichment_cache[key] = enrichment

        merged = dict(record)
        merged.update(enrichment)

        print(
            "AWS PDF enrichment:",
            merged.get("merchant", "Not detected"),
            merged.get("amount", 0),
            merged.get("category", "Other"),
        )

        return merged

    except Exception as error:
        api_invoice_failure_cache[key] = time.time()
        print(
            f"AWS PDF enrichment skipped for {key}: {error}"
        )
        return record

    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _run_background_enrichment(record, key):
    try:
        _enrich_api_invoice_now(record)
    except Exception as error:
        api_invoice_failure_cache[key] = time.time()
        print(f"Background enrichment error for {key}: {error}")
    finally:
        with _enrichment_lock:
            api_invoice_pending.discard(key)


def enrich_api_invoice(record):
    """
    Fast, non-blocking version used by every page.

    Returns the record immediately. If the PDF has already been read from
    S3, the cached amount/merchant/date/category are merged in. If not, the
    download + local PDF parsing is started in a background thread (same
    logic as before, in _enrich_api_invoice_now) and the result shows up
    on the next page load.
    """
    record = dict(record or {})

    key = record.get("s3_key") or record.get("expense_id") or ""
    if not key.startswith("invoices/"):
        return record

    existing_amount = _to_float(
        record.get(
            "amount",
            record.get("total", record.get("total_amount", 0)),
        )
    )

    if existing_amount > 0:
        record["amount"] = existing_amount
        record["total"] = existing_amount
        return record

    if key in api_invoice_enrichment_cache:
        merged = dict(record)
        merged.update(api_invoice_enrichment_cache[key])
        return merged

    filename = record.get("invoice_name") or os.path.basename(key)

    if os.path.splitext(filename)[1].lower() != ".pdf":
        return record

    failed_at = api_invoice_failure_cache.get(key)
    if failed_at and time.time() - failed_at < ENRICHMENT_RETRY_SECONDS:
        return record

    with _enrichment_lock:
        if key in api_invoice_pending:
            return record
        api_invoice_pending.add(key)

    _enrichment_executor.submit(_run_background_enrichment, record, key)

    return record


def get_combined_expenses():
    """
    Combine demo transactions, locally parsed uploads and AWS records.

    If the same uploaded invoice exists locally and in the AWS API,
    the locally parsed version is preferred because it contains the
    amount/category extracted from the PDF.
    """
    api_expenses = get_expenses_from_api()

    combined = [normalize_transaction(t) for t in transactions]

    local_by_key = {}
    local_by_name = {}

    for record in uploaded_invoices:
        normalized = normalize_transaction(record)

        if normalized["s3_key"]:
            local_by_key[normalized["s3_key"]] = normalized

        if normalized["invoice_name"]:
            local_by_name[normalized["invoice_name"]] = normalized

    api_keys = set()
    api_names = set()

    for raw_record in api_expenses:
        # First enrich text PDFs from S3 when DynamoDB only contains
        # file metadata.
        record = enrich_api_invoice(raw_record)

        key = record.get("s3_key") or record.get("expense_id") or ""
        name = record.get("invoice_name") or ""

        local = local_by_key.get(key) or local_by_name.get(name)

        if local:
            combined.append(local)
        else:
            combined.append(normalize_transaction(record))

        if key:
            api_keys.add(key)

        if name:
            api_names.add(name)

    # Immediately include a fresh upload even if Lambda/DynamoDB
    # has not returned it through API Gateway yet.
    for record in uploaded_invoices:
        key = record.get("s3_key") or ""
        name = record.get("invoice_name") or ""

        if key and key in api_keys:
            continue

        if name and name in api_names:
            continue

        combined.append(normalize_transaction(record))

    return combined, api_expenses

# ============================================================
# MONTHLY SPENDING CALCULATION
# ============================================================

_DATE_FORMATS = [
    "%d %B %Y",
    "%d %b %Y",
    "%d %B, %Y",
    "%d %b, %Y",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%d %b %y",
]

_NO_DATE_TEXT = {"", "not detected", "processed", "unknown", "none", "n/a"}


def _parse_date_text(value):
    """Parse one date string; returns None if it is missing or malformed."""
    text = str(value or "").strip()

    if text.lower() in _NO_DATE_TEXT:
        return None

    parsed = None

    # ISO date/time from AWS
    try:
        parsed = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        ).replace(tzinfo=None)
    except Exception:
        # Common invoice date formats
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except Exception:
                continue

    # Guard against nonsense dates produced by text extraction.
    if parsed is None or not (2000 <= parsed.year <= 2100):
        return None

    return parsed


def parse_transaction_date(transaction):
    """
    Best usable date for a transaction, or None.

    Tries the invoice date first and falls back to the upload time, so a
    value such as "Not detected" no longer hides a valid uploaded_at.
    """
    for field in ("date", "invoice_date", "uploaded_at"):
        parsed = _parse_date_text(transaction.get(field))
        if parsed is not None:
            return parsed

    return None


def _shift_month(year, month, delta):
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def calculate_monthly_series(all_transactions, months=6):
    """
    Spending per month for the latest `months` months, from real
    transaction dates and amounts. This is the ONE monthly series used by
    both the Dashboard bar chart and the Analytics line chart.

    The window ends at the month of the most recent dated transaction
    (so demo / older invoices still produce a meaningful chart). Months
    without spending are returned with value 0.
    """
    dated = []

    for transaction in all_transactions or []:
        transaction_date = parse_transaction_date(transaction)

        if transaction_date is None:
            continue

        dated.append((
            transaction_date,
            _to_float(transaction.get("amount", 0))
        ))

    anchor = (
        max(d for d, _ in dated)
        if dated
        else datetime.now()
    )

    series = []
    by_month = {}

    for offset in range(months - 1, -1, -1):
        year, month = _shift_month(anchor.year, anchor.month, -offset)
        month_start = datetime(year, month, 1)

        entry = {
            "key": month_start.strftime("%Y-%m"),
            "label": month_start.strftime("%b"),
            "full_label": month_start.strftime("%b %Y"),
            "value": 0.0,
            "count": 0,
        }

        series.append(entry)
        by_month[(year, month)] = entry

    for transaction_date, amount in dated:
        entry = by_month.get((transaction_date.year, transaction_date.month))

        if entry is not None:
            entry["value"] += amount
            entry["count"] += 1

    for entry in series:
        entry["value"] = round(entry["value"], 2)

    return series


def calculate_monthly_spending(all_transactions):
    """Backward-compatible (labels, values) view of the monthly series."""
    series = calculate_monthly_series(all_transactions)

    return (
        [entry["label"] for entry in series],
        [entry["value"] for entry in series],
    )


def calculate_category_totals(all_transactions):
    totals = {}

    for transaction in all_transactions:
        category = transaction.get("category") or "Other"
        amount = _to_float(transaction.get("amount", 0))
        totals[category] = totals.get(category, 0) + amount

    return totals


# ============================================================
# SPENDING SUMMARY  (single source of truth for charts/analytics)
# ============================================================

CHART_COLORS = [
    "#6257e8", "#8278ee", "#4ec8b0", "#f2b34d",
    "#e9788a", "#4785df", "#d6d2fa", "#9aa0b2",
]

_UNKNOWN_MERCHANTS = {
    "", "not detected", "aws invoice", "unknown", "processed",
}


def build_line_chart(series, width=600, height=240):
    """
    Pre-computed SVG geometry for the Analytics line chart, derived from the
    same monthly series the Dashboard bar chart uses.
    """
    left, right, top, bottom = 52, 18, 18, 34

    plot_w = width - left - right
    plot_h = height - top - bottom
    baseline = top + plot_h

    max_value = max((entry["value"] for entry in series), default=0)
    count = len(series)

    points = []

    for index, entry in enumerate(series):
        x = left + (plot_w * index / (count - 1) if count > 1 else plot_w / 2)
        y = baseline - (
            plot_h * entry["value"] / max_value if max_value > 0 else 0
        )

        points.append({
            "x": round(x, 1),
            "y": round(y, 1),
            "label": entry["label"],
            "full_label": entry["full_label"],
            "value": entry["value"],
        })

    if points:
        line = " ".join(f"{p['x']},{p['y']}" for p in points)
        area = (
            f"M{points[0]['x']},{baseline} "
            + " ".join(f"L{p['x']},{p['y']}" for p in points)
            + f" L{points[-1]['x']},{baseline} Z"
        )
    else:
        line = ""
        area = ""

    ticks = [
        {
            "y": round(baseline - plot_h * fraction, 1),
            "value": max_value * fraction,
        }
        for fraction in ((0, 0.5, 1) if max_value > 0 else (0,))
    ]

    return {
        "width": width,
        "height": height,
        "left": left,
        "right": width - right,
        "baseline": baseline,
        "points": points,
        "line": line,
        "area": area,
        "ticks": ticks,
        "has_data": max_value > 0,
    }


def build_spending_summary(all_transactions, settings=None, recent_limit=5):
    """
    Every number shown on the Dashboard and Analytics pages is computed here,
    once, from the combined transaction list (the same list the rest of the
    app already uses). Nothing in this function is hard-coded or random, and
    nothing is invented: a metric that cannot be computed is None / empty.
    """
    settings = settings or DEFAULT_SETTINGS
    transactions_list = list(all_transactions or [])

    # ---------------- totals ----------------
    amounts = [_to_float(t.get("amount", 0)) for t in transactions_list]
    total = sum(amounts)
    count = len(transactions_list)

    priced = [
        (t, a) for t, a in zip(transactions_list, amounts) if a > 0
    ]
    unpriced_count = count - len(priced)

    average = (
        sum(a for _, a in priced) / len(priced) if priced else 0.0
    )

    highest = None
    if priced:
        top_t, top_a = max(priced, key=lambda pair: pair[1])
        highest = {
            "merchant": top_t.get("merchant") or "Unknown",
            "category": top_t.get("category") or "Other",
            "date": top_t.get("date") or "",
            "amount": top_a,
        }

    # ---------------- months ----------------
    monthly = calculate_monthly_series(transactions_list)
    month_keys = {entry["key"] for entry in monthly}

    dated_count = 0
    outside_window_count = 0

    for t in transactions_list:
        parsed = parse_transaction_date(t)

        if parsed is None:
            continue

        dated_count += 1

        if parsed.strftime("%Y-%m") not in month_keys:
            outside_window_count += 1

    undated_count = count - dated_count

    active_months = [m for m in monthly if m["value"] > 0]
    average_monthly = (
        sum(m["value"] for m in active_months) / len(active_months)
        if active_months else 0.0
    )

    latest_month = monthly[-1] if monthly else None
    previous_month = monthly[-2] if len(monthly) > 1 else None

    mom_change = None
    if (
        latest_month
        and previous_month
        and previous_month["value"] > 0
    ):
        mom_change = (
            (latest_month["value"] - previous_month["value"])
            / previous_month["value"] * 100
        )

    peak_month = (
        max(monthly, key=lambda m: m["value"]) if active_months else None
    )

    max_month_value = max((m["value"] for m in monthly), default=0)

    # ---------------- categories ----------------
    category_totals = calculate_category_totals(transactions_list)

    category_rows = []

    for index, (name, value) in enumerate(
        sorted(category_totals.items(), key=lambda kv: kv[1], reverse=True)
    ):
        category_rows.append({
            "name": name,
            "value": value,
            "pct": (value / total * 100) if total > 0 else 0.0,
            "color": CHART_COLORS[index % len(CHART_COLORS)],
        })

    top_category = (
        category_rows[0] if category_rows and category_rows[0]["value"] > 0
        else None
    )

    # conic-gradient for the dashboard donut (real percentages)
    stops = []
    cursor = 0.0
    visible = [row for row in category_rows if row["value"] > 0]

    for position, row in enumerate(visible):
        end = (
            100.0 if position == len(visible) - 1
            else cursor + row["pct"]
        )
        stops.append(f"{row['color']} {cursor:.2f}% {end:.2f}%")
        cursor = end

    donut_gradient = (
        ", ".join(stops) if stops else "#eceef5 0% 100%"
    )

    # ---------------- merchants ----------------
    merchant_totals = {}

    for t, amount in zip(transactions_list, amounts):
        name = str(t.get("merchant") or "").strip()
        lowered = name.lower()

        if (
            lowered in _UNKNOWN_MERCHANTS
            or lowered.endswith((".pdf", ".png", ".jpg", ".jpeg"))
            or amount <= 0
        ):
            continue

        entry = merchant_totals.setdefault(
            lowered, {"name": name, "value": 0.0, "count": 0}
        )
        entry["value"] += amount
        entry["count"] += 1

    top_merchants = sorted(
        merchant_totals.values(),
        key=lambda m: m["value"],
        reverse=True,
    )[:5]

    for merchant in top_merchants:
        merchant["pct"] = (
            merchant["value"] / total * 100 if total > 0 else 0.0
        )

    # ---------------- recent ----------------
    recent = sorted(
        transactions_list,
        key=lambda t: parse_transaction_date(t) or datetime.min,
        reverse=True,
    )[:max(int(recent_limit or 5), 1)]

    # ---------------- budget ----------------
    budget = None
    monthly_budget = _to_float(settings.get("monthly_budget", 0))

    if monthly_budget > 0 and latest_month:
        spent = latest_month["value"]
        pct = spent / monthly_budget * 100

        budget = {
            "limit": monthly_budget,
            "spent": spent,
            "remaining": monthly_budget - spent,
            "pct": pct,
            "bar_pct": min(pct, 100),
            "month": latest_month["full_label"],
            "state": (
                "over" if pct >= 100
                else "warn" if pct >= 80
                else "ok"
            ),
        }

    return {
        "total": total,
        "count": count,
        "priced_count": len(priced),
        "unpriced_count": unpriced_count,
        "average": average,
        "highest": highest,
        "monthly": monthly,
        "max_month_value": max_month_value,
        "line_chart": build_line_chart(monthly),
        "dated_count": dated_count,
        "undated_count": undated_count,
        "outside_window_count": outside_window_count,
        "average_monthly": average_monthly,
        "active_month_count": len(active_months),
        "latest_month": latest_month,
        "previous_month": previous_month,
        "mom_change": mom_change,
        "peak_month": peak_month,
        "categories": category_rows,
        "top_category": top_category,
        "donut_gradient": donut_gradient,
        "top_merchants": top_merchants,
        "recent": recent,
        "budget": budget,
    }


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/")
def dashboard():
    all_transactions, aws_expenses = get_combined_expenses()

    settings = get_settings()

    # One summary feeds every number and chart on this page.
    summary = build_spending_summary(
        all_transactions,
        settings,
        recent_limit=settings["recent_count"],
    )

    return render_template(
        "dashboard.html",
        summary=summary,
        transactions=summary["recent"],
        total=summary["total"],
        categories=calculate_category_totals(all_transactions),
        invoice_count=summary["count"],
        transaction_count=summary["count"],
        aws_expenses=aws_expenses,
        api_url=API_URL
    )


# ============================================================
# UPLOAD INVOICE → AMAZON S3
# ============================================================

@app.route("/upload", methods=["GET", "POST"])
def upload_invoice():

    if request.method == "POST":

        print("\n" + "=" * 60)
        print("NEW INVOICE UPLOAD")
        print("=" * 60)

        # ----------------------------------------------------
        # GET FILE
        # ----------------------------------------------------

        file = request.files.get("invoice")

        if file is None:

            print(
                "ERROR: No file object received."
            )

            flash(
                "No file was received. Please choose an invoice."
            )

            return redirect(
                url_for("upload_invoice")
            )

        if file.filename == "":

            print(
                "ERROR: Empty filename."
            )

            flash(
                "Please choose an invoice file."
            )

            return redirect(
                url_for("upload_invoice")
            )

        # ----------------------------------------------------
        # SECURE ORIGINAL FILENAME
        # ----------------------------------------------------

        original_filename = secure_filename(
            file.filename
        )

        # ----------------------------------------------------
        # CREATE UNIQUE S3 OBJECT KEY
        # ----------------------------------------------------

        file_extension = os.path.splitext(
            original_filename
        )[1]

        unique_filename = (
            "invoices/"
            + uuid.uuid4().hex
            + file_extension
        )

        print(
            f"Original filename: "
            f"{original_filename}"
        )

        print(
            f"S3 object key: "
            f"{unique_filename}"
        )

        # ----------------------------------------------------
        # SAVE FILE LOCALLY
        # ----------------------------------------------------

        local_filename = (
            uuid.uuid4().hex
            + file_extension
        )

        local_path = os.path.join(
            app.config["UPLOAD_FOLDER"],
            local_filename
        )

        file.save(local_path)

        print(
            f"Local file saved: "
            f"{local_path}"
        )

        # ----------------------------------------------------
        # UPLOAD TO S3
        # ----------------------------------------------------

        try:

            print(
                "Uploading file to Amazon S3..."
            )

            s3.upload_file(
                local_path,
                S3_BUCKET,
                unique_filename
            )

            print(
                "SUCCESS: File uploaded to S3!"
            )

            print(
                f"Bucket: {S3_BUCKET}"
            )

            print(
                f"Object: {unique_filename}"
            )

            print("=" * 60)

            # ------------------------------------------------
            # EXTRACT TEXT LOCALLY (NO TEXTRACT REQUIRED)
            # ------------------------------------------------
            content_type = file.content_type or "application/octet-stream"

            invoice_record = build_uploaded_invoice_record(
                original_filename=original_filename,
                local_path=local_path,
                s3_key=unique_filename,
                content_type=content_type,
            )

            # Reuse the same category rules used by Analytics/Categories.
            if (
                not invoice_record.get("category")
                or invoice_record.get("category") == "Other"
            ):
                invoice_record["category"] = infer_category(
                    " ".join([
                        invoice_record.get("merchant", ""),
                        invoice_record.get("invoice_name", ""),
                        invoice_record.get("extracted_text", ""),
                    ])
                )

            # Keep the newest upload at the top.
            uploaded_invoices.insert(0, invoice_record)
            save_uploaded_invoice_cache()

            if invoice_record["extracted_text"]:
                print("SUCCESS: Invoice text extracted locally.")
                print(f"Merchant: {invoice_record['merchant']}")
                print(f"Invoice No: {invoice_record['invoice_number']}")
                print(f"Date: {invoice_record['date']}")
                print(f"Total: {invoice_record['amount']}")
            else:
                print(
                    "WARNING: No selectable text was found in the PDF. "
                    "This may be an image/scanned PDF."
                )

            flash(
                "Invoice uploaded to S3 and processed successfully."
            )

            return redirect(
                url_for(
                    "uploaded_invoice_detail",
                    index=0
                )
            )

        except ClientError as error:

            print(
                "\nS3 CLIENT ERROR:"
            )

            print(error)

            flash(
                "AWS rejected the upload. "
                "Check the terminal for details."
            )

            return redirect(
                url_for("upload_invoice")
            )

        except BotoCoreError as error:

            print(
                "\nBOTOCORE ERROR:"
            )

            print(error)

            flash(
                "Boto3 encountered an AWS error."
            )

            return redirect(
                url_for("upload_invoice")
            )

        except Exception as error:

            print(
                "\nUNEXPECTED ERROR:"
            )

            print(error)

            flash(
                "Unexpected upload error. "
                "Check the terminal."
            )

            return redirect(
                url_for("upload_invoice")
            )

    return render_template(
        "upload.html"
    )


# ============================================================
# INVOICE DISPLAY LIST
# ============================================================

def get_invoice_records_for_display():
    """
    Return the exact invoice list used by the Invoices page.

    Local parsed uploads are placed first so the newest upload is easy to
    find. Older records are fetched from API Gateway/DynamoDB and enriched
    from their S3 PDF when possible. Duplicate local/API records are removed.
    """
    api_expenses = get_expenses_from_api()
    display_records = []
    seen_keys = set()
    seen_names = set()

    for local_record in uploaded_invoices:
        record = normalize_transaction(local_record)
        display_records.append(record)

        key = record.get("s3_key") or ""
        name = record.get("invoice_name") or ""
        if key:
            seen_keys.add(key)
        if name:
            seen_names.add(name)

    for raw_record in api_expenses:
        record = enrich_api_invoice(raw_record)
        key = record.get("s3_key") or record.get("expense_id") or ""
        name = record.get("invoice_name") or ""

        if (key and key in seen_keys) or (name and name in seen_names):
            continue

        normalized = normalize_transaction(record)
        display_records.append(normalized)

        if key:
            seen_keys.add(key)
        if name:
            seen_names.add(name)

    return display_records


# ============================================================
# INVOICES
# ============================================================

@app.route("/invoices")
def invoices():
    category = request.args.get("category")

    invoice_records = get_invoice_records_for_display()

    if category:
        invoice_records = [
            record
            for record in invoice_records
            if record.get("category") == category
        ]

    visible_transactions = [
        transaction
        for transaction in transactions
        if not category or transaction["category"] == category
    ]

    return render_template(
        "invoices.html",
        transactions=visible_transactions,
        category=category,
        aws_expenses=invoice_records,
        api_url=API_URL
    )


# ============================================================
# ANALYTICS
# ============================================================

@app.route("/analytics")
def analytics():
    all_transactions, aws_expenses = get_combined_expenses()

    categories = calculate_category_totals(all_transactions)

    # Same summary builder as the Dashboard -> same numbers, same chart data.
    summary = build_spending_summary(
        all_transactions,
        get_settings(),
        recent_limit=8,
    )

    total = summary["total"]

    transaction_count = summary["count"]

    return render_template(
        "analytics.html",
        summary=summary,
        transactions=all_transactions,
        categories=categories,
        total=total,
        invoice_count=transaction_count,
        transaction_count=transaction_count,
        aws_expenses=aws_expenses
    )


@app.route("/categories")
def categories():
    all_transactions, aws_expenses = get_combined_expenses()

    category_totals = calculate_category_totals(all_transactions)

    return render_template(
        "categories.html",
        categories=category_totals,
        transactions=all_transactions,
        aws_expenses=aws_expenses
    )


# ============================================================
# SETTINGS
# ============================================================

@app.route("/settings", methods=["GET", "POST"])
def settings():

    if request.method == "POST":

        # "Reset to defaults" button
        if request.form.get("action") == "reset":

            if save_settings(dict(DEFAULT_SETTINGS)):
                flash("Settings were reset to their defaults.")
            else:
                flash("Could not reset settings (file could not be written).")

            return redirect(url_for("settings"))

        # Validate the budget first: never silently ignore bad input.
        budget_text = (
            request.form.get("monthly_budget", "") or ""
        ).strip().replace(",", "")

        try:
            budget_value = float(budget_text) if budget_text else 0.0
            if not 0 <= budget_value <= 1_000_000_000:
                raise ValueError("out of range")
        except ValueError:
            flash(
                "Monthly budget must be a number of 0 or more. "
                "Nothing was saved."
            )
            return redirect(url_for("settings"))

        new_settings = sanitize_settings({
            "name": request.form.get("name", ""),
            "currency": request.form.get("currency", ""),
            "notifications": request.form.get("notifications") == "on",
            "monthly_budget": budget_value,
            "recent_count": request.form.get("recent_count", 5),
        })

        if save_settings(new_settings):
            flash("Settings saved successfully.")
        else:
            flash(
                "Settings could not be saved because the settings "
                "file is not writable on this server."
            )

        return redirect(url_for("settings"))

    return render_template(
        "settings.html",

        settings=get_settings(),

        recent_count_choices=RECENT_COUNT_CHOICES
    )


# ============================================================
# UPLOADED INVOICE DETAILS
# ============================================================

@app.route("/invoice/uploaded/<int:index>")
def uploaded_invoice_detail(index):
    """Display an invoice processed by the local PDF parser."""

    if index < 0 or index >= len(uploaded_invoices):
        return "Uploaded invoice not found", 404

    invoice = uploaded_invoices[index]

    return render_template(
        "invoice_detail.html",
        invoice=invoice,
        is_aws=True,
        is_uploaded=True,
    )


# ============================================================
# AWS INVOICE DETAILS
# ============================================================

@app.route("/invoice/aws/<int:index>")
def aws_invoice_detail(index):
    """
    Show the same invoice record that was clicked on the Invoices page.

    The page contains both locally parsed uploads and API/DynamoDB records,
    so the detail route must use the same combined list instead of indexing
    the API response directly.
    """
    invoice_records = get_invoice_records_for_display()

    if index < 0 or index >= len(invoice_records):
        return "AWS Invoice not found", 404

    invoice = invoice_records[index]

    return render_template(
        "invoice_detail.html",
        invoice=invoice,
        is_aws=True,
        is_uploaded=invoice.get("source") != "aws_api"
    )


# ============================================================
# DEMO INVOICE DETAILS
# ============================================================

@app.route("/invoice/<int:index>")
def invoice_detail(index):

    if (
        index < 0
        or index >= len(transactions)
    ):

        return (
            "Invoice not found",
            404
        )

    return render_template(
        "invoice_detail.html",

        invoice=transactions[index],

        is_aws=False
    )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health")
def health():

    return {
        "status": "ok",
        "service": "SpendSense"
    }


# ============================================================
# START APPLICATION
# ============================================================

# Start loading the AWS records as soon as the app starts so the first
# page the user opens is usually already served from memory.
_start_api_refresh_if_needed()


if __name__ == "__main__":

    print("=" * 60)
    print("SpendSense is starting...")
    print("=" * 60)

    print(
        f"AWS Profile : {AWS_PROFILE}"
    )

    print(
        f"AWS Region  : {AWS_REGION}"
    )

    print(
        f"S3 Bucket   : {S3_BUCKET}"
    )

    print(
        f"API Endpoint: {API_URL}"
    )

    print(
        "Dashboard   : http://127.0.0.1:5000"
    )

    print("=" * 60)

    # Render provides the PORT environment variable.
    # Locally it defaults to 5000.

    port = int(
        os.getenv("PORT", 5000)
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )