import requests
url = "https://ivory-geographical-lungfish-400.mypinata.cloud/ipfs/bafybeifpn7y2r2rsjtvm4hun3dy63jkp5ah7qxfwhk5u6bemkapmis2qku"
r = requests.get(url, timeout=30)
data = r.json()
credits = data if isinstance(data, list) else data.get("credits", [])
print(f"Total raw credits: {len(credits)}")
if credits:
    print(f"First ID: {credits[0].get('credit_id')}")
    print(f"Last ID: {credits[-1].get('credit_id')}")

# Now let's see what parse_discrete_credits does
from data_utils import parse_discrete_credits
parsed = parse_discrete_credits(credits)
print(f"Total parsed credits: {len(parsed)}")
if parsed:
    print(f"Parsed First ID: {parsed[0].get('credit_id')}")
    print(f"Parsed Last ID: {parsed[-1].get('credit_id')}")
