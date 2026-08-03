import jwt
import requests
import datetime

SECRET_KEY = "ctn-dev-secret-change-in-production-2024"
payload = {
    "sub": str(2),
    "email": "demo@installer.ctn",
    "role": "installer",
    "exp": datetime.datetime.utcnow() + datetime.timedelta(days=7)
}
token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
cookies = {"session_token": token}
r = requests.get("https://ctn-api-railway-production.up.railway.app/api/marketplace/listings", cookies=cookies)
print(r.status_code)
print(r.text)
