from flask import Flask, render_template, request, redirect, url_for, flash
from werkzeug.utils import secure_filename
from botocore.exceptions import BotoCoreError, ClientError
import boto3
import os
import uuid
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

app = Flask(__name__)
app.secret_key = "spendsense-dev-key"

# ============================================================
# CONFIGURATION
# ============================================================

UPLOAD_FOLDER = os.path.join(
    app.root_path,
    "static",
    "uploads"
)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

AWS_PROFILE = os.getenv("AWS_PROFILE", "spendsense")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
S3_BUCKET = os.getenv(
    "S3_BUCKET",
    "spendsense-invoices-megh-2026"
)
API_URL = "https://8ngwlz4334.execute-api.ap-south-1.amazonaws.com/expenses"

# Local development:
# Use the existing "spendsense" AWS profile.
#
# Cloud deployment:
# Set AWS_PROFILE to an empty value so boto3
# uses the deployment environment credentials.
AWS_PROFILE = os.getenv("AWS_PROFILE", "spendsense")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
S3_BUCKET = os.getenv(
    "S3_BUCKET",
    "spendsense-invoices-megh-2026"
)

# Render: use AWS credentials supplied through environment variables.
if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"):
    aws_session = boto3.Session(
        region_name=AWS_REGION
    )
else:
    aws_session = boto3.Session(
        profile_name=AWS_PROFILE,
        region_name=AWS_REGION
    )

s3 = aws_session.client(
    "s3",
    region_name=AWS_REGION
)


# ============================================================
# API GATEWAY → DYNAMODB
# ============================================================

def get_expenses_from_api():
    """
    Fetch the real invoice records from:
    API Gateway → Lambda → DynamoDB.

    The app continues working with the demo transactions if the
    API is temporarily unavailable.
    """
    try:
        request = Request(
            API_URL,
            headers={"Accept": "application/json"},
            method="GET"
        )

        with urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

        if data.get("success"):
            return data.get("expenses", [])

        print("API returned an unsuccessful response:", data)
        return []

    except HTTPError as error:
        print(f"API HTTP error: {error.code} - {error.reason}")
        return []

    except URLError as error:
        print(f"API connection error: {error.reason}")
        return []

    except Exception as error:
        print(f"Unexpected API error: {error}")
        return []


# Make the API URL available to templates for the next frontend step.
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
# DASHBOARD
# ============================================================

@app.route("/")
def dashboard():

    # Real records coming from API Gateway → Lambda → DynamoDB.
    aws_expenses = get_expenses_from_api()

    total = sum(
        t["amount"]
        for t in transactions
    )

    categories = {}

    for t in transactions:

        category = t["category"]

        categories[category] = (
            categories.get(category, 0)
            + t["amount"]
        )

    return render_template(
        "dashboard.html",
        transactions=transactions[:5],
        total=total,
        categories=categories,
        invoice_count=len(aws_expenses) if aws_expenses else len(transactions),
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

        # Get uploaded file
        file = request.files.get("invoice")

        # Check file
        if file is None:

            print("ERROR: No file object received.")

            flash(
                "No file was received. Please choose an invoice."
            )

            return redirect(
                url_for("upload_invoice")
            )

        if file.filename == "":

            print("ERROR: Empty filename.")

            flash(
                "Please choose an invoice file."
            )

            return redirect(
                url_for("upload_invoice")
            )

        # Make filename safe
        original_filename = secure_filename(
            file.filename
        )

        # Add a unique ID so files never overwrite each other
        file_extension = os.path.splitext(
            original_filename
        )[1]

        unique_filename = (
            "invoices/"
            + uuid.uuid4().hex
            + file_extension
        )

        print(
            f"Original filename: {original_filename}"
        )

        print(
            f"S3 object key: {unique_filename}"
        )

        # ====================================================
        # SAVE LOCALLY
        # ====================================================

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
            f"Local file saved: {local_path}"
        )

        # ====================================================
        # UPLOAD TO S3
        # ====================================================

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

            flash(
                "Invoice uploaded successfully to Amazon S3."
            )

            return render_template(
                "processing.html",
                filename=original_filename
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

    # Real invoice records from API Gateway → Lambda → DynamoDB.
    aws_expenses = get_expenses_from_api()

    visible = [
        t
        for t in transactions
        if (
            not category
            or t["category"] == category
        )
    ]

    return render_template(
        "invoices.html",
        transactions=visible,
        category=category,
        aws_expenses=aws_expenses,
        api_url=API_URL
    )


# ============================================================
# ANALYTICS
# ============================================================

@app.route("/analytics")
def analytics():

    categories = {}

    for t in transactions:

        category = t["category"]

        categories[category] = (
            categories.get(category, 0)
            + t["amount"]
        )

    return render_template(
        "analytics.html",
        transactions=transactions,
        categories=categories
    )


# ============================================================
# CATEGORIES
# ============================================================

@app.route("/categories")
def categories():

    category_totals = {}

    for t in transactions:

        category = t["category"]

        category_totals[category] = (
            category_totals.get(category, 0)
            + t["amount"]
        )

    return render_template(
        "categories.html",
        categories=category_totals
    )


# ============================================================
# API GATEWAY → DYNAMODB
# ============================================================

def get_expenses_from_api():
    """
    Fetch the real invoice records from:
    API Gateway → Lambda → DynamoDB.

    The app continues working with the demo transactions if the
    API is temporarily unavailable.
    """
    try:
        request = Request(
            API_URL,
            headers={"Accept": "application/json"},
            method="GET"
        )

        with urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

        if data.get("success"):
            return data.get("expenses", [])

        print("API returned an unsuccessful response:", data)
        return []

    except HTTPError as error:
        print(f"API HTTP error: {error.code} - {error.reason}")
        return []

    except URLError as error:
        print(f"API connection error: {error.reason}")
        return []

    except Exception as error:
        print(f"Unexpected API error: {error}")
        return []


# Make the API URL available to templates for the next frontend step.
@app.context_processor
def inject_api_config():
    return {
        "api_url": API_URL
    }


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
            "/settings"
        )

    return render_template(
        "settings.html",
        settings=settings_data
    )


# ============================================================
# INVOICE DETAILS
# ============================================================

@app.route("/invoice/<int:index>")
def invoice_detail(index):

    if (
        index < 0
        or index >= len(transactions)
    ):
        return "Invoice not found", 404

    return render_template(
        "invoice_detail.html",
        invoice=transactions[index]
    )


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

    app.run(
        debug=True
    )