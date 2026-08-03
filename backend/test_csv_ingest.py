import requests
import time

BASE_URL = "http://127.0.0.1:8000"

def wait_for_server():
    for _ in range(10):
        try:
            requests.get(BASE_URL)
            return
        except requests.exceptions.ConnectionError:
            time.sleep(1)
    print("Server not running")
    exit(1)

wait_for_server()

# Login as admin
print("Logging in as admin...")
resp = requests.post(f"{BASE_URL}/api/auth/login", json={
    "email": "admin@ctn.org",
    "password": "ctn-admin-2024"
})
print("Login:", resp.status_code, resp.text)
token = resp.cookies.get("ctn_session")
cookies = {"ctn_session": token}

# 1. Register device
print("Registering device...")
resp = requests.post(f"{BASE_URL}/api/admin/devices", json={
    "device_id": "DEVICE-TEST-01",
    "owner_email": "demo@installer.ctn",
    "location": "India"
}, cookies=cookies)
print(resp.json())

# 2. Upload CSV
print("Uploading CSV...")
csv_content = """device_id,timestamp,delta_kwh
DEVICE-TEST-01,2026-01-01 06:00:00,500.0
DEVICE-TEST-01,2026-01-01 06:15:00,500.0
DEVICE-TEST-01,2026-01-01 06:30:00,500.0
DEVICE-TEST-01,2026-01-01 06:45:00,500.0
"""
files = {'file': ('test.csv', csv_content, 'text/csv')}
resp = requests.post(f"{BASE_URL}/api/admin/ingest-csv", files=files, cookies=cookies)
print(resp.json())
