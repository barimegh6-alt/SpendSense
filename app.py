from flask import Flask, render_template, request, redirect, url_for, flash
from werkzeug.utils import secure_filename
from botocore.exceptions import BotoCoreError, ClientError
import boto3
import os
import uuid
import json
import re
import tempfile
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


# ============================================================
# API GATEWAY → LAMBDA → DYNAMODB
# ============================================================

def get_expenses_from_api():
    """
    Fetch invoice/expense records from:

    API Gateway → Lambda → DynamoDB

    Returns an empty list if the API is unavailable.
    """

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

        if data.get("success"):

            return data.get("expenses", [])

        print(
            "API returned an unsuccessful response:",
            data
        )

        return []

    except HTTPError as error:

        print(
            f"API HTTP error: "
            f"{error.code} - {error.reason}"
        )

        return []

    except URLError as error:

        print(
            f"API connection error: "
            f"{error.reason}"
        )

        return []

    except Exception as error:

        print(
            f"Unexpected API error: {error}"
        )

        return []


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

settings_data = {

    "name": "Ananya",

    "currency": "INR (₹)",

    "notifications": True
}


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
    Best-effort extraction for ordinary text PDFs.

    We deliberately keep this conservative. If a field cannot be
    confidently found, it is shown as 'Not detected' rather than
    inventing invoice data.
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

    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in text.splitlines()
        if line.strip()
    ]

    # Invoice number
    invoice_patterns = [
        r"(?:invoice\s*(?:no|number|#)?|bill\s*(?:no|number|#)?)\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9./_-]*)",
    ]
    for pattern in invoice_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            result["invoice_number"] = match.group(1).strip()
            break

    # Date
    date_patterns = [
        r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b",
        r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})\b",
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{2,4})\b",
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            result["date"] = match.group(1).strip()
            break

    # Total amount. Invoice PDFs often use formats such as:
    #   Total: ₹1,123.50
    #   Total .... ₹1,123.50
    #   GRAND TOTAL     1,123.50
    #   Amount Due: Rs. 1,123.50
    #
    # We inspect the complete line rather than requiring the number
    # to appear immediately after the word "total".
    total_keywords = (
        "grand total",
        "total amount",
        "amount due",
        "net total",
        "balance due",
        "total",
    )

    for line in lines:
        low_line = line.lower()
        if not any(keyword in low_line for keyword in total_keywords):
            continue

        amount_matches = re.findall(
            r"(?:₹|rs\.?|inr|\$)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
            line,
            re.IGNORECASE,
        )

        if amount_matches:
            # The last number on a total line is normally the final bill total.
            amount = _clean_money(amount_matches[-1])
            if amount is not None:
                result["amount"] = amount
                break

    # Merchant: first plausible heading before invoice metadata.
    merchant_candidates = []
    for line in lines[:12]:
        low = line.lower()
        if any(word in low for word in [
            "invoice", "bill", "date", "gst", "tax", "phone",
            "email", "address", "total", "amount", "receipt"
        ]):
            continue
        if re.search(r"\d{6,}", line):
            continue
        if 2 <= len(line) <= 80:
            merchant_candidates.append(line)

    if merchant_candidates:
        result["merchant"] = merchant_candidates[0]

    # Basic category inference from extracted words.
    low_text = text.lower()
    if any(x in low_text for x in ["restaurant", "cafe", "food", "meal", "dining", "pizza", "burger"]):
        result["category"] = "Food & Dining"
    elif any(x in low_text for x in ["uber", "ola", "taxi", "transport", "cab"]):
        result["category"] = "Travel"
    elif any(x in low_text for x in ["amazon", "shopping", "store", "retail"]):
        result["category"] = "Shopping"
    elif any(x in low_text for x in ["grocery", "groceries", "supermarket"]):
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


def enrich_api_invoice(record):
    """
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

        s3.download_file(
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


def calculate_category_totals(all_transactions):
    totals = {}

    for transaction in all_transactions:
        category = transaction.get("category") or "Other"
        amount = _to_float(transaction.get("amount", 0))
        totals[category] = totals.get(category, 0) + amount

    return totals


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/")
def dashboard():
    all_transactions, aws_expenses = get_combined_expenses()

    # --------------------------------------------------------
    # TOTAL SPENDING
    # --------------------------------------------------------
    total = sum(
        _to_float(t.get("amount", 0))
        for t in all_transactions
    )

    # --------------------------------------------------------
    # CATEGORY TOTALS
    # --------------------------------------------------------
    categories = calculate_category_totals(all_transactions)

    # --------------------------------------------------------
    # TRANSACTION COUNT
    # --------------------------------------------------------
    transaction_count = len(all_transactions)

    # --------------------------------------------------------
    # RECENT TRANSACTIONS
    #
    # The old code used all_transactions[:5].
    # Since demo transactions are stored first, that caused
    # the dashboard to always show The Urban Cafe, Swiggy, etc.
    #
    # We now sort by the invoice date/upload date so that newly
    # uploaded invoices appear at the top.
    # --------------------------------------------------------

    def transaction_sort_key(transaction):
        date_value = (
            transaction.get("date")
            or transaction.get("uploaded_at")
            or ""
        )

        if not date_value:
            return datetime.min

        date_text = str(date_value).strip()

        # ISO date/time from AWS
        try:
            return datetime.fromisoformat(
                date_text.replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except Exception:
            pass

        # Demo dates such as "21 Sep 2026"
        for fmt in [
            "%d %b %Y",
            "%d %B %Y",
            "%Y-%m-%d"
        ]:
            try:
                return datetime.strptime(
                    date_text,
                    fmt
                )
            except Exception:
                pass

        return datetime.min

    recent_transactions = sorted(
        all_transactions,
        key=transaction_sort_key,
        reverse=True
    )[:5]

    # --------------------------------------------------------
    # DASHBOARD
    # --------------------------------------------------------

    return render_template(
        "dashboard.html",
        transactions=recent_transactions,
        total=total,
        categories=categories,
        invoice_count=transaction_count,
        transaction_count=transaction_count,
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

    total = sum(
        _to_float(t.get("amount", 0))
        for t in all_transactions
    )

    transaction_count = len(all_transactions)

    return render_template(
        "analytics.html",
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

        settings_data["name"] = (

            request.form.get(
                "name",
                "Ananya"
            ).strip()

            or "Ananya"

        )

        settings_data["currency"] = (
            request.form.get(
                "currency",
                "INR (₹)"
            )
        )

        settings_data["notifications"] = (
            request.form.get(
                "notifications"
            ) == "on"
        )

        flash(
            "Settings saved successfully."
        )

        return redirect(
            url_for("settings")
        )

    return render_template(
        "settings.html",

        settings=settings_data
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