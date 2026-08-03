import requests
import json
url = "https://ivory-geographical-lungfish-400.mypinata.cloud/ipfs/bafybeifpn7y2r2rsjtvm4hun3dy63jkp5ah7qxfwhk5u6bemkapmis2qku"
data = requests.get(url, timeout=30).json()
credits = data if isinstance(data, list) else data.get("credits", [])
c2121 = next((c for c in credits if c.get("credit_id") == 2121), None)
c2122 = next((c for c in credits if c.get("credit_id") == 2122), None)
print(json.dumps(c2121, indent=2))
print(json.dumps(c2122, indent=2))

from data_utils import parse_discrete_credits
parsed = parse_discrete_credits(credits)
p2121 = next((c for c in parsed if c.get("credit_id") == 2121), None)
p2122 = next((c for c in parsed if c.get("credit_id") == 2122), None)
print("Parsed 2121:", json.dumps(p2121, indent=2))
print("Parsed 2122:", json.dumps(p2122, indent=2))
