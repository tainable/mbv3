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
    "nhl": {
        # Keys must be already-normalized (lowercase, non-alphanumeric → space, no double spaces).
        # Polymarket uses short single-word nicknames; Matchbook/SX Bet use full city+nickname.
        # All short nicknames map to the canonical "city nickname" form.

        # Boston Bruins
        "bruins": "boston bruins",
        "boston bruins nhl": "boston bruins",
        # Buffalo Sabres
        "sabres": "buffalo sabres",
        "buffalo sabres nhl": "buffalo sabres",
        # Detroit Red Wings
        "red wings": "detroit red wings",
        "detroit red wings nhl": "detroit red wings",
        # Florida Panthers
        "panthers": "florida panthers",
        "florida panthers nhl": "florida panthers",
        # Montreal Canadiens
        "canadiens": "montreal canadiens",
        "montreal canadians": "montreal canadiens",  # common misspelling
        # Ottawa Senators
        "senators": "ottawa senators",
        "ottawa senators nhl": "ottawa senators",
        # Tampa Bay Lightning
        "lightning": "tampa bay lightning",
        "tampa bay lightning nhl": "tampa bay lightning",
        "tb lightning": "tampa bay lightning",
        # Toronto Maple Leafs
        "maple leafs": "toronto maple leafs",
        "toronto maple leafs nhl": "toronto maple leafs",
        # Carolina Hurricanes
        "hurricanes": "carolina hurricanes",
        "carolina hurricanes nhl": "carolina hurricanes",
        # Columbus Blue Jackets
        "blue jackets": "columbus blue jackets",
        "columbus blue jackets nhl": "columbus blue jackets",
        # New Jersey Devils
        "devils": "new jersey devils",
        "new jersey devils nhl": "new jersey devils",
        # New York Islanders
        "islanders": "new york islanders",
        "new york islanders nhl": "new york islanders",
        # New York Rangers
        "rangers": "new york rangers",
        "new york rangers nhl": "new york rangers",
        # Philadelphia Flyers
        "flyers": "philadelphia flyers",
        "philadelphia flyers nhl": "philadelphia flyers",
        # Pittsburgh Penguins
        "penguins": "pittsburgh penguins",
        "pittsburgh penguins nhl": "pittsburgh penguins",
        # Washington Capitals
        "capitals": "washington capitals",
        "washington capitals nhl": "washington capitals",
        # Chicago Blackhawks
        "blackhawks": "chicago blackhawks",
        "chicago blackhawks nhl": "chicago blackhawks",
        # Colorado Avalanche
        "avalanche": "colorado avalanche",
        "colorado avalanche nhl": "colorado avalanche",
        # Dallas Stars
        "stars": "dallas stars",
        "dallas stars nhl": "dallas stars",
        # Minnesota Wild
        "wild": "minnesota wild",
        "minnesota wild nhl": "minnesota wild",
        # Nashville Predators
        "predators": "nashville predators",
        "nashville predators nhl": "nashville predators",
        # St. Louis Blues — '.' strips to space → "st  louis blues" → "st louis blues"
        "blues": "st. louis blues",
        "st louis blues": "st. louis blues",
        "saint louis blues": "st. louis blues",
        # Winnipeg Jets
        "jets": "winnipeg jets",
        "winnipeg jets nhl": "winnipeg jets",
        # Anaheim Ducks
        "ducks": "anaheim ducks",
        "anaheim ducks nhl": "anaheim ducks",
        # Calgary Flames
        "flames": "calgary flames",
        "calgary flames nhl": "calgary flames",
        # Edmonton Oilers
        "oilers": "edmonton oilers",
        "edmonton oilers nhl": "edmonton oilers",
        # Los Angeles Kings
        "kings": "los angeles kings",
        "los angeles kings nhl": "los angeles kings",
        "la kings": "los angeles kings",
        # San Jose Sharks
        "sharks": "san jose sharks",
        "san jose sharks nhl": "san jose sharks",
        # Seattle Kraken
        "kraken": "seattle kraken",
        "seattle kraken nhl": "seattle kraken",
        # Vancouver Canucks
        "canucks": "vancouver canucks",
        "vancouver canucks nhl": "vancouver canucks",
        # Vegas Golden Knights
        "golden knights": "vegas golden knights",
        "vegas knights": "vegas golden knights",
        # Utah Hockey Club / Utah Mammoth (relocated from Arizona Coyotes 2024)
        # Polymarket uses "Utah Hc" (canonical), Matchbook uses "Utah Mammoth" (2025-26 rebrand)
        "utah mammoth": "utah hc",
        "utah hockey club": "utah hc",
        "utah": "utah hc",
        "arizona coyotes": "utah hc",
    },
    "uel": {
        # Keys must be already-normalized (lowercase, non-alphanumeric → space, no double spaces).
        # Note: special chars (é, ø, ç, ü, etc.) are stripped to a space by normalize_team_name,
        # then collapsed, so e.g. "Balompié" → "balompi", "Fenerbahçe" → "fenerbah e".

        # Celta de Vigo — Polymarket uses "RC Celta de Vigo"
        "rc celta de vigo": "celta de vigo",
        "rc celta": "celta de vigo",

        # SC Freiburg — SX Bet uses full name "Sport Club Freiburg"
        "sport club freiburg": "sc freiburg",
        "freiburg": "sc freiburg",

        # Aston Villa
        "aston villa fc": "aston villa",

        # Bologna — Polymarket uses "Bologna FC 1909"
        "bologna fc 1909": "bologna",
        "bologna fc": "bologna",

        # Nottingham Forest
        "nottingham forest fc": "nottingham forest",
        "nottm forest": "nottingham forest",
        "nott m forest": "nottingham forest",

        # Porto
        "fc porto": "porto",

        # Real Betis — "Balompié" → 'é' strips to space → "balompi " → "balompi"
        "real betis balompi": "real betis",
        "real betis balompie": "real betis",  # fallback if 'é' encodes as 'e'
        "real betis fc": "real betis",
        "betis": "real betis",

        # Braga — Matchbook uses "Braga", SX Bet uses "Sporting Braga"
        "sc braga": "braga",
        "sporting braga": "braga",
        "sporting de braga": "braga",

        # Eintracht Frankfurt
        "eintracht frankfurt fc": "eintracht frankfurt",
        "sgf": "eintracht frankfurt",

        # Lyon
        "olympique lyonnais": "lyon",
        "ol": "lyon",

        # Roma
        "as roma": "roma",

        # Lazio
        "ss lazio": "lazio",

        # Athletic Club / Bilbao
        "athletic bilbao": "athletic club",
        "athletic club bilbao": "athletic club",

        # Ajax
        "afc ajax": "ajax",
        "ajax amsterdam": "ajax",

        # Fenerbahce — 'ç' strips to space → "fenerbah e"
        "fenerbah e": "fenerbahce",
        "fenerbahce sk": "fenerbahce",

        # Galatasaray
        "galatasaray sk": "galatasaray",
        "galatasaray as": "galatasaray",

        # Rangers
        "glasgow rangers": "rangers",
        "rangers fc": "rangers",

        # Anderlecht
        "rsc anderlecht": "anderlecht",

        # RB Leipzig
        "rasenballsport leipzig": "rb leipzig",
        "red bull leipzig": "rb leipzig",

        # AZ Alkmaar
        "az alkmaar": "az",

        # Bodo/Glimt — 'ø' strips to space → "bod glimt"
        "bod glimt": "bodo glimt",
        "fk bodo glimt": "bodo glimt",
        "bodo/glimt": "bodo glimt",

        # Slavia Prague
        "sk slavia prague": "slavia prague",
        "slavia praha": "slavia prague",

        # Tottenham
        "tottenham hotspur": "tottenham",
        "tottenham hotspur fc": "tottenham",
        "spurs": "tottenham",

        # Manchester United
        "man utd": "manchester united",
        "man united": "manchester united",
        "manchester utd": "manchester united",
        "manchester united fc": "manchester united",

        # Nice
        "ogc nice": "nice",
    },
    "ipl": {
        # Keys must be already-normalized (lowercase, non-alphanumeric → space, no double spaces).
        # Polymarket uses short two/three-letter codes; Matchbook/SX Bet/Smarkets use full names.
        # Canonical form is the full franchise name as used in the 2024-25 season.

        # Short codes → full names (Polymarket slug abbreviations)
        "mi":   "mumbai indians",
        "csk":  "chennai super kings",
        "rcb":  "royal challengers bengaluru",
        "kkr":  "kolkata knight riders",
        "dc":   "delhi capitals",
        "pbks": "punjab kings",
        "pk":   "punjab kings",          # alternative short form
        "rr":   "rajasthan royals",
        "srh":  "sunrisers hyderabad",
        "lsg":  "lucknow super giants",
        "gt":   "gujarat titans",

        # Historical franchise names (providers may still use old names)
        "delhi daredevils":              "delhi capitals",
        "kings xi punjab":               "punjab kings",
        "royal challengers bangalore":   "royal challengers bengaluru",  # renamed 2024
        "rcb bangalore":                 "royal challengers bengaluru",

        # With FC/cc suffixes some providers append
        "mumbai indians ipl":            "mumbai indians",
        "chennai super kings ipl":       "chennai super kings",

        # Polymarket event titles prefix the home team with "Indian Premier League"
        # (e.g. "Indian Premier League Mumbai Indians vs Punjab Kings").
        # When _parse_teams splits on " vs ", the left half carries this prefix, which
        # must be stripped so the canonical home_team matches bare selection_names.
        "indian premier league mumbai indians":               "mumbai indians",
        "indian premier league chennai super kings":          "chennai super kings",
        "indian premier league royal challengers bengaluru":  "royal challengers bengaluru",
        "indian premier league royal challengers bangalore":  "royal challengers bengaluru",
        "indian premier league kolkata knight riders":        "kolkata knight riders",
        "indian premier league delhi capitals":               "delhi capitals",
        "indian premier league punjab kings":                 "punjab kings",
        "indian premier league rajasthan royals":             "rajasthan royals",
        "indian premier league sunrisers hyderabad":          "sunrisers hyderabad",
        "indian premier league lucknow super giants":         "lucknow super giants",
        "indian premier league gujarat titans":               "gujarat titans",
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
