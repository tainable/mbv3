import requests


def get_specific_markets(query):
    url = "https://gamma-api.polymarket.com/markets"
    params = {
        "active": "true",
        "closed": "false",
        "search": query,  # This filters on the server!
        "limit": 100
    }

    response = requests.get(url, params=params)
    if response.status_code == 200:
        return response.json()
    return []


# Example: only get markets containing "nba"
nba_markets = get_specific_markets("Lakers")

for market in nba_markets:
    print(f"Slug: {market['slug']}")
    print(f"Question: {market['question']}\n")