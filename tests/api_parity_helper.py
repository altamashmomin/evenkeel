"""Collect canonical API responses for one code version of Ledger.

Internal helper for test_api_parity: runs in its own process because two
versions of app.py cannot share one interpreter (module cache), prints a
JSON map of endpoint -> {status, body} on stdout.

Usage: python api_parity_helper.py <code_dir> <db_path>
"""
import importlib.util
import json
import os
import sqlite3
import sys


def main():
    code_dir, db_path = sys.argv[1], sys.argv[2]
    os.environ["DATABASE_PATH"] = os.path.abspath(db_path)
    os.environ.setdefault("SECRET_KEY", "parity-test-secret")
    sys.path.insert(0, code_dir)
    spec = importlib.util.spec_from_file_location(
        "app_under_test", os.path.join(code_dir, "app.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    conn = sqlite3.connect(db_path)
    months = [row[0] for row in conn.execute(
        "SELECT DISTINCT substr(txn_date, 1, 7) FROM transactions ORDER BY 1")]
    conn.close()

    module.app.config["TESTING"] = True
    client = module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = 1

    endpoints = ["/api/balance", "/api/me", "/api/goals", "/api/status",
                 "/api/categories"]
    for month in months:
        endpoints.append(f"/api/bills?period={month}")
        endpoints.append(f"/api/dashboard?month={month}")
        endpoints.append(f"/api/transactions?month={month}")

    responses = {}
    for endpoint in endpoints:
        result = client.get(endpoint)
        responses[endpoint] = {
            "status": result.status_code,
            "body": result.get_data(as_text=True),
        }

    # Unauthenticated parity: the 401 refusal body and the sessionless
    # /api/status shape are deployed API surface too.
    anonymous = module.app.test_client()
    for endpoint in ("/api/status", "/api/transactions", "/api/balance"):
        result = anonymous.get(endpoint)
        responses[f"unauth:{endpoint}"] = {
            "status": result.status_code,
            "body": result.get_data(as_text=True),
        }
    print(json.dumps(responses, sort_keys=True))


if __name__ == "__main__":
    main()
