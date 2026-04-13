from __future__ import annotations

import re


TEAM_ALIASES = {
    "nba": {
        "okc thunder": "oklahoma city thunder",
        "la clippers": "los angeles clippers",
        "la lakers": "los angeles lakers",
        "l a lakers": "los angeles lakers",
        "ny knicks": "new york knicks",
        "phx suns": "phoenix suns",
        "gs warriors": "golden state warriors",
        "sixers": "philadelphia 76ers",
        "cavs": "cleveland cavaliers",
        "cavaliers": "cleveland cavaliers",
        "pels": "new orleans pelicans",
        "pelicans": "new orleans pelicans",
        "wolves": "minnesota timberwolves",
        "blazers": "portland trail blazers",
        "trail blazers": "portland trail blazers",
        "hawks": "atlanta hawks",
        "celtics": "boston celtics",
        "nets": "brooklyn nets",
        "hornets": "charlotte hornets",
        "bulls": "chicago bulls",
        "mavericks": "dallas mavericks",
        "nuggets": "denver nuggets",
        "pistons": "detroit pistons",
        "warriors": "golden state warriors",
        "rockets": "houston rockets",
        "pacers": "indiana pacers",
        "clippers": "los angeles clippers",
        "lakers": "los angeles lakers",
        "grizzlies": "memphis grizzlies",
        "heat": "miami heat",
        "bucks": "milwaukee bucks",
        "timberwolves": "minnesota timberwolves",
        "knicks": "new york knicks",
        "magic": "orlando magic",
        "76ers": "philadelphia 76ers",
        "suns": "phoenix suns",
        "kings": "sacramento kings",
        "spurs": "san antonio spurs",
        "thunder": "oklahoma city thunder",
        "raptors": "toronto raptors",
        "jazz": "utah jazz",
        "wizards": "washington wizards",
    },
    "ucl": {
        # Bayern Munich — Matchbook encodes ü as space giving "m nchen"
        "bayern": "bayern munich",
        "fc bayern munich": "bayern munich",
        "fc bayern m nchen": "bayern munich",
        "fc bayern munchen": "bayern munich",
        # PSG
        "psg": "paris saint germain",
        "paris saint-germain": "paris saint germain",
        "paris saint germain fc": "paris saint germain",
        # Sporting CP
        "sporting": "sporting clube de portugal cp",
        "sporting cp": "sporting clube de portugal cp",
        "sporting clube de portugal": "sporting clube de portugal cp",
        # Atletico Madrid — é strips to space giving "atl tico"
        "atletico madrid": "atletico de madrid",
        "atletico": "atletico de madrid",
        "atl tico de madrid": "atletico de madrid",
        "atl tico madrid": "atletico de madrid",
        "club atl tico de madrid": "atletico de madrid",
        "club atletico de madrid": "atletico de madrid",
        # Barcelona
        "barca": "fc barcelona",
        "barcelona": "fc barcelona",
        "barcelona fc": "fc barcelona",
        # Arsenal
        "arsenal fc": "arsenal",
        # Real Madrid
        "real madrid cf": "real madrid",
        # Liverpool
        "liverpool fc": "liverpool",
        # Other common UCL teams
        "man city": "manchester city",
        "inter": "inter milan",
        "ac milan": "ac milan",
        "milan": "ac milan",
    },
    "epl": {
        # NOTE: keys must be in already-normalized form (lowercase, special chars → space,
        # no double spaces) because normalize_team_name() normalizes before alias lookup.
        # e.g. "Brighton & Hove Albion FC" → "brighton hove albion fc" (& stripped to space)

        # Arsenal
        "arsenal fc": "arsenal",
        # Chelsea
        "chelsea fc": "chelsea",
        # Liverpool
        "liverpool fc": "liverpool",
        # Manchester City
        "man city": "manchester city",
        "manchester city fc": "manchester city",
        # Manchester United
        "man utd": "manchester united",
        "man united": "manchester united",
        "manchester utd": "manchester united",
        "manchester united fc": "manchester united",
        # Tottenham
        "tottenham hotspur": "tottenham",
        "tottenham hotspur fc": "tottenham",
        "spurs": "tottenham",
        # Newcastle
        "newcastle united": "newcastle",
        "newcastle utd": "newcastle",
        "newcastle united fc": "newcastle",
        # Aston Villa
        "aston villa fc": "aston villa",
        # Brighton — & stripped to space by normalization
        "brighton hove albion": "brighton",
        "brighton hove albion fc": "brighton",
        "brighton and hove albion": "brighton",
        # West Ham
        "west ham united": "west ham",
        "west ham utd": "west ham",
        "west ham united fc": "west ham",
        # Wolves
        "wolverhampton wanderers": "wolves",
        "wolverhampton wanderers fc": "wolves",
        "wolverhampton": "wolves",
        # Crystal Palace
        "crystal palace fc": "crystal palace",
        # Fulham
        "fulham fc": "fulham",
        # Brentford
        "brentford fc": "brentford",
        # Everton
        "everton fc": "everton",
        # Bournemouth
        "afc bournemouth": "bournemouth",
        "bournemouth fc": "bournemouth",
        # Nottingham Forest
        "nottingham forest fc": "nottingham forest",
        "nottm forest": "nottingham forest",
        "nott m forest": "nottingham forest",   # "Nott'm Forest" → ' stripped to space
        "notts forest": "nottingham forest",
        # Leeds United
        "leeds united": "leeds",
        "leeds united fc": "leeds",
        # Burnley
        "burnley fc": "burnley",
        # Sunderland
        "sunderland afc": "sunderland",
        "sunderland fc": "sunderland",
    },
    "mlb": {
        "d backs": "arizona diamondbacks",
        "diamondbacks": "arizona diamondbacks",
        "white sox": "chicago white sox",
        "cubs": "chicago cubs",
        "guardians": "cleveland guardians",
        "reds": "cincinnati reds",
        "rockies": "colorado rockies",
        "tigers": "detroit tigers",
        "astros": "houston astros",
        "royals": "kansas city royals",
        "angels": "los angeles angels",
        "dodgers": "los angeles dodgers",
        "marlins": "miami marlins",
        "brewers": "milwaukee brewers",
        "twins": "minnesota twins",
        "mets": "new york mets",
        "yankees": "new york yankees",
        "athletics": "oakland athletics",
        "a's": "oakland athletics",
        "phillies": "philadelphia phillies",
        "pirates": "pittsburgh pirates",
        "padres": "san diego padres",
        "giants": "san francisco giants",
        "mariners": "seattle mariners",
        "rays": "tampa bay rays",
        "rangers": "texas rangers",
        "blue jays": "toronto blue jays",
        "nationals": "washington nationals",
        "red sox": "boston red sox",
        "braves": "atlanta braves",
        "orioles": "baltimore orioles",
        "cardinals": "st louis cardinals",
    },
}


def normalize_team_name(team_name: str, league: str) -> str:
    normalized = re.sub(r"[^a-z0-9\s]", " ", team_name.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return TEAM_ALIASES.get(league, {}).get(normalized, normalized)
