import requests

url = "https://api.matchbook.com/edge/rest/lookups/sports?offset=0&per-page=20&order=name%20asc&status=active"

headers = {
    "accept": "application/json",
    "User-Agent": "api-doc-test-client"
}

response = requests.get(url, headers=headers)

print(response.text)