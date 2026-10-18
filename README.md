# Acme Appliances Ledger Agent

## Render setup
- Runtime: Python 3
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Environment variable `LEDGER_EXPORT_URL`: paste the full task-specific `/export` URL from the initial `questionData` response.

The export is newline-delimited JSON (NDJSON). The service caches the export on first request, deduplicates order IDs by latest `updated_at`, interprets business dates in Asia/Kolkata, and converts to USD using USD=1, EUR=1.06, INR=0.01223.

## Test
Send `POST /` with JSON `{"question":"What was the total revenue in USD from the North region in March 2026?"}`. Response shape: `{"answer": 123.45}`.

This is a lightweight rule-based question parser for common questions about revenue, refunds, products, regions, customer IDs, dates, order counts, and quantities. Test against the grader and expand parsing if it asks unsupported formulations.
