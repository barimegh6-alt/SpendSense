from flask import Flask, render_template, request, redirect, url_for, flash
from werkzeug.utils import secure_filename
from botocore.exceptions import BotoCoreError, ClientError
import boto3
import os
import uuid
import json
import re
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

AWS_PROFILE = os.getenv("AWS_PROFILE", "spendsense")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")

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

    # Local development:
    # Use the existing AWS CLI profile.
    aws_session = boto3.Session(
        profile_name=AWS_PROFILE,
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

    # Total amount. Prefer lines containing total/grand total/amount due.
    total_patterns = [
        r"(?:grand\s+total|total\s+amount|amount\s+due|net\s+total|total)\s*[:\-]?\s*(?:₹|rs\.?|inr|\$)?\s*([\d,]+(?:\.\d{1,2})?)",
    ]

    for pattern in total_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            # Usually the final total is the most useful match.
            amount = _clean_money(matches[-1])
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
# DASHBOARD
# ============================================================

@app.route("/")
def dashboard():
    aws_expenses = get_expenses_from_api()

    # Start with the existing demo transactions.
    all_transactions = list(transactions)

    # Convert AWS records into dashboard-compatible transactions
    # only when useful financial fields are available.
    for invoice in aws_expenses:
        amount = invoice.get("amount", invoice.get("total", 0))

        try:
            amount = float(amount or 0)
        except (TypeError, ValueError):
            amount = 0.0

        category = invoice.get("category", "Uncategorized")

        merchant = (
            invoice.get("merchant")
            or invoice.get("invoice_name")
            or "AWS Invoice"
        )

        date = (
            invoice.get("date")
            or invoice.get("uploaded_at")
            or "Processed"
        )

        all_transactions.append({
            "date": date,
            "merchant": merchant,
            "category": category,
            "amount": amount,
            "status": invoice.get("status", "Processed")
        })

    # Total financial spending
    total = sum(
        t.get("amount", 0)
        for t in all_transactions
    )

    # Category totals
    categories = {}

    for t in all_transactions:
        category = t.get("category", "Uncategorized")

        try:
            amount = float(t.get("amount", 0) or 0)
        except (TypeError, ValueError):
            amount = 0.0

        categories[category] = (
            categories.get(category, 0) + amount
        )

    # Number of actual processed AWS invoices
    invoice_count = len(aws_expenses)

    # If there are no AWS invoices yet, retain demo count
    if invoice_count == 0:
        invoice_count = len(transactions)

    return render_template(
        "dashboard.html",
        transactions=all_transactions[:5],
        total=total,
        categories=categories,
        invoice_count=invoice_count,
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

            # Keep the newest upload at the top.
            uploaded_invoices.insert(0, invoice_record)

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
# INVOICES
# ============================================================

@app.route("/invoices")
def invoices():

    category = request.args.get(
        "category"
    )

    api_expenses = get_expenses_from_api()

    # Keep API/DynamoDB records intact and place newly uploaded
    # locally-extracted invoices above them.
    aws_expenses = uploaded_invoices + api_expenses

    visible_transactions = [

        transaction

        for transaction in transactions

        if (
            not category
            or transaction["category"] == category
        )

    ]

    return render_template(
        "invoices.html",

        transactions=visible_transactions,

        category=category,

        aws_expenses=aws_expenses,

        api_url=API_URL
    )


# ============================================================
# ANALYTICS
# ============================================================

@app.route("/analytics")
def analytics():
    aws_expenses = get_expenses_from_api()

    all_transactions = list(transactions)

    for invoice in aws_expenses:
        amount = invoice.get(
            "amount",
            invoice.get("total", 0)
        )

        try:
            amount = float(amount or 0)
        except (TypeError, ValueError):
            amount = 0.0

        category = invoice.get(
            "category",
            "Uncategorized"
        )

        merchant = (
            invoice.get("merchant")
            or invoice.get("invoice_name")
            or "AWS Invoice"
        )

        date = (
            invoice.get("date")
            or invoice.get("uploaded_at")
            or "Processed"
        )

        all_transactions.append({
            "date": date,
            "merchant": merchant,
            "category": category,
            "amount": amount,
            "status": invoice.get(
                "status",
                "Processed"
            )
        })

    categories = {}

    for transaction in all_transactions:
        category = transaction.get(
            "category",
            "Uncategorized"
        )

        try:
            amount = float(
                transaction.get("amount", 0) or 0
            )
        except (TypeError, ValueError):
            amount = 0.0

        categories[category] = (
            categories.get(category, 0)
            + amount
        )

    total = sum(
        transaction.get("amount", 0)
        for transaction in all_transactions
    )

    invoice_count = len(aws_expenses)

    if invoice_count == 0:
        invoice_count = len(transactions)

    return render_template(
        "analytics.html",
        transactions=all_transactions,
        categories=categories,
        total=total,
        invoice_count=invoice_count,
        aws_expenses=aws_expenses
    )

# ============================================================
# CATEGORIES
# ============================================================

@app.route("/categories")
def categories():
    aws_expenses = get_expenses_from_api()

    all_transactions = list(transactions)

    for invoice in aws_expenses:
        amount = invoice.get(
            "amount",
            invoice.get("total", 0)
        )

        try:
            amount = float(amount or 0)
        except (TypeError, ValueError):
            amount = 0.0

        category = invoice.get(
            "category",
            "Uncategorized"
        )

        merchant = (
            invoice.get("merchant")
            or invoice.get("invoice_name")
            or "AWS Invoice"
        )

        date = (
            invoice.get("date")
            or invoice.get("uploaded_at")
            or "Processed"
        )

        all_transactions.append({
            "date": date,
            "merchant": merchant,
            "category": category,
            "amount": amount,
            "status": invoice.get(
                "status",
                "Processed"
            )
        })

    category_totals = {}

    for transaction in all_transactions:
        category = transaction.get(
            "category",
            "Uncategorized"
        )

        try:
            amount = float(
                transaction.get("amount", 0) or 0
            )
        except (TypeError, ValueError):
            amount = 0.0

        category_totals[category] = (
            category_totals.get(category, 0)
            + amount
        )

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
    Show an invoice returned from:

    API Gateway → Lambda → DynamoDB
    """

    aws_expenses = get_expenses_from_api()

    if (
        index < 0
        or index >= len(aws_expenses)
    ):

        return (
            "AWS Invoice not found",
            404
        )

    invoice = aws_expenses[index]

    return render_template(
        "invoice_detail.html",

        invoice=invoice,

        is_aws=True
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