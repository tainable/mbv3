import requests


def get_azuro_leagues():
    # 2026 PRODUCTION ENDPOINT: Azuro Data Feed (Polygon)
    # This mirror is maintained by Azuro to avoid the Graph Gateway API key requirement
    url = "https://thegraph.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-data-feed-polygon/graphql"

    # Updated Query: Notice we use 'leagues' with the 'hasActiveGames' filter
    query = """
    {
      leagues(first: 1000, where: { hasActiveGames: true }) {
        name
        slug
        sport {
          name
        }
        country {
          name
        }
      }
    }
    """

    try:
        response = requests.post(url, json={'query': query}, timeout=15)
        response.raise_for_status()

        data = response.json()

        # Check for GraphQL specific errors (even if status is 200)
        if 'errors' in data:
            return f"GraphQL Error: {data['errors'][0]['message']}"

        leagues = data.get('data', {}).get('leagues', [])

        formatted_leagues = []
        for l in leagues:
            formatted_leagues.append({
                "league": l['name'],
                "sport": l['sport']['name'],
                "country": l['country']['name'],
                "slug": l['slug']
            })

        return formatted_leagues

    except requests.exceptions.RequestException as e:
        return f"Network Connection Error: {e}"


# Execute
leagues_list = get_azuro_leagues()

if isinstance(leagues_list, list):
    print(f"✅ Successfully pulled {len(leagues_list)} active leagues.")
    for l in leagues_list[:10]:
        print(f" - [{l['sport']}] {l['league']} ({l['country']})")
else:
    print(leagues_list)