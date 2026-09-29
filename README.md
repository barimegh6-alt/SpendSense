# SpendSense — Smart Invoice & Expense Intelligence

Mini-project built with Flask as the development layer and designed for AWS integration.

## Current local prototype
- Flask web application
- SpendSense dashboard UI
- Invoice upload page with local file saving
- Working Categories and Settings pages
- Working camera/file picker action
- Processing workflow mockup
- Invoice details
- Invoice history
- Analytics page
- Responsive styling

## Planned AWS architecture
Amazon S3 → AWS Lambda → Amazon Textract → Amazon DynamoDB → Amazon API Gateway

Optional analytics layer: Amazon QuickSight.

## Run locally
```bash
python -m venv venv
# Windows:
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000
