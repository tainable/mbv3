"""
specials_registry.py
--------------------
Catalog of one-off prediction markets ("specials") and the strategies enabled
for each.  Separate from the sports pipeline by design — specials use manually
curated provider IDs and outcome mappings rather than the slug/team-name
discovery the sports providers do.

Edit SPECIALS_EVENTS to add a new event.  Run find_polymarket_specials.py and
find_matchbook_specials.py to capture the IDs you need to populate the catalog.

Catalog vs strategies
---------------------
Each event has two halves:

  providers : the catalog — per-provider market IDs and per-outcome metadata.
              Static once captured; only changes if the market itself is
              re-listed or a candidate is added/replaced by the bookie.

  strategies: a list of plays opted into for this event.  Each strategy is
              type-tagged and individually enabled.  Disabling a strategy is
              instant (flip 'enabled': False) and does not require touching
              the catalog.

Strategy types
--------------
  cross_hedge          Back on one provider, lay on another. Direction is
                       explicit: back-side and lay-side each name their
                       provider+outcome+side. The 'risk_class' field documents
                       what's NOT hedged (e.g. candidate replacement).

  same_provider_basket Back every outcome on one provider. If Σ implied prob
                       < 1 you have a true sure bet — every outcome is covered.
                       Use include_outcomes='*' to require the catalog be
                       complete (refuses to fire if any catalog outcome lacks
                       a price).

  selective_basket     Back a subset of outcomes on one provider. Not a sure
                       bet — loses everything if an excluded outcome wins.
                       max_excluded_prob is a hard cutoff: the strategy only
                       fires if Σ implied prob of excluded outcomes is below
                       the threshold.

Safety defaults
---------------
Every strategy is born with enabled=False and alert_only=True.  Flip enabled
to surface the strategy in evaluations; flip alert_only to make it eligible
for auto-bet.  Both flags must be off to ever place a bet, on top of the
global --auto-bet flag at the orchestrator level.
"""
from __future__ import annotations

from typing import Any


SPECIALS_EVENTS: dict[str, dict[str, Any]] = {

    # =========================================================================
    # Makerfield by-election — discovery pending
    # =========================================================================
    # Run find_polymarket_specials.py --search "makerfield" and
    # find_matchbook_specials.py --search "makerfield" to populate the IDs
    # below.  Cross-check the candidate->party mapping against the official
    # nomination paper before flipping any strategy on.
    "makerfield_by_election_2026": {
        "key":         "makerfield_by_election_2026",
        "title":       "Makerfield by-election",
        "resolves_by": "2026-06-19T06:00:00Z",   # count expected overnight after polling day 18 Jun

        # Active-window config.
        # For a Sinner French Open outright you would instead use:
        #   "active": {
        #       "blackout_windows": [
        #           {"start": "2026-05-27T09:00:00Z", "end": "2026-05-27T18:00:00Z"},
        #       ],
        #       # "dynamic_blackout_callable": "sinner_checks.is_on_court",
        #       "cutoff_minutes_before_resolution": 120,
        #   },
        "active": {
            "cutoff_minutes_before_resolution": 60,
            "blackout_windows": [
                # Overnight count window — polls close 22:00 BST (21:00 UTC), result ~03:00 BST
                {"start": "2026-06-18T21:00:00Z", "end": "2026-06-19T06:00:00Z"},
            ],
        },

        "providers": {
            "polymarket": {
                "market_id": "485538",            # Gamma event: "Makerfield by-election Winner"
                "outcomes": {
                    "andy_burnham": {
                        "clob_token_id":    "90009899914557914380740818092814887697530845879971485578382561848013521916029",
                        "clob_token_id_no": "68040636488505157931853763274692990642251292767458498478812146930949037630253",
                        "candidate": "Andy Burnham",
                        "party":     "labour",
                    },
                    "robert_kenyon": {
                        "clob_token_id":    "32749652958490459738077534958372321998219313230738592738299008092673106086919",
                        "clob_token_id_no": "69647131137181263161209582692292085273656986396122522741327860094193790245800",
                        "candidate": "Robert Kenyon",
                        "party":     "reform",
                    },
                    "rebecca_shepherd": {
                        "clob_token_id":    "32605480710229459496814068809521535528500860730244041769252795555668751623653",
                        "clob_token_id_no": "99124197360263115356180485758906225193712536154336191636725841881851359643673",
                        "candidate": "Rebecca Shepherd",
                        "party":     "restore_britain",
                    },
                },
            },
            "matchbook": {
                "event_id":  33318904842600023,
                "market_id": 33318923194700023,
                "outcomes": {
                    "labour":          {"runner_id": 33318923194901023, "party": "labour"},
                    "reform":          {"runner_id": 33318923195101023, "party": "reform"},
                    "restore_britain": {"runner_id": 33318923195301023, "party": "restore_britain"},
                    "green":           {"runner_id": 33318923195600023, "party": "green"},
                    "conservative":    {"runner_id": 33318923195701023, "party": "conservative"},
                    "lib_dem":         {"runner_id": 33318923195901023, "party": "lib_dem"},
                },
            },
        },

        "strategies": [

            # ---- Cross-provider hedge: Burnham / Labour ---------------------
            # Risk: if Burnham is replaced AND a different Labour candidate wins,
            # both legs lose.  Disable if candidate uncertainty arises.
            {
                "type":         "cross_hedge",
                "name":         "burnham-labour-hedge",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "andy_burnham", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "labour",       "side": "lay"},
                "min_edge_pct": 0.5,
                "risk_class":   "candidate_replacement",
                "gap_outcomes": ["non_burnham_labour_wins"],
                "notes":        "Loses both legs if Burnham replaced AND another Labour candidate wins.",
            },

            # ---- Cross-provider hedge: Kenyon / Reform ----------------------
            {
                "type":         "cross_hedge",
                "name":         "kenyon-reform-hedge",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "robert_kenyon", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "reform",        "side": "lay"},
                "min_edge_pct": 0.5,
                "risk_class":   "candidate_replacement",
                "gap_outcomes": ["non_kenyon_reform_wins"],
                "notes":        "Loses both legs if Kenyon replaced AND another Reform candidate wins.",
            },

            # ---- Cross-provider hedge: Shepherd / Restore Britain -----------
            {
                "type":         "cross_hedge",
                "name":         "shepherd-restore-britain-hedge",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "rebecca_shepherd", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "restore_britain",  "side": "lay"},
                "min_edge_pct": 0.5,
                "risk_class":   "candidate_replacement",
                "gap_outcomes": ["non_shepherd_restore_britain_wins"],
                "notes":        "Loses both legs if Shepherd replaced AND another Restore Britain candidate wins.",
            },
        ],
    },

    # =========================================================================
    # US Presidential Election 2028
    # =========================================================================
    # Both Polymarket and Matchbook list the same named candidates, so the arb
    # runs in BOTH directions per candidate:
    #   Direction 1 — PM YES + MB lay : fires when PM back odds > MB lay odds
    #   Direction 2 — MB back + PM NO : fires when MB back odds > PM NO equivalent
    #
    # Strategies are auto-generated from the catalog candidate list below.
    # To add a candidate: add them to providers.polymarket.outcomes and
    # providers.matchbook.outcomes with matching keys, then re-run the scan.
    "us_presidential_2028": {
        "key":         "us_presidential_2028",
        "title":       "US Presidential Election 2028",
        "resolves_by": "2028-11-15T12:00:00Z",   # allow up to a week for the call

        "active": {
            "cutoff_minutes_before_resolution": 120,
        },

        "providers": {
            "polymarket": {
                "market_id": "31552",             # Gamma event: "Presidential Election Winner 2028"
                "outcomes": {
                    "jd_vance": {
                        "clob_token_id":    "16040015440196279900485035793550429453516625694844857319147506590755961451627",
                        "clob_token_id_no": "94476829201604408463453426454480212459887267917122244941405244686637914508323",
                        "candidate": "JD Vance", "party": "republican",
                    },
                    "gavin_newsom": {
                        "clob_token_id":    "98250445447699368679516529207365255018790721464590833209064266254238063117329",
                        "clob_token_id_no": "18823838997443878656879952590502524526556504037944392973476854588563571859850",
                        "candidate": "Gavin Newsom", "party": "democrat",
                    },
                    "marco_rubio": {
                        "clob_token_id":    "11171618905857319805355893398009978356002287408842633593535559412449299952250",
                        "clob_token_id_no": "65892530750232246342273648992064742042382898919201083513241874070914353628268",
                        "candidate": "Marco Rubio", "party": "republican",
                    },
                    "josh_shapiro": {
                        "clob_token_id":    "1420113190297259908358916251277696183671602680024474979931472444792502392139",
                        "clob_token_id_no": "9195312844128905514645503106857912558078141569888031031723687687489317147622",
                        "candidate": "Josh Shapiro", "party": "democrat",
                    },
                    "gretchen_whitmer": {
                        "clob_token_id":    "4999047257294547885970323631312419427544062131054733229841947946130772277335",
                        "clob_token_id_no": "102084577517225131375286042838758381959278092930102485541091152099183604495224",
                        "candidate": "Gretchen Whitmer", "party": "democrat",
                    },
                    "aoc": {
                        "clob_token_id":    "8113830512143065501177519557124858581375945005490796772950589720955457782465",
                        "clob_token_id_no": "11493926619286817179231142642565394485790380952577225820901334479399429951651",
                        "candidate": "Alexandria Ocasio-Cortez", "party": "democrat",
                    },
                    "pete_buttigieg": {
                        "clob_token_id":    "35629456194498013232473191985311930903993567273832459837879398120893197410860",
                        "clob_token_id_no": "26087406687932108531005204502244492184534000122992461059156880611150953145122",
                        "candidate": "Pete Buttigieg", "party": "democrat",
                    },
                    "kamala_harris": {
                        "clob_token_id":    "70663352401606372246362604193214664065595751757222752105245221905399175050480",
                        "clob_token_id_no": "4988819538339626436114997160558538168652916536529198363646261454228218983102",
                        "candidate": "Kamala Harris", "party": "democrat",
                    },
                    "ron_desantis": {
                        "clob_token_id":    "104480514270625615787868282411330496385404675495672110015326625654626015018993",
                        "clob_token_id_no": "65724430065358897810064400118831287249960059855415477063660293398018349529272",
                        "candidate": "Ron DeSantis", "party": "republican",
                    },
                    "donald_trump_jr": {
                        "clob_token_id":    "77660944053451874285291959901958937238149180369572698706675030657716494119346",
                        "clob_token_id_no": "52761630651452310319295064365704431564140478140269624735570317771849185169635",
                        "candidate": "Donald Trump Jr.", "party": "republican",
                    },
                    "ivanka_trump": {
                        "clob_token_id":    "40932864651005132434669457369564353816786330205469778285035893634458129671101",
                        "clob_token_id_no": "56215025902031889702122383201324191240581604266667334383154318117703136383150",
                        "candidate": "Ivanka Trump", "party": "republican",
                    },
                    "nikki_haley": {
                        "clob_token_id":    "14253956839582698877884742930225331487593177635330318923853046874994118249367",
                        "clob_token_id_no": "84386913213192310444216977895083273572419886997719576824320630372812149173320",
                        "candidate": "Nikki Haley", "party": "republican",
                    },
                    "wes_moore": {
                        "clob_token_id":    "35272507273162958827997790327967872292415567859334925862043618606081750011897",
                        "clob_token_id_no": "374661216370333715221919731169956915917040066803205972387948453758235235593",
                        "candidate": "Wes Moore", "party": "democrat",
                    },
                    "tulsi_gabbard": {
                        "clob_token_id":    "97633418272282627828834872960145259034624388719803091466146359304286851612172",
                        "clob_token_id_no": "17854429586516792397214557963923647011517685971868312042416963526864862523185",
                        "candidate": "Tulsi Gabbard", "party": "republican",
                    },
                    "vivek_ramaswamy": {
                        "clob_token_id":    "48067717079255656974334181122173546721823870434119599450595298250019538972254",
                        "clob_token_id_no": "50325246165657443726245430907883748796540778371466135108478470877169556977248",
                        "candidate": "Vivek Ramaswamy", "party": "republican",
                    },
                    "jon_ossoff": {
                        "clob_token_id":    "110102705199530588976777616718115948005824665233400132417374621750887344831757",
                        "clob_token_id_no": "98394838816403247684221016468696784125095120854407803889228262583112436699261",
                        "candidate": "Jon Ossoff", "party": "democrat",
                    },
                    "andy_beshear": {
                        "clob_token_id":    "50507496430692162923857760920722624157737138256097150616293040054748564876552",
                        "clob_token_id_no": "53112547591746110422238703365888013497196468199611167129486770819598374380742",
                        "candidate": "Andy Beshear", "party": "democrat",
                    },
                    "jb_pritzker": {
                        "clob_token_id":    "70948731404464821824516595498024506131170394300814446118501620719416471444076",
                        "clob_token_id_no": "111825297267676135304812085666512747626812691749338692567733264184356420470119",
                        "candidate": "JB Pritzker", "party": "democrat",
                    },
                    "glenn_youngkin": {
                        "clob_token_id":    "631059815958854406224163480574507434200946764542129005247984171366597476420",
                        "clob_token_id_no": "100418331065194201410948024347944500798513421135064468615649525110872253639397",
                        "candidate": "Glenn Youngkin", "party": "republican",
                    },
                    "tucker_carlson": {
                        "clob_token_id":    "45015916581148592087728250896178171120552786117190685135618507471166113474733",
                        "clob_token_id_no": "5353194854864999214467212176625447374807704051407621946584152368216621932371",
                        "candidate": "Tucker Carlson", "party": "republican",
                    },
                },
            },
            "matchbook": {
                "event_id":  29403376931300076,
                "market_id": 29403415731100076,
                "outcomes": {
                    "jd_vance":         {"runner_id": 29403417445700076, "party": "republican"},
                    "gavin_newsom":     {"runner_id": 29403420745600076, "party": "democrat"},
                    "marco_rubio":      {"runner_id": 29403442509600076, "party": "republican"},
                    "josh_shapiro":     {"runner_id": 29403418537000076, "party": "democrat"},
                    "gretchen_whitmer": {"runner_id": 29403425910800076, "party": "democrat"},
                    "aoc":              {"runner_id": 29403432892300076, "party": "democrat"},
                    "pete_buttigieg":   {"runner_id": 29403423283600076, "party": "democrat"},
                    "kamala_harris":    {"runner_id": 29403441555900076, "party": "democrat"},
                    "ron_desantis":     {"runner_id": 29403430165900076, "party": "republican"},
                    "donald_trump_jr":  {"runner_id": 29403428589800076, "party": "republican"},
                    "ivanka_trump":     {"runner_id": 29403430935400076, "party": "republican"},
                    "nikki_haley":      {"runner_id": 29403437725700076, "party": "republican"},
                    "wes_moore":        {"runner_id": 29403433638900076, "party": "democrat"},
                    "tulsi_gabbard":    {"runner_id": 29403436899800076, "party": "republican"},
                    "vivek_ramaswamy":  {"runner_id": 29403438713900076, "party": "republican"},
                    "jon_ossoff":       {"runner_id": 29403473871400076, "party": "democrat"},
                    "andy_beshear":     {"runner_id": 29403434895700076, "party": "democrat"},
                    "jb_pritzker":      {"runner_id": 29403449943500076, "party": "democrat"},
                    "glenn_youngkin":   {"runner_id": 29403450928000076, "party": "republican"},
                    "tucker_carlson":   {"runner_id": 29403465413900076, "party": "republican"},
                },
            },
        },

        # Auto-generate both arb directions for every candidate in the catalog.
        # Direction 1: PM YES back + MB lay  (fires when PM misprices high vs MB)
        # Direction 2: MB back + PM NO back  (fires when MB misprices high vs PM)
        "strategies": [
            strat
            for candidate in [
                "jd_vance", "gavin_newsom", "marco_rubio", "josh_shapiro",
                "gretchen_whitmer", "aoc", "pete_buttigieg", "kamala_harris",
                "ron_desantis", "donald_trump_jr", "ivanka_trump", "nikki_haley",
                "wes_moore", "tulsi_gabbard", "vivek_ramaswamy", "jon_ossoff",
                "andy_beshear", "jb_pritzker", "glenn_youngkin", "tucker_carlson",
            ]
            for strat in [
                {
                    "type":         "cross_hedge",
                    "name":         f"{candidate}-pm-mb",
                    "enabled":      True,
                    "alert_only":   True,
                    "back":         {"provider": "polymarket", "outcome": candidate, "side": "yes"},
                    "lay":          {"provider": "matchbook",  "outcome": candidate, "side": "lay"},
                    "min_edge_pct": 1.0,
                    "risk_class":   "cross_platform",
                },
                {
                    "type":         "cross_hedge",
                    "name":         f"{candidate}-mb-pm",
                    "enabled":      True,
                    "alert_only":   True,
                    "back":         {"provider": "matchbook",  "outcome": candidate, "side": "back"},
                    "lay":          {"provider": "polymarket", "outcome": candidate, "side": "no"},
                    "min_edge_pct": 1.0,
                    "risk_class":   "cross_platform",
                },
            ]
        ],
    },

    # =========================================================================
    # 2026 FIFA World Cup Group Stage Winners (A–L)
    # =========================================================================
    # Polymarket slugs: world-cup-group-{a..l}-winner
    # Matchbook: "FIFA World Cup - Group {A..L} Winner" (separate events)
    # All groups resolve by 2026-06-27T06:00:00Z (last matchday June 25-26)
    #
    # Name normalisations across groups:
    #   turkiye         — PM "Türkiye"            / MB "Türkiye"
    #   bosnia_herzegovina — PM "Bosnia and Herzegovina" / MB "Bosnia & Herzegovina"
    #   curacao         — PM "Curaçao"            / MB "Curaçao"
    #   congo_dr        — PM "Congo DR"           / MB "DR Congo"

    # --- Group A: Mexico, Czechia, South Korea, South Africa ---
    "wc_2026_group_a": {
        "key":         "wc_2026_group_a",
        "title":       "2026 FIFA World Cup Group A Winner",
        "resolves_by": "2026-06-27T06:00:00Z",
        "active": {"cutoff_minutes_before_resolution": 90},
        "providers": {
            "polymarket": {
                "market_id": "98252",
                "outcomes": {
                    "mexico":       {"clob_token_id": "46772842018157002809944329297399754730565067653684028656821201330414899002319", "clob_token_id_no": "2255922344739472302348163765571751657183794883928857683040904373129390919064",  "candidate": "Mexico"},
                    "south_korea":  {"clob_token_id": "87351907526943178422029741800130120652110413212978205825674325623212966790505", "clob_token_id_no": "78670562145011339714199048291487309804972837282336953106976349058344426268732", "candidate": "South Korea"},
                    "south_africa": {"clob_token_id": "70207728888349217963806621760705113568803648303314432404178123889438828460434", "clob_token_id_no": "77945040490655001935193081336327056310599494073693136493386019937539517746988", "candidate": "South Africa"},
                    "czechia":      {"clob_token_id": "96465681201202920147086919609793552982259292941505132446288923410507678316564", "clob_token_id_no": "97910690931826668675178880694933538183472147505310558778359290060810483415994", "candidate": "Czechia"},
                },
            },
            "matchbook": {
                "event_id":  32969933557900081,
                "market_id": 32969946047800081,
                "outcomes": {
                    "mexico":       {"runner_id": 32969946048200081, "candidate": "Mexico"},
                    "czechia":      {"runner_id": 32969946048600081, "candidate": "Czechia"},
                    "south_korea":  {"runner_id": 32969946049001081, "candidate": "South Korea"},
                    "south_africa": {"runner_id": 32969946048800081, "candidate": "South Africa"},
                },
            },
        },
        "strategies": [
            strat
            for team in ["mexico", "czechia", "south_korea", "south_africa"]
            for strat in [
                {"type": "cross_hedge", "name": f"{team}-pm-mb", "enabled": True, "alert_only": True,
                 "back": {"provider": "polymarket", "outcome": team, "side": "yes"},
                 "lay":  {"provider": "matchbook",  "outcome": team, "side": "lay"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
                {"type": "cross_hedge", "name": f"{team}-mb-pm", "enabled": True, "alert_only": True,
                 "back": {"provider": "matchbook",  "outcome": team, "side": "back"},
                 "lay":  {"provider": "polymarket", "outcome": team, "side": "no"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
            ]
        ],
    },

    # --- Group B: Switzerland, Canada, Bosnia & Herzegovina, Qatar ---
    "wc_2026_group_b": {
        "key":         "wc_2026_group_b",
        "title":       "2026 FIFA World Cup Group B Winner",
        "resolves_by": "2026-06-27T06:00:00Z",
        "active": {"cutoff_minutes_before_resolution": 90},
        "providers": {
            "polymarket": {
                "market_id": "98263",
                "outcomes": {
                    "canada":              {"clob_token_id": "48187624975632752295312561743215593385055796608528135882106706924535491220897", "clob_token_id_no": "97671046113000204950878221061928218613413930047906731660847862929889111113868", "candidate": "Canada"},
                    "qatar":               {"clob_token_id": "37311826215166621578658829276253228521642803688744106418575628473685175197384", "clob_token_id_no": "16906633031420318575056881602651499224113051501441427029273525668505553336623", "candidate": "Qatar"},
                    "bosnia_herzegovina":  {"clob_token_id": "12881745432815950650844966177787773573153210953075860709534550099018183696164", "clob_token_id_no": "22015518537995443113231113889633032891449456021290898799876397094426107803585", "candidate": "Bosnia and Herzegovina"},
                    "switzerland":         {"clob_token_id": "87499184655381882963387695123459684203225076159948233321927840510488271812742", "clob_token_id_no": "84601253470291336367793929535870321504816493185633556218432665574668042171346", "candidate": "Switzerland"},
                },
            },
            "matchbook": {
                "event_id":  32969955290400081,
                "market_id": 32969971844100081,
                "outcomes": {
                    "switzerland":        {"runner_id": 32969971844300081, "candidate": "Switzerland"},
                    "canada":             {"runner_id": 32969971844601081, "candidate": "Canada"},
                    "bosnia_herzegovina": {"runner_id": 32969971844801081, "candidate": "Bosnia & Herzegovina"},
                    "qatar":              {"runner_id": 32969971845100081, "candidate": "Qatar"},
                },
            },
        },
        "strategies": [
            strat
            for team in ["switzerland", "canada", "bosnia_herzegovina", "qatar"]
            for strat in [
                {"type": "cross_hedge", "name": f"{team}-pm-mb", "enabled": True, "alert_only": True,
                 "back": {"provider": "polymarket", "outcome": team, "side": "yes"},
                 "lay":  {"provider": "matchbook",  "outcome": team, "side": "lay"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
                {"type": "cross_hedge", "name": f"{team}-mb-pm", "enabled": True, "alert_only": True,
                 "back": {"provider": "matchbook",  "outcome": team, "side": "back"},
                 "lay":  {"provider": "polymarket", "outcome": team, "side": "no"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
            ]
        ],
    },

    # --- Group C: Brazil, Morocco, Scotland, Haiti ---
    "wc_2026_group_c": {
        "key":         "wc_2026_group_c",
        "title":       "2026 FIFA World Cup Group C Winner",
        "resolves_by": "2026-06-27T06:00:00Z",
        "active": {"cutoff_minutes_before_resolution": 90},
        "providers": {
            "polymarket": {
                "market_id": "98264",
                "outcomes": {
                    "scotland": {"clob_token_id": "66265680142177294497572235248200066124169304713332831132816781431445413907569", "clob_token_id_no": "96800156676225921195730752129404882998029968936208844846299752417475268205786", "candidate": "Scotland"},
                    "brazil":   {"clob_token_id": "81847767762276015376638362563472043266790475968888863771265036582040507866830", "clob_token_id_no": "284717320081839269860357237903286904886607070475454682161945348414652316634",  "candidate": "Brazil"},
                    "haiti":    {"clob_token_id": "91399166209216163431231173062786395215620442056888296437823451282732143924332", "clob_token_id_no": "79641397642615548218794504851128898947984007797041258964017360526641252308395", "candidate": "Haiti"},
                    "morocco":  {"clob_token_id": "9993362623254508594982212570716155560975868718584876749669600087469425428671",  "clob_token_id_no": "31605067815721599197943501343636662558593313871709056980278563636917591139852", "candidate": "Morocco"},
                },
            },
            "matchbook": {
                "event_id":  32969979540000081,
                "market_id": 32970071751800081,
                "outcomes": {
                    "brazil":   {"runner_id": 32970071752101081, "candidate": "Brazil"},
                    "morocco":  {"runner_id": 32970071752501081, "candidate": "Morocco"},
                    "scotland": {"runner_id": 32970071753101081, "candidate": "Scotland"},
                    "haiti":    {"runner_id": 32970071752800081, "candidate": "Haiti"},
                },
            },
        },
        "strategies": [
            strat
            for team in ["brazil", "morocco", "scotland", "haiti"]
            for strat in [
                {"type": "cross_hedge", "name": f"{team}-pm-mb", "enabled": True, "alert_only": True,
                 "back": {"provider": "polymarket", "outcome": team, "side": "yes"},
                 "lay":  {"provider": "matchbook",  "outcome": team, "side": "lay"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
                {"type": "cross_hedge", "name": f"{team}-mb-pm", "enabled": True, "alert_only": True,
                 "back": {"provider": "matchbook",  "outcome": team, "side": "back"},
                 "lay":  {"provider": "polymarket", "outcome": team, "side": "no"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
            ]
        ],
    },

    # --- Group D: USA, Türkiye, Paraguay, Australia ---
    "wc_2026_group_d": {
        "key":         "wc_2026_group_d",
        "title":       "2026 FIFA World Cup Group D Winner",
        "resolves_by": "2026-06-27T06:00:00Z",
        "active": {"cutoff_minutes_before_resolution": 90},
        "providers": {
            "polymarket": {
                "market_id": "98266",
                "outcomes": {
                    "paraguay": {"clob_token_id": "79760491979992857160973768218230450627919182641934117785417889721150583757281", "clob_token_id_no": "77720844980623572458823664521476028896539571660362208157388982643937026784235", "candidate": "Paraguay"},
                    "turkiye":  {"clob_token_id": "2998401951487647599946104879570449804897073856917450832908434188988033277425",  "clob_token_id_no": "42756805753536141978592390600745564917611989349340240303914783332767038437903", "candidate": "Türkiye"},
                    "usa":      {"clob_token_id": "12851947870596792913759169846820854206991726435656942626040547199033743252060", "clob_token_id_no": "78730716186090032094554028763269655005261142977694562088127031819054075553429", "candidate": "USA"},
                    "australia":{"clob_token_id": "69010504454296192424119678911693824678228097995152248626000204709985081973540", "clob_token_id_no": "82006152933429093137140190574393994409946252744133752481099352602247816575196", "candidate": "Australia"},
                },
            },
            "matchbook": {
                "event_id":  32969985655800081,
                "market_id": 32970079295900081,
                "outcomes": {
                    "usa":       {"runner_id": 32970079296400081, "candidate": "USA"},
                    "turkiye":   {"runner_id": 32970079296701081, "candidate": "Türkiye"},
                    "paraguay":  {"runner_id": 32970079297100081, "candidate": "Paraguay"},
                    "australia": {"runner_id": 32970079297401081, "candidate": "Australia"},
                },
            },
        },
        "strategies": [
            strat
            for team in ["usa", "turkiye", "paraguay", "australia"]
            for strat in [
                {"type": "cross_hedge", "name": f"{team}-pm-mb", "enabled": True, "alert_only": True,
                 "back": {"provider": "polymarket", "outcome": team, "side": "yes"},
                 "lay":  {"provider": "matchbook",  "outcome": team, "side": "lay"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
                {"type": "cross_hedge", "name": f"{team}-mb-pm", "enabled": True, "alert_only": True,
                 "back": {"provider": "matchbook",  "outcome": team, "side": "back"},
                 "lay":  {"provider": "polymarket", "outcome": team, "side": "no"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
            ]
        ],
    },

    # --- Group E: Germany, Ecuador, Ivory Coast, Curaçao ---
    "wc_2026_group_e": {
        "key":         "wc_2026_group_e",
        "title":       "2026 FIFA World Cup Group E Winner",
        "resolves_by": "2026-06-27T06:00:00Z",
        "active": {"cutoff_minutes_before_resolution": 90},
        "providers": {
            "polymarket": {
                "market_id": "98271",
                "outcomes": {
                    "curacao":     {"clob_token_id": "52664025558236401756255522072101814494878370874359672539511824678935707929254", "clob_token_id_no": "86117921529062342101527542096312658892943297905285997597661872070583825587880", "candidate": "Curaçao"},
                    "ecuador":     {"clob_token_id": "34573952063722244002460616101049750490257186169502334603161738792981917623593", "clob_token_id_no": "6311300232397758809507796546673730917178524850603918488920616779608125616525",  "candidate": "Ecuador"},
                    "germany":     {"clob_token_id": "59218941927047907810591387256060705413648802249558263579729635010260975412938", "clob_token_id_no": "4047586417078976184094031568818397069472713493953171551597358674820666726773",  "candidate": "Germany"},
                    "ivory_coast": {"clob_token_id": "62454809816723771663546500491039435418126766289492834276388417922943351452939", "clob_token_id_no": "51119932436248322677799270933603720827071860779785081947199711042216174707587", "candidate": "Ivory Coast"},
                },
            },
            "matchbook": {
                "event_id":  32969988729500081,
                "market_id": 32970083493700081,
                "outcomes": {
                    "germany":     {"runner_id": 32970083493900081, "candidate": "Germany"},
                    "ecuador":     {"runner_id": 32970083494601081, "candidate": "Ecuador"},
                    "ivory_coast": {"runner_id": 32970083494400081, "candidate": "Ivory Coast"},
                    "curacao":     {"runner_id": 32970083494200081, "candidate": "Curaçao"},
                },
            },
        },
        "strategies": [
            strat
            for team in ["germany", "ecuador", "ivory_coast", "curacao"]
            for strat in [
                {"type": "cross_hedge", "name": f"{team}-pm-mb", "enabled": True, "alert_only": True,
                 "back": {"provider": "polymarket", "outcome": team, "side": "yes"},
                 "lay":  {"provider": "matchbook",  "outcome": team, "side": "lay"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
                {"type": "cross_hedge", "name": f"{team}-mb-pm", "enabled": True, "alert_only": True,
                 "back": {"provider": "matchbook",  "outcome": team, "side": "back"},
                 "lay":  {"provider": "polymarket", "outcome": team, "side": "no"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
            ]
        ],
    },

    # --- Group F: Netherlands, Japan, Sweden, Tunisia ---
    "wc_2026_group_f": {
        "key":         "wc_2026_group_f",
        "title":       "2026 FIFA World Cup Group F Winner",
        "resolves_by": "2026-06-27T06:00:00Z",
        "active": {"cutoff_minutes_before_resolution": 90},
        "providers": {
            "polymarket": {
                "market_id": "98272",
                "outcomes": {
                    "tunisia":     {"clob_token_id": "401092017045416095806154226065286673247228247545697412935246410167851803426",   "clob_token_id_no": "60563244978093325118606039389318141307397013317638951849398395626525992974214", "candidate": "Tunisia"},
                    "japan":       {"clob_token_id": "6145459172806300928111232756204733500855895982725896082419535913841017504888",   "clob_token_id_no": "49462964785520085072530979138125889030762763191239290243513717467706566660027", "candidate": "Japan"},
                    "netherlands": {"clob_token_id": "88806419358433907255572616491822943056677544856183755131290982378194523182571",  "clob_token_id_no": "25038296623521848527195253266240699595375885625506972029849790750942036710616", "candidate": "Netherlands"},
                    "sweden":      {"clob_token_id": "12278428292465793035804008354384439230187602276736110039881536767419680383785",  "clob_token_id_no": "94130066052387862643883814651188838244122915296859975608727718612469776924640", "candidate": "Sweden"},
                },
            },
            "matchbook": {
                "event_id":  32969991011400081,
                "market_id": 32970088614100081,
                "outcomes": {
                    "netherlands": {"runner_id": 32970088614301081, "candidate": "Netherlands"},
                    "japan":       {"runner_id": 32970088614501081, "candidate": "Japan"},
                    "sweden":      {"runner_id": 32970088614701081, "candidate": "Sweden"},
                    "tunisia":     {"runner_id": 32970088614901081, "candidate": "Tunisia"},
                },
            },
        },
        "strategies": [
            strat
            for team in ["netherlands", "japan", "sweden", "tunisia"]
            for strat in [
                {"type": "cross_hedge", "name": f"{team}-pm-mb", "enabled": True, "alert_only": True,
                 "back": {"provider": "polymarket", "outcome": team, "side": "yes"},
                 "lay":  {"provider": "matchbook",  "outcome": team, "side": "lay"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
                {"type": "cross_hedge", "name": f"{team}-mb-pm", "enabled": True, "alert_only": True,
                 "back": {"provider": "matchbook",  "outcome": team, "side": "back"},
                 "lay":  {"provider": "polymarket", "outcome": team, "side": "no"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
            ]
        ],
    },

    # --- Group G: Belgium, Egypt, Iran, New Zealand ---
    "wc_2026_group_g": {
        "key":         "wc_2026_group_g",
        "title":       "2026 FIFA World Cup Group G Winner",
        "resolves_by": "2026-06-27T06:00:00Z",
        "active": {"cutoff_minutes_before_resolution": 90},
        "providers": {
            "polymarket": {
                "market_id": "98273",
                "outcomes": {
                    "new_zealand": {"clob_token_id": "99475148310728334521105712086896756791459890464171235927033307392239026849840", "clob_token_id_no": "37895244194050034103716086162029008030453409573420147745990983310246853999787", "candidate": "New Zealand"},
                    "iran":        {"clob_token_id": "73789156198019359880626199750993974213903650385789628298266593898486463891949", "clob_token_id_no": "854649518554835817195373344515950319419725002359236089618701397306565294847",  "candidate": "Iran"},
                    "egypt":       {"clob_token_id": "7584761206200526306024688158719132295937581405262529580584713385959111789894",  "clob_token_id_no": "98678179911612220135527125607509026193232485435795264014075483342129392841152", "candidate": "Egypt"},
                    "belgium":     {"clob_token_id": "99321633597831181390903962340220460451021767803773731828899913833943125225395", "clob_token_id_no": "30359884005901516049306058537929357428540164330039855907372415952891778624451", "candidate": "Belgium"},
                },
            },
            "matchbook": {
                "event_id":  32969993392600081,
                "market_id": 32970093684000081,
                "outcomes": {
                    "belgium":     {"runner_id": 32970093684101081, "candidate": "Belgium"},
                    "egypt":       {"runner_id": 32970093684400081, "candidate": "Egypt"},
                    "iran":        {"runner_id": 32970093684600081, "candidate": "Iran"},
                    "new_zealand": {"runner_id": 32970093684800081, "candidate": "New Zealand"},
                },
            },
        },
        "strategies": [
            strat
            for team in ["belgium", "egypt", "iran", "new_zealand"]
            for strat in [
                {"type": "cross_hedge", "name": f"{team}-pm-mb", "enabled": True, "alert_only": True,
                 "back": {"provider": "polymarket", "outcome": team, "side": "yes"},
                 "lay":  {"provider": "matchbook",  "outcome": team, "side": "lay"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
                {"type": "cross_hedge", "name": f"{team}-mb-pm", "enabled": True, "alert_only": True,
                 "back": {"provider": "matchbook",  "outcome": team, "side": "back"},
                 "lay":  {"provider": "polymarket", "outcome": team, "side": "no"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
            ]
        ],
    },

    # --- Group H: Spain, Uruguay, Saudi Arabia, Cape Verde ---
    "wc_2026_group_h": {
        "key":         "wc_2026_group_h",
        "title":       "2026 FIFA World Cup Group H Winner",
        "resolves_by": "2026-06-27T06:00:00Z",
        "active": {"cutoff_minutes_before_resolution": 90},
        "providers": {
            "polymarket": {
                "market_id": "98287",
                "outcomes": {
                    "cape_verde":   {"clob_token_id": "48798713543570043708519572283996274166434555254223187784173975782323893253161", "clob_token_id_no": "18886071757433730805058425783489577095531488965406349218603184777181999398816", "candidate": "Cape Verde"},
                    "uruguay":      {"clob_token_id": "28340358178431871363210562118980589174378751473778056391324752187634135006587", "clob_token_id_no": "25187617842973863417046397115457758055463276587336812276748398130048708487113", "candidate": "Uruguay"},
                    "spain":        {"clob_token_id": "53767774368871487958852302456977659279367510767287923221897564430044671384128", "clob_token_id_no": "37821011509134197239774147789543695590581250725127252293498663449982199078732", "candidate": "Spain"},
                    "saudi_arabia": {"clob_token_id": "62936673101986451926090395737592610622578623891593865188871534951560986755694", "clob_token_id_no": "82288808824512831867843440235168062258569745676202189749082431944418762530133", "candidate": "Saudi Arabia"},
                },
            },
            "matchbook": {
                "event_id":  32969995773100081,
                "market_id": 32970098798800081,
                "outcomes": {
                    "spain":        {"runner_id": 32970098799001081, "candidate": "Spain"},
                    "uruguay":      {"runner_id": 32970098799900081, "candidate": "Uruguay"},
                    "saudi_arabia": {"runner_id": 32970098799700081, "candidate": "Saudi Arabia"},
                    "cape_verde":   {"runner_id": 32970098799400081, "candidate": "Cape Verde"},
                },
            },
        },
        "strategies": [
            strat
            for team in ["spain", "uruguay", "saudi_arabia", "cape_verde"]
            for strat in [
                {"type": "cross_hedge", "name": f"{team}-pm-mb", "enabled": True, "alert_only": True,
                 "back": {"provider": "polymarket", "outcome": team, "side": "yes"},
                 "lay":  {"provider": "matchbook",  "outcome": team, "side": "lay"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
                {"type": "cross_hedge", "name": f"{team}-mb-pm", "enabled": True, "alert_only": True,
                 "back": {"provider": "matchbook",  "outcome": team, "side": "back"},
                 "lay":  {"provider": "polymarket", "outcome": team, "side": "no"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
            ]
        ],
    },

    # --- Group I: France, Norway, Senegal, Iraq ---
    "wc_2026_group_i": {
        "key":         "wc_2026_group_i",
        "title":       "2026 FIFA World Cup Group I Winner",
        "resolves_by": "2026-06-27T06:00:00Z",
        "active": {"cutoff_minutes_before_resolution": 90},
        "providers": {
            "polymarket": {
                "market_id": "98330",
                "outcomes": {
                    "senegal": {"clob_token_id": "18153508030666155125947977294274736212785936906864251762750218720297370202447", "clob_token_id_no": "114214449360522971374831440200472069699209252064654778896920046363669211122020", "candidate": "Senegal"},
                    "norway":  {"clob_token_id": "36982737812609801400501176287982677104372661602083682910520606276456764515825", "clob_token_id_no": "55330357113700639161778310206196430456387221997458437489478387897057084776974",  "candidate": "Norway"},
                    "france":  {"clob_token_id": "103359786443267486482227328692138374862249745048615687484182109062176862247011","clob_token_id_no": "86999834375646169233774441347346610387444272178383678745343987016849228425669",  "candidate": "France"},
                    "iraq":    {"clob_token_id": "1530571385266372090851504346844459172584141813027421133450144340832000423348",   "clob_token_id_no": "76740372668344733061461244146487800941680440438688184161097059306818011164889",  "candidate": "Iraq"},
                },
            },
            "matchbook": {
                "event_id":  32969998552800081,
                "market_id": 32970104292400081,
                "outcomes": {
                    "france":  {"runner_id": 32970104292501081, "candidate": "France"},
                    "norway":  {"runner_id": 32970104293001081, "candidate": "Norway"},
                    "senegal": {"runner_id": 32970104292801081, "candidate": "Senegal"},
                    "iraq":    {"runner_id": 32970104293200081, "candidate": "Iraq"},
                },
            },
        },
        "strategies": [
            strat
            for team in ["france", "norway", "senegal", "iraq"]
            for strat in [
                {"type": "cross_hedge", "name": f"{team}-pm-mb", "enabled": True, "alert_only": True,
                 "back": {"provider": "polymarket", "outcome": team, "side": "yes"},
                 "lay":  {"provider": "matchbook",  "outcome": team, "side": "lay"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
                {"type": "cross_hedge", "name": f"{team}-mb-pm", "enabled": True, "alert_only": True,
                 "back": {"provider": "matchbook",  "outcome": team, "side": "back"},
                 "lay":  {"provider": "polymarket", "outcome": team, "side": "no"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
            ]
        ],
    },

    # --- Group J: Argentina, Austria, Algeria, Jordan ---
    "wc_2026_group_j": {
        "key":         "wc_2026_group_j",
        "title":       "2026 FIFA World Cup Group J Winner",
        "resolves_by": "2026-06-27T06:00:00Z",
        "active": {"cutoff_minutes_before_resolution": 90},
        "providers": {
            "polymarket": {
                "market_id": "98336",
                "outcomes": {
                    "algeria":   {"clob_token_id": "92164182491510182310123495903380549553913472263313307350658405852689253843391", "clob_token_id_no": "20250684096007875644251497111644492561215459150615964587962176988023724813370", "candidate": "Algeria"},
                    "jordan":    {"clob_token_id": "52413286178114758645448140096119981030884770320180436755705846910483084566311", "clob_token_id_no": "102589401009614242647686356774156681575251243854225046029003165960030251498812","candidate": "Jordan"},
                    "argentina": {"clob_token_id": "29310968984111157045877690958171436580080894040094166930240964278064848697878", "clob_token_id_no": "14986855929669532981405120064288599371320706936997762830537494362351025840061", "candidate": "Argentina"},
                    "austria":   {"clob_token_id": "67790554874698491204541029155141537306085275981394053217859385972888342190825", "clob_token_id_no": "11804686755681210134021055046573258476701256016028452984245414070228607489086", "candidate": "Austria"},
                },
            },
            "matchbook": {
                "event_id":  32970001026200081,
                "market_id": 32970108519300081,
                "outcomes": {
                    "argentina": {"runner_id": 32970108519600081, "candidate": "Argentina"},
                    "austria":   {"runner_id": 32970108520201081, "candidate": "Austria"},
                    "algeria":   {"runner_id": 32970108520000081, "candidate": "Algeria"},
                    "jordan":    {"runner_id": 32970108520501081, "candidate": "Jordan"},
                },
            },
        },
        "strategies": [
            strat
            for team in ["argentina", "austria", "algeria", "jordan"]
            for strat in [
                {"type": "cross_hedge", "name": f"{team}-pm-mb", "enabled": True, "alert_only": True,
                 "back": {"provider": "polymarket", "outcome": team, "side": "yes"},
                 "lay":  {"provider": "matchbook",  "outcome": team, "side": "lay"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
                {"type": "cross_hedge", "name": f"{team}-mb-pm", "enabled": True, "alert_only": True,
                 "back": {"provider": "matchbook",  "outcome": team, "side": "back"},
                 "lay":  {"provider": "polymarket", "outcome": team, "side": "no"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
            ]
        ],
    },

    # --- Group K: Portugal, Colombia, Congo DR, Uzbekistan ---
    "wc_2026_group_k": {
        "key":         "wc_2026_group_k",
        "title":       "2026 FIFA World Cup Group K Winner",
        "resolves_by": "2026-06-27T06:00:00Z",
        "active": {"cutoff_minutes_before_resolution": 90},
        "providers": {
            "polymarket": {
                "market_id": "98337",
                "outcomes": {
                    "colombia":   {"clob_token_id": "80431988549118286984274941029124469538511077088999854826157671654195362803995", "clob_token_id_no": "18358448860478111951920431414684369103385677299011822433247781648138972487371", "candidate": "Colombia"},
                    "congo_dr":   {"clob_token_id": "83735225718772529626222453758023253699050591147484336983600542757792786332719", "clob_token_id_no": "105786121747891327800633879530647189660758582629852873544012829465029304891767","candidate": "Congo DR"},
                    "portugal":   {"clob_token_id": "45899170820661883713531815084207403901586412008642366895662984540247439050281", "clob_token_id_no": "80019845119788933927006902079471678997297334567889206120042429199211148033203", "candidate": "Portugal"},
                    "uzbekistan": {"clob_token_id": "78007745983222430194177074008120390540613661826866832475524028914349293584445", "clob_token_id_no": "113829786023912198068689023780477791119776749939242163247015828008819258884630","candidate": "Uzbekistan"},
                },
            },
            "matchbook": {
                "event_id":  32970003450600081,
                "market_id": 32970112502400081,
                "outcomes": {
                    "portugal":   {"runner_id": 32970112502501081, "candidate": "Portugal"},
                    "colombia":   {"runner_id": 32970112502800081, "candidate": "Colombia"},
                    "congo_dr":   {"runner_id": 32970112503000081, "candidate": "DR Congo"},
                    "uzbekistan": {"runner_id": 32970112503200081, "candidate": "Uzbekistan"},
                },
            },
        },
        "strategies": [
            strat
            for team in ["portugal", "colombia", "congo_dr", "uzbekistan"]
            for strat in [
                {"type": "cross_hedge", "name": f"{team}-pm-mb", "enabled": True, "alert_only": True,
                 "back": {"provider": "polymarket", "outcome": team, "side": "yes"},
                 "lay":  {"provider": "matchbook",  "outcome": team, "side": "lay"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
                {"type": "cross_hedge", "name": f"{team}-mb-pm", "enabled": True, "alert_only": True,
                 "back": {"provider": "matchbook",  "outcome": team, "side": "back"},
                 "lay":  {"provider": "polymarket", "outcome": team, "side": "no"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
            ]
        ],
    },

    # --- Group L: England, Croatia, Ghana, Panama ---
    "wc_2026_group_l": {
        "key":         "wc_2026_group_l",
        "title":       "2026 FIFA World Cup Group L Winner",
        "resolves_by": "2026-06-27T06:00:00Z",
        "active": {"cutoff_minutes_before_resolution": 90},
        "providers": {
            "polymarket": {
                "market_id": "98338",
                "outcomes": {
                    "england": {"clob_token_id": "87113768620206876753087644287667053800414777481920442542213378393626446563787", "clob_token_id_no": "107843883431193876012497376122649746860880620389547110974518836305649220988021","candidate": "England"},
                    "ghana":   {"clob_token_id": "44400075833588942587984997910267756070075846193993336198441637885660662007680", "clob_token_id_no": "9150003469462845352065749484647133997624745938591363706957166266170584162700",  "candidate": "Ghana"},
                    "croatia": {"clob_token_id": "75601855683297294632958373706544822786837095388074071336382755968953372325386", "clob_token_id_no": "32431710281840016927754764056612238056353636764548361853840304676223308679430", "candidate": "Croatia"},
                    "panama":  {"clob_token_id": "87397524278336612817723950490394422078526268266201563400045689474649715961986", "clob_token_id_no": "28234505452866310763577502279951644033851146467187271444506018691757817176941", "candidate": "Panama"},
                },
            },
            "matchbook": {
                "event_id":  32970005793500081,
                "market_id": 32970117404500081,
                "outcomes": {
                    "england": {"runner_id": 32970117404900081, "candidate": "England"},
                    "croatia": {"runner_id": 32970117405101081, "candidate": "Croatia"},
                    "ghana":   {"runner_id": 32970117405400081, "candidate": "Ghana"},
                    "panama":  {"runner_id": 32970117405600081, "candidate": "Panama"},
                },
            },
        },
        "strategies": [
            strat
            for team in ["england", "croatia", "ghana", "panama"]
            for strat in [
                {"type": "cross_hedge", "name": f"{team}-pm-mb", "enabled": True, "alert_only": True,
                 "back": {"provider": "polymarket", "outcome": team, "side": "yes"},
                 "lay":  {"provider": "matchbook",  "outcome": team, "side": "lay"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
                {"type": "cross_hedge", "name": f"{team}-mb-pm", "enabled": True, "alert_only": True,
                 "back": {"provider": "matchbook",  "outcome": team, "side": "back"},
                 "lay":  {"provider": "polymarket", "outcome": team, "side": "no"},
                 "min_edge_pct": 1.0, "risk_class": "cross_platform"},
            ]
        ],
    },

    # =========================================================================
    # 2026 FIFA World Cup Winner
    # =========================================================================
    # Polymarket slug: 2026-fifa-world-cup-winner-595 (event_id=30615)
    # Matchbook: FIFA World Cup 2026 (event_id=30750999688900058, market_id=30751007402900058)
    # Final: Sunday 19 July 2026.
    #
    # 48 teams on both books.  Italy and Peru are Polymarket-only (did not qualify).
    # Name normalisations:
    #   turkiye        — PM "Turkiye"          / MB "Turkey"
    #   congo_dr       — PM "Congo DR"         / MB "DR Congo"
    #   bosnia_herzegovina — PM "Bosnia-Herzegovina" / MB "Bosnia & Herzegovina"
    "fifa_world_cup_2026": {
        "key":         "fifa_world_cup_2026",
        "title":       "2026 FIFA World Cup Winner",
        "resolves_by": "2026-07-20T00:00:00Z",   # Polymarket endDate; final is 19 Jul

        "active": {
            "cutoff_minutes_before_resolution": 120,
        },

        "providers": {
            "polymarket": {
                "market_id": "30615",
                "outcomes": {
                    "spain": {
                        "clob_token_id":    "4394372887385518214471608448209527405727552777602031099972143344338178308080",
                        "clob_token_id_no": "112680630004798425069810935278212000865453267506345451433803052322987302357330",
                        "candidate": "Spain",
                    },
                    "france": {
                        "clob_token_id":    "108233603819467706476318984012158651931658302669301887462181073562758483842092",
                        "clob_token_id_no": "32270411694523539495262303868629477861017829722282576458031815333486368239544",
                        "candidate": "France",
                    },
                    "england": {
                        "clob_token_id":    "115556263888245616435851357148058235707004733438163639091106356867234218207169",
                        "clob_token_id_no": "77121637225348873006259930776623502125079210522997384841464684944292365296940",
                        "candidate": "England",
                    },
                    "brazil": {
                        "clob_token_id":    "27576533317283401577758999384642760405921738493660383550832555714312627457443",
                        "clob_token_id_no": "52986718774908357330412653486471347449818893503063830313445318937088822580057",
                        "candidate": "Brazil",
                    },
                    "argentina": {
                        "clob_token_id":    "18812649149814341758733697580460697418474693998558159483117100240528657629879",
                        "clob_token_id_no": "115428153746996892211798999366308897078723117634059783423375188043903703749062",
                        "candidate": "Argentina",
                    },
                    "portugal": {
                        "clob_token_id":    "45415751658241142530386585138386640503488308219341470020075667342738719018629",
                        "clob_token_id_no": "31940783580344558651011323787577288681658737625185216525249046282994042503801",
                        "candidate": "Portugal",
                    },
                    "germany": {
                        "clob_token_id":    "81739002353269632749850710185641576213562066971072676369728657545679630163887",
                        "clob_token_id_no": "45484070731786948288366703334552551439356529561722304542938873238430842810537",
                        "candidate": "Germany",
                    },
                    "netherlands": {
                        "clob_token_id":    "55935183786009449883683540312350046975246300613283087403691731856990327029236",
                        "clob_token_id_no": "103711573894614472510743687764792452240919804104728889027222697502832804498206",
                        "candidate": "Netherlands",
                    },
                    "norway": {
                        "clob_token_id":    "60447443643099453130956385288904175887233107411078568881602330835010340506057",
                        "clob_token_id_no": "111538579557239934343870815626480092245052857494675784434731223739153238373070",
                        "candidate": "Norway",
                    },
                    "belgium": {
                        "clob_token_id":    "30815807067456631524510535002617106205417832891402132396713720656146245200000",
                        "clob_token_id_no": "71145888994888153292442623019750517622535407476309461406574461229898137896934",
                        "candidate": "Belgium",
                    },
                    "colombia": {
                        "clob_token_id":    "98803390175521456712653678280474920637934596234667490983228578374641217211132",
                        "clob_token_id_no": "66826965351166675155887515167306086307412332225034728589879767944935462342380",
                        "candidate": "Colombia",
                    },
                    "japan": {
                        "clob_token_id":    "19159976531313550247579355752030367100657092033093647047491459813592996250034",
                        "clob_token_id_no": "30480347120583372139084467958111617967991872806450711152133323106144105346695",
                        "candidate": "Japan",
                    },
                    "morocco": {
                        "clob_token_id":    "69910730841487615802736046038473620030754616421912831175284551372639933569112",
                        "clob_token_id_no": "64291832879722161879651094688874074984529456778901604558632306686248535158725",
                        "candidate": "Morocco",
                    },
                    "usa": {
                        "clob_token_id":    "94603648636330087039501304492699481091005420017442244191603206509188088089447",
                        "clob_token_id_no": "45270201343463663182019040935560267543606888663369415494551943549463253748361",
                        "candidate": "USA",
                    },
                    "mexico": {
                        "clob_token_id":    "22587775301869146748237913050505932485648958481571808324285560650057390882036",
                        "clob_token_id_no": "89041006475364789358805026139650677807087698981377208157664917554760333198878",
                        "candidate": "Mexico",
                    },
                    "uruguay": {
                        "clob_token_id":    "97239126062673310243763617236644392945530356142765650402171508075574679292913",
                        "clob_token_id_no": "19291692040378529618917910599727571242305935029274321291612270922648172794670",
                        "candidate": "Uruguay",
                    },
                    "ecuador": {
                        "clob_token_id":    "39971087496427056640429359043364261029374524049464674733142166279730655826181",
                        "clob_token_id_no": "76638532101043658630369484092110685720624002077285242368448475410253808399541",
                        "candidate": "Ecuador",
                    },
                    "switzerland": {
                        "clob_token_id":    "62131913648515148266463816694306031394539656598501514114816028349608560215534",
                        "clob_token_id_no": "45315272750116791836504013666029583517532908319286234834610455739871173419179",
                        "candidate": "Switzerland",
                    },
                    "turkiye": {
                        "clob_token_id":    "18704778540677036028551740861729937822696511439974613107767625408338509619395",
                        "clob_token_id_no": "79903375968759183705713252823977157971970949200427111658524625623146332835513",
                        "candidate": "Turkiye",
                    },
                    "croatia": {
                        "clob_token_id":    "106593539437032467615148553707998472829334050617128244920821917025746481184109",
                        "clob_token_id_no": "22335540631248526397385139154377717431237265005174891396662761131414559126312",
                        "candidate": "Croatia",
                    },
                    "senegal": {
                        "clob_token_id":    "32169302633723235235251659810064817019484855501133685217130365128535248672349",
                        "clob_token_id_no": "113265059897956746691843122168656761655221736394810246476379385708691102755798",
                        "candidate": "Senegal",
                    },
                    "sweden": {
                        "clob_token_id":    "41004484905556820430171783088292854654441952667499527125436634397522798168110",
                        "clob_token_id_no": "5505010946036330007136366602843685650332532780067992773464881166501235782352",
                        "candidate": "Sweden",
                    },
                    "austria": {
                        "clob_token_id":    "88168215299416146215691671077998911754346458567817860712850392736799004561327",
                        "clob_token_id_no": "88156352219650092845199724072742433369069988856568846096337560285593531555407",
                        "candidate": "Austria",
                    },
                    "scotland": {
                        "clob_token_id":    "105252206997885252352889070218074909957179496257006510170583432513037465278006",
                        "clob_token_id_no": "83036729811309811995794968105073507480036161583897760544623930912713394505713",
                        "candidate": "Scotland",
                    },
                    "qatar": {
                        "clob_token_id":    "18605216520960122093689427575806651607517827372535894526532079999408408169156",
                        "clob_token_id_no": "84553189233868837011173802287245466242361930181501918469303092803731514390253",
                        "candidate": "Qatar",
                    },
                    "canada": {
                        "clob_token_id":    "99303605181956827630838461879484468077121754034387765735989859308848389894408",
                        "clob_token_id_no": "69727421603231227453181655442677441322028725849495647325726292556190704508348",
                        "candidate": "Canada",
                    },
                    "ivory_coast": {
                        "clob_token_id":    "58374167250364215964582274356498746399676421878376948523944979542572589542202",
                        "clob_token_id_no": "70753274007979480001882804624792378182371580903023185997594096497692751054228",
                        "candidate": "Ivory Coast",
                    },
                    "paraguay": {
                        "clob_token_id":    "93165696161088512376930999170413968261015485018106746563527821398897374023845",
                        "clob_token_id_no": "64564930076241670294516403352804569921660004975706088367725793399153499601932",
                        "candidate": "Paraguay",
                    },
                    "czechia": {
                        "clob_token_id":    "35797818400757287472708740881657961304270157125643131597907636474183210188025",
                        "clob_token_id_no": "7178132479418833271220202496199802982233847400465199094850639568068936253094",
                        "candidate": "Czechia",
                    },
                    "curacao": {
                        "clob_token_id":    "69020832226510184384177497367584971770730339593583713190288186699694495509961",
                        "clob_token_id_no": "57880090995762041847706075076337915070476459103836011898392946953962001410644",
                        "candidate": "Curacao",
                    },
                    "egypt": {
                        "clob_token_id":    "30499731947464516579580181356221397335865912996104577000510883912653418218808",
                        "clob_token_id_no": "65652065262666433168143828460188123377751865571317729312655170081534213236077",
                        "candidate": "Egypt",
                    },
                    "jordan": {
                        "clob_token_id":    "98686749402500678753487703372528277029342097490180026723487433517076969282825",
                        "clob_token_id_no": "64799211284650435818319700973089994510508551533538430983091938324711590775445",
                        "candidate": "Jordan",
                    },
                    "south_korea": {
                        "clob_token_id":    "80724786407275266937534613008558715581084712230616856739273522348302669402554",
                        "clob_token_id_no": "100966306057435164439067339228624332754270252294302540206756310699254942177976",
                        "candidate": "South Korea",
                    },
                    "algeria": {
                        "clob_token_id":    "58392024727359233794992635293106675983094683080284912908526627785964160484939",
                        "clob_token_id_no": "49871252661742427315226109307954871479267113637891510501334531720454017709951",
                        "candidate": "Algeria",
                    },
                    "bosnia_herzegovina": {
                        "clob_token_id":    "89770121993255619705119104591644526712193505786928967250693522950895615785005",
                        "clob_token_id_no": "334114555297655433618918589540388457186530884312703967834539640747792887116",
                        "candidate": "Bosnia-Herzegovina",
                    },
                    "new_zealand": {
                        "clob_token_id":    "79609298644734030886284029462369514848707878622071495577618126141372199748974",
                        "clob_token_id_no": "19300733445673111152563700067702596357686293159476142927049736146587326855017",
                        "candidate": "New Zealand",
                    },
                    "ghana": {
                        "clob_token_id":    "43907673646206657865778036957293730446366626078011238443367990170655175896145",
                        "clob_token_id_no": "10398766811303655595339782235864350348828881761515262235130136230008770201075",
                        "candidate": "Ghana",
                    },
                    "australia": {
                        "clob_token_id":    "43661509251351142169227141691164122649250455115438867334436875294380701133091",
                        "clob_token_id_no": "36726371220435722462734467218102931225443889493446335046337202920930764439505",
                        "candidate": "Australia",
                    },
                    "uzbekistan": {
                        "clob_token_id":    "90538013438399246674125939147272424357773921253199632436930218305581040235987",
                        "clob_token_id_no": "105754735859803813986361714630753276237891536292440584153365249756008425623608",
                        "candidate": "Uzbekistan",
                    },
                    "haiti": {
                        "clob_token_id":    "113379015922744700109400673843380371641970914885846970213129353605968934558386",
                        "clob_token_id_no": "7452445673065716792879664309598949732608843578632923430902121866203768915742",
                        "candidate": "Haiti",
                    },
                    "congo_dr": {
                        "clob_token_id":    "87403333427856945144645806003352057704193778078820484282942507058200689996202",
                        "clob_token_id_no": "43528414195007977002195624860045129351164807876978747566236159804271175691009",
                        "candidate": "Congo DR",
                    },
                    "cape_verde": {
                        "clob_token_id":    "61595193871140044336898809781418183952441527621084596848414908595268863899573",
                        "clob_token_id_no": "51603557647605526784272094285656249935467020057355371392584885347569984808630",
                        "candidate": "Cape Verde",
                    },
                    "south_africa": {
                        "clob_token_id":    "29544965695734183971376022965555206180154533479443150154863135205600734339980",
                        "clob_token_id_no": "111245917748445356120705119950074393193967942028497826347734348592797775138872",
                        "candidate": "South Africa",
                    },
                    "panama": {
                        "clob_token_id":    "112181485529919660901332188537214992263355343785744498550605179448744432717486",
                        "clob_token_id_no": "54713157489249811305010442901837547158293079674050742517995375602657385198814",
                        "candidate": "Panama",
                    },
                    "saudi_arabia": {
                        "clob_token_id":    "23542782083949026234898323432000742558288032327930681121040136746492993951914",
                        "clob_token_id_no": "114546670306823666642195507621782951860129088864927983250949362928442688463192",
                        "candidate": "Saudi Arabia",
                    },
                    "iraq": {
                        "clob_token_id":    "53465512181802150755993130711224070738002100921790051090044528012833736167995",
                        "clob_token_id_no": "104137480799334367207764507943317769727862093918816792190533154232239547234217",
                        "candidate": "Iraq",
                    },
                    "tunisia": {
                        "clob_token_id":    "86035858416252385053603758548087253570549129844230230783932738372075949702177",
                        "clob_token_id_no": "110028171623846551687151479494325822981244001939750602839571004785511377900325",
                        "candidate": "Tunisia",
                    },
                    "iran": {
                        "clob_token_id":    "33747305042007778221968790541070114008811587676172030120559423448386310500957",
                        "clob_token_id_no": "80777593725072886250387021020386140490553855405927920539616996585797139624012",
                        "candidate": "Iran",
                    },
                },
            },
            "matchbook": {
                "event_id":  30750999688900058,
                "market_id": 30751007402900058,
                "outcomes": {
                    "spain":              {"runner_id": 30751013819900058},
                    "france":             {"runner_id": 30751017183500058},
                    "england":            {"runner_id": 30751018917000058},
                    "brazil":             {"runner_id": 30751020183900058},
                    "argentina":          {"runner_id": 30751020942400058},
                    "portugal":           {"runner_id": 30751023821300058},
                    "germany":            {"runner_id": 30751022488400058},
                    "netherlands":        {"runner_id": 30751024751400058},
                    "norway":             {"runner_id": 30751033084300058},
                    "belgium":            {"runner_id": 30751028816700058},
                    "colombia":           {"runner_id": 30751028001000058},
                    "japan":              {"runner_id": 30751035695600058},
                    "morocco":            {"runner_id": 30751034877800058},
                    "usa":                {"runner_id": 30751031899500058},
                    "mexico":             {"runner_id": 30751029571500058},
                    "uruguay":            {"runner_id": 30751027150200058},
                    "ecuador":            {"runner_id": 30751039385600058},
                    "switzerland":        {"runner_id": 30751041058500058},
                    "turkiye":            {"runner_id": 30751043392800058},   # MB name: "Turkey"
                    "croatia":            {"runner_id": 30751034095500058},
                    "senegal":            {"runner_id": 31504839195200058},
                    "sweden":             {"runner_id": 30751037720900058},
                    "austria":            {"runner_id": 30751040193600058},
                    "scotland":           {"runner_id": 31504842667000058},
                    "qatar":              {"runner_id": 31504853467700058},
                    "canada":             {"runner_id": 30751042551200058},
                    "ivory_coast":        {"runner_id": 31504851919100058},
                    "paraguay":           {"runner_id": 31382001755300061},
                    "czechia":            {"runner_id": 31924581261000045},
                    "curacao":            {"runner_id": 31924578962300045},
                    "egypt":              {"runner_id": 30751048344500058},
                    "jordan":             {"runner_id": 31382007031800061},
                    "south_korea":        {"runner_id": 31382004757800061},
                    "algeria":            {"runner_id": 30751049024900058},
                    "bosnia_herzegovina": {"runner_id": 31927654099900045},   # MB name: "Bosnia & Herzegovina"
                    "new_zealand":        {"runner_id": 31382005714800061},
                    "ghana":              {"runner_id": 31504848887200058},
                    "australia":          {"runner_id": 30751049763200058},
                    "uzbekistan":         {"runner_id": 31382007804900061},
                    "haiti":              {"runner_id": 31924587205900045},
                    "congo_dr":           {"runner_id": 31924586007600045},   # MB name: "DR Congo"
                    "cape_verde":         {"runner_id": 31504858044800058},
                    "south_africa":       {"runner_id": 31504858782300058},
                    "panama":             {"runner_id": 31924594185600045},
                    "saudi_arabia":       {"runner_id": 31504855842100058},
                    "iraq":               {"runner_id": 31924588490900045},
                    "tunisia":            {"runner_id": 31382002664300061},
                    "iran":               {"runner_id": 31382003944200061},
                },
            },
        },

        "strategies": [
            strat
            for team in [
                "spain", "france", "england", "brazil", "argentina", "portugal",
                "germany", "netherlands", "norway", "belgium", "colombia", "japan",
                "morocco", "usa", "mexico", "uruguay", "ecuador", "switzerland",
                "turkiye", "croatia", "senegal", "sweden", "austria", "scotland",
                "qatar", "canada", "ivory_coast", "paraguay", "czechia", "curacao",
                "egypt", "jordan", "south_korea", "algeria", "bosnia_herzegovina",
                "new_zealand", "ghana", "australia", "uzbekistan", "haiti",
                "congo_dr", "cape_verde", "south_africa", "panama", "saudi_arabia",
                "iraq", "tunisia", "iran",
            ]
            for strat in [
                {
                    "type":         "cross_hedge",
                    "name":         f"{team}-pm-mb",
                    "enabled":      True,
                    "alert_only":   True,
                    "back":         {"provider": "polymarket", "outcome": team, "side": "yes"},
                    "lay":          {"provider": "matchbook",  "outcome": team, "side": "lay"},
                    "min_edge_pct": 1.0,
                    "risk_class":   "cross_platform",
                },
                {
                    "type":         "cross_hedge",
                    "name":         f"{team}-mb-pm",
                    "enabled":      True,
                    "alert_only":   True,
                    "back":         {"provider": "matchbook",  "outcome": team, "side": "back"},
                    "lay":          {"provider": "polymarket", "outcome": team, "side": "no"},
                    "min_edge_pct": 1.0,
                    "risk_class":   "cross_platform",
                },
            ]
        ],
    },


    # =========================================================================
    # 2026 F1 Drivers' Championship
    # =========================================================================
    # Polymarket event 100371 (slug: 2026-f1-drivers-champion, negRisk market)
    # Matchbook event 32403876415300045 "F1 Drivers Championship"
    # Season ends with the final race; Polymarket endDate 2026-12-06.
    #
    # Placeholder outcomes ("Driver A"–"Driver I", "Another Driver") on Polymarket
    # have no Matchbook counterpart and are omitted from the catalog.
    # Only the 7 top contenders have MB lay quotes; the rest are back-only on MB,
    # so only the MB-back + PM-NO direction will fire for the tail drivers.
    "f1_drivers_championship_2026": {
        "key":         "f1_drivers_championship_2026",
        "title":       "2026 F1 Drivers' Championship",
        "resolves_by": "2026-12-06T00:00:00Z",

        "active": {
            "cutoff_minutes_before_resolution": 120,
        },

        "providers": {
            "polymarket": {
                "market_id": "100371",
                "outcomes": {
                    "lando_norris":      {"clob_token_id": "49363857890713679658249116526667302240522215201279074666732334459281427193397",  "clob_token_id_no": "92810961400014685661454592832771984088831290167084620635197423420158788055855",  "candidate": "Lando Norris"},
                    "oscar_piastri":     {"clob_token_id": "45035925547723382374916856041538026483546248029152755914741212781718674476988",  "clob_token_id_no": "115566215208676819623338601769630868080437681137578005512462887928091437806438", "candidate": "Oscar Piastri"},
                    "george_russell":    {"clob_token_id": "93099562462257249309605734586566434036442082827152130023015985868027704959821",  "clob_token_id_no": "66179493383876804270523225216910123185590092065107948827970707627138225160961",  "candidate": "George Russell"},
                    "max_verstappen":    {"clob_token_id": "1253784411034901624384838438079744067552854611268416006133878658719743556218",   "clob_token_id_no": "108879872919407003618123992310695745119360422359207656432575295100543906401921", "candidate": "Max Verstappen"},
                    "kimi_antonelli":    {"clob_token_id": "32950178421556833525068948927823594772134813180063196823389171317494746105102",  "clob_token_id_no": "113530594522634794165460425612745976368792987948987062860518413913599754643814", "candidate": "Kimi Antonelli"},
                    "charles_leclerc":   {"clob_token_id": "2709284834738258468924839192854829143750961889384406449536217899798175177890",   "clob_token_id_no": "46955778407439783468859052837985793249006652932715101652741257747909716565323",  "candidate": "Charles Leclerc"},
                    "lewis_hamilton":    {"clob_token_id": "69833456690136644541052533876758499587133457040620330400923606566331654948227",  "clob_token_id_no": "107302425155378894910136485915807504436516647414953576160717018829314861130331", "candidate": "Lewis Hamilton"},
                    "isack_hadjar":      {"clob_token_id": "740554372395562969852824732102908743873362172986366970263111332168756914258",    "clob_token_id_no": "7098002907532810701211724759641809615571533544590189777506937337388681658362",   "candidate": "Isack Hadjar"},
                    "fernando_alonso":   {"clob_token_id": "106808524115843794998672503569418644305147473603718663102162467102112041190726", "clob_token_id_no": "24394883319429306039663264000718457398189890106185871018116449375372229819354",  "candidate": "Fernando Alonso"},
                    "carlos_sainz":      {"clob_token_id": "42426899074513566315797130662195264566557574463805850375923543197752542696845",  "clob_token_id_no": "108945045801592569258419974610786649161511991147915144537532323865537362991760", "candidate": "Carlos Sainz Jr."},
                    "oliver_bearman":    {"clob_token_id": "36651695143257976296316443097895676898349243442057744955120299247443104698921",  "clob_token_id_no": "19054482362666943562273480225378531345203144018228355695430835034849325181406",  "candidate": "Oliver Bearman"},
                    "alexander_albon":   {"clob_token_id": "13029823801528169768801236200914248929410601401263829196262132988455774125707",  "clob_token_id_no": "35458733425021369518224383181183526269511353161052003336876735517491897561081",  "candidate": "Alexander Albon"},
                    "lance_stroll":      {"clob_token_id": "39133371819296059003541521092128254346605102576351704426326802558136998572706",  "clob_token_id_no": "43260208680331236347091855559160701327888583562138767948147649710953870631622",  "candidate": "Lance Stroll"},
                    "gabriel_bortoleto": {"clob_token_id": "4685949819716376522669877569719026780983708592137529477703111488178918425734",   "clob_token_id_no": "42923736542470939550570685921838090823858051754384940958313539596190529665100",  "candidate": "Gabriel Bortoleto"},
                    "valtteri_bottas":   {"clob_token_id": "47163454224938970320488874259309764809542500890687165913620315329004602524782",  "clob_token_id_no": "54819903648523033773427879935996909150765971447117202762780995176525969003013",  "candidate": "Valtteri Bottas"},
                    "sergio_perez":      {"clob_token_id": "100302969466707633402130016364096558046329364344629994410264128494752146348783", "clob_token_id_no": "98022007377949427917514310572018527503347804540666511558177601365440340216781",  "candidate": "Sergio Pérez"},
                    "arvid_lindblad":    {"clob_token_id": "64226098776561185059769533241366611985885973140193149458309872405524360027300",  "clob_token_id_no": "77423503973314105278307298935957072836743340928887609922419974229890831772870",  "candidate": "Arvid Lindblad"},
                    "franco_colapinto":  {"clob_token_id": "6928573582565267822546667761915798013669025288274552856000053706900531241559",   "clob_token_id_no": "58394629104635350677984868205770706908984914797706302088659596865729991254815",  "candidate": "Franco Colapinto"},
                    "pierre_gasly":      {"clob_token_id": "2626952234259590914555295564799878112009792928452646455961775031030757356274",   "clob_token_id_no": "36848251106993102357819279494829257932701974883969106404200355092502922813518",  "candidate": "Pierre Gasly"},
                    "nico_hulkenberg":   {"clob_token_id": "83003410514761138712737264614452116001122212044042161379084125075837068427386",  "clob_token_id_no": "78034582458652085897694090673950811469941243590325007090210923420397952167269",  "candidate": "Nico Hülkenberg"},
                    "liam_lawson":       {"clob_token_id": "12049029739674782709547394278437698405073155537994805992211273099129156266263",  "clob_token_id_no": "103862174600718979743576901161749418143790066185839837189169765006104491928832", "candidate": "Liam Lawson"},
                    "esteban_ocon":      {"clob_token_id": "6916715613784204991816353276931022246228879746737115769314596145468774983735",   "clob_token_id_no": "13103535630115884311336558023996533108266193798006949928798181628568235072214",  "candidate": "Esteban Ocon"},
                },
            },
            "matchbook": {
                "event_id":  32403876415300045,
                "market_id": 32403904762600045,
                "outcomes": {
                    "lando_norris":      {"runner_id": 32403904763600045,  "candidate": "Lando Norris"},
                    "oscar_piastri":     {"runner_id": 32403904763801045,  "candidate": "Oscar Piastri"},
                    "george_russell":    {"runner_id": 32403904762900045,  "candidate": "George Russell"},
                    "max_verstappen":    {"runner_id": 32403904763301045,  "candidate": "Max Verstappen"},
                    "kimi_antonelli":    {"runner_id": 32403904764200045,  "candidate": "Kimi Antonelli"},
                    "charles_leclerc":   {"runner_id": 32403904764600045,  "candidate": "Charles Leclerc"},
                    "lewis_hamilton":    {"runner_id": 32403904764801045,  "candidate": "Lewis Hamilton"},
                    "isack_hadjar":      {"runner_id": 32403904765001045,  "candidate": "Isack Hadjar"},
                    "fernando_alonso":   {"runner_id": 32403904764400045,  "candidate": "Fernando Alonso"},
                    "carlos_sainz":      {"runner_id": 32403904765601045,  "candidate": "Carlos Sainz"},
                    "oliver_bearman":    {"runner_id": 32403904766701045,  "candidate": "Oliver Bearman"},
                    "alexander_albon":   {"runner_id": 32403904765900045,  "candidate": "Alex Albon"},
                    "lance_stroll":      {"runner_id": 32403904765401045,  "candidate": "Lance Stroll"},
                    "gabriel_bortoleto": {"runner_id": 32403904766501045,  "candidate": "Gabriel Bortoleto"},
                    "valtteri_bottas":   {"runner_id": 32403904766901045,  "candidate": "Valtteri Bottas"},
                    "sergio_perez":      {"runner_id": 32403904767101045,  "candidate": "Sergio Perez"},
                    "arvid_lindblad":    {"runner_id": 32403904767300045,  "candidate": "Arvid Lindblad"},
                    "franco_colapinto":  {"runner_id": 32403904767700045,  "candidate": "Franco Colapinto"},
                    "pierre_gasly":      {"runner_id": 32403904765201045,  "candidate": "Pierre Gasly"},
                    "nico_hulkenberg":   {"runner_id": 32403904766100045,  "candidate": "Nico Hulkenberg"},
                    "liam_lawson":       {"runner_id": 32403904766300045,  "candidate": "Liam Lawson"},
                    "esteban_ocon":      {"runner_id": 32403904767500045,  "candidate": "Esteban Ocon"},
                },
            },
        },

        "strategies": [
            strat
            for driver in [
                "lando_norris", "oscar_piastri", "george_russell", "max_verstappen",
                "kimi_antonelli", "charles_leclerc", "lewis_hamilton", "isack_hadjar",
                "fernando_alonso", "carlos_sainz", "oliver_bearman", "alexander_albon",
                "lance_stroll", "gabriel_bortoleto", "valtteri_bottas", "sergio_perez",
                "arvid_lindblad", "franco_colapinto", "pierre_gasly", "nico_hulkenberg",
                "liam_lawson", "esteban_ocon",
            ]
            for strat in [
                {
                    "type":         "cross_hedge",
                    "name":         f"{driver}-pm-mb",
                    "enabled":      True,
                    "alert_only":   True,
                    "back":         {"provider": "polymarket", "outcome": driver, "side": "yes"},
                    "lay":          {"provider": "matchbook",  "outcome": driver, "side": "lay"},
                    "min_edge_pct": 1.0,
                    "risk_class":   "cross_platform",
                },
                {
                    "type":         "cross_hedge",
                    "name":         f"{driver}-mb-pm",
                    "enabled":      True,
                    "alert_only":   True,
                    "back":         {"provider": "matchbook",  "outcome": driver, "side": "back"},
                    "lay":          {"provider": "polymarket", "outcome": driver, "side": "no"},
                    "min_edge_pct": 1.0,
                    "risk_class":   "cross_platform",
                },
            ]
        ],
    },


    # =========================================================================
    # 2026 NBA Champion
    # =========================================================================
    # Polymarket slug: 2026-nba-champion (negRisk, endDate 2026-07-01)
    # Matchbook event 30655890156900076 "NBA Championship 2025-26"
    #            (market 30655926374800076 "Winner outright")
    #
    # Finals matchup: New York Knicks vs San Antonio Spurs — only these two
    # runners remain open on Matchbook (all other teams mathematically
    # eliminated and removed from the registry, mirroring ipl_champion_2026).
    "nba_champion_2026": {
        "key":         "nba_champion_2026",
        "title":       "2026 NBA Champion",
        "resolves_by": "2026-07-01T00:00:00Z",

        "active": {
            "cutoff_minutes_before_resolution": 60,
        },

        "providers": {
            "polymarket": {
                "market_id": "2026-nba-champion",
                "outcomes": {
                    "new_york_knicks": {
                        "clob_token_id":    "20257190540739490630509657713144742134547949967093643458458133445357169845406",
                        "clob_token_id_no": "1770840559776249239623005379825945674336282130390798724203946923853499387834",
                        "candidate": "New York Knicks",
                    },
                    "san_antonio_spurs": {
                        "clob_token_id":    "102227184035967850089766981958743064457339118173548431660886438726896222843254",
                        "clob_token_id_no": "12636035070565821048178968461063687179393834041535317885287743395873720755118",
                        "candidate": "San Antonio Spurs",
                    },
                },
            },
            "matchbook": {
                "event_id":  30655890156900076,
                "market_id": 30655926374800076,
                "outcomes": {
                    "new_york_knicks":   {"runner_id": 30655926376801076, "candidate": "New York Knicks"},
                    "san_antonio_spurs": {"runner_id": 30655926379800076, "candidate": "San Antonio Spurs"},
                },
            },
        },

        "strategies": [

            # ── Cross hedges ──────────────────────────────────────────────
            {
                "type":         "cross_hedge",
                "name":         "knicks-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "new_york_knicks", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "new_york_knicks", "side": "lay"},
                "min_edge_pct": 0.5,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "knicks-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "new_york_knicks", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "new_york_knicks", "side": "no"},
                "min_edge_pct": 0.5,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "spurs-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "san_antonio_spurs", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "san_antonio_spurs", "side": "lay"},
                "min_edge_pct": 0.5,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "spurs-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "san_antonio_spurs", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "san_antonio_spurs", "side": "no"},
                "min_edge_pct": 0.5,
                "risk_class":   "cross_platform",
            },

            # ── Cross-provider sure bets ──────────────────────────────────
            # Back one finalist on PM and the other on MB.
            # Fires when Σ(1/eff_back_odds) < 1 — guaranteed profit regardless
            # of which team wins.
            {
                "type":       "cross_provider_basket",
                "name":       "sure-bet-pm-knicks-mb-spurs",
                "enabled":    True,
                "alert_only": True,
                "legs": [
                    {"provider": "polymarket", "outcome": "new_york_knicks",   "side": "yes"},
                    {"provider": "matchbook",  "outcome": "san_antonio_spurs", "side": "back"},
                ],
                "min_edge_pct": 0.3,
                "risk_class":   "sure_bet_final",
            },
            {
                "type":       "cross_provider_basket",
                "name":       "sure-bet-mb-knicks-pm-spurs",
                "enabled":    True,
                "alert_only": True,
                "legs": [
                    {"provider": "matchbook",  "outcome": "new_york_knicks",   "side": "back"},
                    {"provider": "polymarket", "outcome": "san_antonio_spurs", "side": "yes"},
                ],
                "min_edge_pct": 0.3,
                "risk_class":   "sure_bet_final",
            },

            # ── Same-provider sure bets ───────────────────────────────────
            # With only 2 runners on MB, a same-provider basket is also viable.
            {
                "type":              "same_provider_basket",
                "name":              "sure-bet-mb-only",
                "enabled":           True,
                "alert_only":        True,
                "provider":          "matchbook",
                "side":              "back",
                "include_outcomes":  "*",
                "min_edge_pct":      0.3,
                "risk_class":        "sure_bet_final",
            },
        ],
    },

    "world_cup_nation_to_reach_final_2026": {
        "key":         "world_cup_nation_to_reach_final_2026",
        "title":       "2026 FIFA World Cup: Nation to Reach Final",
        "resolves_by": "2026-07-20T00:00:00Z",

        "active": {
            "cutoff_minutes_before_resolution": 120,
        },

        "providers": {
            "polymarket": {
                "market_id": "414457",
                "outcomes": {
                    "algeria": {
                        "clob_token_id":    "58023433617512912124644088801315799062536398338906797959623467653229911213369",
                        "clob_token_id_no": "111081295594491132008322604685101422812395135083181052232893292817497533237395",
                        "candidate": "Algeria",
                    },
                    "argentina": {
                        "clob_token_id":    "29655164621652735807988359935603921446734809810207924831871042520148753286761",
                        "clob_token_id_no": "106277373056187013173240923109453552579561228009834114848554172121019577707548",
                        "candidate": "Argentina",
                    },
                    "australia": {
                        "clob_token_id":    "53027930901936595285066521540158728681436402678545226721575273421610444403926",
                        "clob_token_id_no": "87079111576260869726989401546479201370930181525146191371616474372843294098159",
                        "candidate": "Australia",
                    },
                    "austria": {
                        "clob_token_id":    "99971116202989392506156676618829001933113056307031345501563101988141024743072",
                        "clob_token_id_no": "111276972029989915630857874053749183763576917350756042870962673995842468191188",
                        "candidate": "Austria",
                    },
                    "belgium": {
                        "clob_token_id":    "42642733841375868250019824393895616178120731186981831192119559664402455807907",
                        "clob_token_id_no": "49444756644707500998425362003350300889304966604464917505823446083561306410717",
                        "candidate": "Belgium",
                    },
                    "bosnia_and_herzegovina": {
                        "clob_token_id":    "56185919768508341781380612061207307562978721154691505770021430571090971786204",
                        "clob_token_id_no": "57761734508087223888528657201471655548073537327777678357980931705084341902851",
                        "candidate": "Bosnia and Herzegovina",
                    },
                    "brazil": {
                        "clob_token_id":    "34787230403043355128636001070710444359614063013416600771124899051091155533274",
                        "clob_token_id_no": "100782189846155998582415336350671529779387350311755570046110441762167889065246",
                        "candidate": "Brazil",
                    },
                    "canada": {
                        "clob_token_id":    "89213630165214047229558386221609640009027601327751312513236271512008084172785",
                        "clob_token_id_no": "35219295891144202437909669036174929431014547511240011696988429426464921390823",
                        "candidate": "Canada",
                    },
                    "cape_verde": {
                        "clob_token_id":    "109984296983427265249754825749207260386091408367458057875764685127642783134831",
                        "clob_token_id_no": "12757676969508312548865886208239321356857559315296008441278654881892445030742",
                        "candidate": "Cape Verde",
                    },
                    "colombia": {
                        "clob_token_id":    "87874922573280957405073053711998890628107829783535977397504480554710455392553",
                        "clob_token_id_no": "43628795712486515833828470752867039200350376777314237956256694829779429130222",
                        "candidate": "Colombia",
                    },
                    "croatia": {
                        "clob_token_id":    "85064257073656890473820528984283875469178323697052988840857974465380428386632",
                        "clob_token_id_no": "8341971819269591726160189753152871197363078367632927575332826464687011573473",
                        "candidate": "Croatia",
                    },
                    "curacao": {
                        "clob_token_id":    "100980405906988727826226574920171356802267690084340572063808617177046535582153",
                        "clob_token_id_no": "10254567185240814614780950352786054321303765195369231385160894126069536359072",
                        "candidate": "Curaçao",
                    },
                    "czechia": {
                        "clob_token_id":    "106053936361505732142390435592980671666236652113761045874815721247502983378223",
                        "clob_token_id_no": "53464679067608272408492598090417894192331397063649345459007906901992294461905",
                        "candidate": "Czechia",
                    },
                    "dr_congo": {
                        "clob_token_id":    "38593765238049325617624980356892544052773960362648072236909224701024279194392",
                        "clob_token_id_no": "6795206900630811235668878534018469100251422372023096372979658961910716119765",
                        "candidate": "DR Congo",
                    },
                    "ecuador": {
                        "clob_token_id":    "16953412027502864190756202361510442521946597707718516295943248287939092789757",
                        "clob_token_id_no": "16536262941195536921389922821958693822498686973868241520522901963206656397022",
                        "candidate": "Ecuador",
                    },
                    "egypt": {
                        "clob_token_id":    "66346760585328821223970788885418023609942201238425199156662738983300883436572",
                        "clob_token_id_no": "112974973359506608548143664025540780361883132440359371746411017497156054286624",
                        "candidate": "Egypt",
                    },
                    "england": {
                        "clob_token_id":    "38330908233013235601308136733112038101325243427323717348299316649415227409423",
                        "clob_token_id_no": "82043367453552770036549565042891005119156751175772147330420062804649379713755",
                        "candidate": "England",
                    },
                    "france": {
                        "clob_token_id":    "51277730199673248502312053152395889329467978674848674323688606017636121233913",
                        "clob_token_id_no": "67697867866538302451407026469381030994678077364956441511771093219468536072529",
                        "candidate": "France",
                    },
                    "germany": {
                        "clob_token_id":    "115534565543700247476635950134316656514715001200265236198356268750955512149656",
                        "clob_token_id_no": "21372287344369382296557653533100907490950450823593811881128531648539234031694",
                        "candidate": "Germany",
                    },
                    "ghana": {
                        "clob_token_id":    "64682656659798246959546439432299964675081675913724973628044785045505636311297",
                        "clob_token_id_no": "3767585932160006595015244432537123402436850961160328793492381218144439259733",
                        "candidate": "Ghana",
                    },
                    "haiti": {
                        "clob_token_id":    "11428379659050724150185534102916985411210097294416349786561786343915951590473",
                        "clob_token_id_no": "83684268120078151961458740752149262158082325490489960817463565133979067546099",
                        "candidate": "Haiti",
                    },
                    "iran": {
                        "clob_token_id":    "71520448369356106309659702203743311032285116161453418031635337240077681807576",
                        "clob_token_id_no": "43695260119489361259707588190837958780602202775914076582409399879011460121005",
                        "candidate": "Iran",
                    },
                    "iraq": {
                        "clob_token_id":    "29746829115115506293839049748280579976567123429883978466588635800487335160109",
                        "clob_token_id_no": "99760488960945883095508033477296485995746091089706387567946860766830295725699",
                        "candidate": "Iraq",
                    },
                    "ivory_coast": {
                        "clob_token_id":    "36535345694467529268146708146520194239009302169984600983180642477912069373515",
                        "clob_token_id_no": "16170353791584164684060429365878157221547114423848675792839714224671419027127",
                        "candidate": "Ivory Coast",
                    },
                    "japan": {
                        "clob_token_id":    "53606572080560975134009349865812502868441192516108570635253416147811273387499",
                        "clob_token_id_no": "83760029165507742955830215953951850911128587169311461743006252730105242985456",
                        "candidate": "Japan",
                    },
                    "jordan": {
                        "clob_token_id":    "57650666279885466522299250397848580797356339068987533243847940984188276171058",
                        "clob_token_id_no": "82556622212718327092678751147149269460350329200522099923450265527532760705939",
                        "candidate": "Jordan",
                    },
                    "mexico": {
                        "clob_token_id":    "44875352804270966260326725617943549165054912423727317327254824975565921809445",
                        "clob_token_id_no": "44842965987473325983162328689997819096526327013729797846376035924525793054604",
                        "candidate": "Mexico",
                    },
                    "morocco": {
                        "clob_token_id":    "89425408670207070621979825849065586507178132915358763765127245631028306353294",
                        "clob_token_id_no": "80801889117099280028443482480895970638102284219291623032551523448127403664623",
                        "candidate": "Morocco",
                    },
                    "netherlands": {
                        "clob_token_id":    "27624953300324592236801443684552670084496826416005007053414490095165179656567",
                        "clob_token_id_no": "50972679167582712257340625424178440680198413323312277729823698049753788401212",
                        "candidate": "Netherlands",
                    },
                    "new_zealand": {
                        "clob_token_id":    "19677928241970874393503278073308516675221528481835971602485826323969907930533",
                        "clob_token_id_no": "102612644223525606230936797118133309209522017654026569051730649538661170975726",
                        "candidate": "New Zealand",
                    },
                    "norway": {
                        "clob_token_id":    "23063155955271078819366528095108117847939210886304219398997160384062018779310",
                        "clob_token_id_no": "110199602853794820790013282833068204081698816986889826362383782295375654666571",
                        "candidate": "Norway",
                    },
                    "panama": {
                        "clob_token_id":    "48467828520127803325454535516288303896003718835635612582835547340950099859296",
                        "clob_token_id_no": "55220003449862231116709001510970012814951236445489598699888685913557241762983",
                        "candidate": "Panama",
                    },
                    "paraguay": {
                        "clob_token_id":    "46621999146257804167115458493072937338146881432191375143354318743998878881629",
                        "clob_token_id_no": "103926462369954243524073911236059876737268021477611455940644175049532881869050",
                        "candidate": "Paraguay",
                    },
                    "portugal": {
                        "clob_token_id":    "78512201071473916877180477359507111087054980172241483782475280980291290538026",
                        "clob_token_id_no": "96988712895070258646486668163343941795147999154112920836577540079516909289165",
                        "candidate": "Portugal",
                    },
                    "qatar": {
                        "clob_token_id":    "83717954047870157884783028818673021299949283920572315696558577356177823850211",
                        "clob_token_id_no": "51096296566680639530480202132703949866682751571587317350096518703910073001773",
                        "candidate": "Qatar",
                    },
                    "saudi_arabia": {
                        "clob_token_id":    "107958992032653784011798035489426087818947572892700370739149822258837517911510",
                        "clob_token_id_no": "114690416658705555635606688136737472901644714431193090116507578495206109052988",
                        "candidate": "Saudi Arabia",
                    },
                    "scotland": {
                        "clob_token_id":    "110399213556487580925207007168405627526348114346301541189000314036572300323545",
                        "clob_token_id_no": "85030676290239258798361637829593579107126339480137488980585884596081550975292",
                        "candidate": "Scotland",
                    },
                    "senegal": {
                        "clob_token_id":    "96379145799894008999885048827643456334848234082069090149922694945843459341574",
                        "clob_token_id_no": "68678726645854767234731496905585550437021730968560816312168987884930331178898",
                        "candidate": "Senegal",
                    },
                    "south_africa": {
                        "clob_token_id":    "98222803325146729606929277746136334327963065320713156564688955976673816492780",
                        "clob_token_id_no": "66396350877709887214500101006727269721210717613444446107014036462929919034442",
                        "candidate": "South Africa",
                    },
                    "south_korea": {
                        "clob_token_id":    "94961458492987151516073107390688592032511087599329809670331430517962676662909",
                        "clob_token_id_no": "78979136207896242490651521032490635446054825897404984835922733494642088315714",
                        "candidate": "South Korea",
                    },
                    "spain": {
                        "clob_token_id":    "101923360499625332788062329411404784738023855630916769561935447176668677624752",
                        "clob_token_id_no": "55701300041791532039905832402592735646051735123856928427727292023589207777460",
                        "candidate": "Spain",
                    },
                    "sweden": {
                        "clob_token_id":    "93560219436776722791435085087704214164737269940374160098544689418609082807170",
                        "clob_token_id_no": "93022941281382453037511009027235388028425195364979695094771613243042261313290",
                        "candidate": "Sweden",
                    },
                    "switzerland": {
                        "clob_token_id":    "2032868846159953092745022941521179604230025014125771596734795991303090319311",
                        "clob_token_id_no": "82055609672173475489519739564891907535438414764092905193080080173199234227714",
                        "candidate": "Switzerland",
                    },
                    "tunisia": {
                        "clob_token_id":    "31646809936955202084285061036611159969537040435686524494469262086433935157494",
                        "clob_token_id_no": "34471449257254026468757941188012579230492541569930431619441202311076167521362",
                        "candidate": "Tunisia",
                    },
                    "turkiye": {
                        "clob_token_id":    "32657256882453404417977065675930607606961022843268564090372216210915153968066",
                        "clob_token_id_no": "36661012700978872058243339859391331404241701234228566341825087734803698924835",
                        "candidate": "Turkiye",
                    },
                    "uruguay": {
                        "clob_token_id":    "107252965975473461077951755862490475875086384446735611728932904123764923072876",
                        "clob_token_id_no": "109116677771925395078189968735779351655189972682083529578210115007863355304327",
                        "candidate": "Uruguay",
                    },
                    "usa": {
                        "clob_token_id":    "66838617789346516409327873387385715577053478133739337062169449451151113366291",
                        "clob_token_id_no": "48820830280168451063017658468719462339882961536329058624478994051544408996133",
                        "candidate": "USA",
                    },
                    "uzbekistan": {
                        "clob_token_id":    "40940842425232073315332232262863724042587357884635453035794995995514366584786",
                        "clob_token_id_no": "91491741411514780308802671444518711033819513069186826222723366139029761136742",
                        "candidate": "Uzbekistan",
                    },
                },
            },
            "matchbook": {
                "event_id":  33392247797600023,
                "market_id": 33392352690600023,
                "outcomes": {
                    "algeria": {"runner_id": 33392352696601023, "candidate": "Algeria"},
                    "argentina": {"runner_id": 33392352691901023, "candidate": "Argentina"},
                    "australia": {"runner_id": 33392352697000023, "candidate": "Australia"},
                    "austria": {"runner_id": 33392352695301023, "candidate": "Austria"},
                    "belgium": {"runner_id": 33392352692401023, "candidate": "Belgium"},
                    "bosnia_and_herzegovina": {"runner_id": 33392753066400023, "candidate": "Bosnia & Herzegovina"},
                    "brazil": {"runner_id": 33392352692101023, "candidate": "Brazil"},
                    "canada": {"runner_id": 33392352695700023, "candidate": "Canada"},
                    "cape_verde": {"runner_id": 33392352698601023, "candidate": "Cape Verde"},
                    "colombia": {"runner_id": 33392352693601023, "candidate": "Colombia"},
                    "croatia": {"runner_id": 33392352694601023, "candidate": "Croatia"},
                    "curacao": {"runner_id": 33392352698701023, "candidate": "Curaçao"},
                    "czechia": {"runner_id": 33392352696301023, "candidate": "Czechia"},
                    "dr_congo": {"runner_id": 33392352697401023, "candidate": "DR Congo"},
                    "ecuador": {"runner_id": 33392352695001023, "candidate": "Ecuador"},
                    "egypt": {"runner_id": 33392352696101023, "candidate": "Egypt"},
                    "england": {"runner_id": 33392352691100023, "candidate": "England"},
                    "france": {"runner_id": 33392352691700023, "candidate": "France"},
                    "germany": {"runner_id": 33392352692601023, "candidate": "Germany"},
                    "ghana": {"runner_id": 33392352696500023, "candidate": "Ghana"},
                    "haiti": {"runner_id": 33392352699201023, "candidate": "Haiti"},
                    "iran": {"runner_id": 33430495834200023, "candidate": "Iran"},
                    "iraq": {"runner_id": 33392352698300023, "candidate": "Iraq"},
                    "ivory_coast": {"runner_id": 33392352696000023, "candidate": "Ivory Coast"},
                    "japan": {"runner_id": 33392352694000023, "candidate": "Japan"},
                    "jordan": {"runner_id": 33392352699100023, "candidate": "Jordan"},
                    "mexico": {"runner_id": 33392352694501023, "candidate": "Mexico"},
                    "morocco": {"runner_id": 33392352693500023, "candidate": "Morocco"},
                    "netherlands": {"runner_id": 33392352692901023, "candidate": "Netherlands"},
                    "new_zealand": {"runner_id": 33392352698101023, "candidate": "New Zealand"},
                    "norway": {"runner_id": 33392352693101023, "candidate": "Norway"},
                    "panama": {"runner_id": 33392352698401023, "candidate": "Panama"},
                    "paraguay": {"runner_id": 33392352695500023, "candidate": "Paraguay"},
                    "portugal": {"runner_id": 33392352692300023, "candidate": "Portugal"},
                    "qatar": {"runner_id": 33392352697600023, "candidate": "Qatar"},
                    "saudi_arabia": {"runner_id": 33392352697800023, "candidate": "Saudi Arabia"},
                    "scotland": {"runner_id": 33392352695801023, "candidate": "Scotland"},
                    "senegal": {"runner_id": 33392352694801023, "candidate": "Senegal"},
                    "south_africa": {"runner_id": 33392352697901023, "candidate": "South Africa"},
                    "south_korea": {"runner_id": 33392352696801023, "candidate": "South Korea"},
                    "spain": {"runner_id": 33392352691301023, "candidate": "Spain"},
                    "sweden": {"runner_id": 33392352695200023, "candidate": "Sweden"},
                    "switzerland": {"runner_id": 33392352694301023, "candidate": "Switzerland"},
                    "tunisia": {"runner_id": 33392352697101023, "candidate": "Tunisia"},
                    "turkiye": {"runner_id": 33392352699401023, "candidate": "Turkey"},
                    "uruguay": {"runner_id": 33392352694200023, "candidate": "Uruguay"},
                    "usa": {"runner_id": 33392352693801023, "candidate": "USA"},
                    "uzbekistan": {"runner_id": 33392352698901023, "candidate": "Uzbekistan"},
                },
            },
        },

        "strategies": [
            {
                "type":         "cross_hedge",
                "name":         "algeria-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "algeria", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "algeria", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "algeria-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "algeria", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "algeria", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "argentina-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "argentina", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "argentina", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "argentina-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "argentina", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "argentina", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "australia-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "australia", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "australia", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "australia-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "australia", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "australia", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "austria-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "austria", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "austria", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "austria-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "austria", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "austria", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "belgium-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "belgium", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "belgium", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "belgium-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "belgium", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "belgium", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "bosnia_and_herzegovina-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "bosnia_and_herzegovina", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "bosnia_and_herzegovina", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "bosnia_and_herzegovina-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "bosnia_and_herzegovina", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "bosnia_and_herzegovina", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "brazil-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "brazil", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "brazil", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "brazil-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "brazil", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "brazil", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "canada-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "canada", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "canada", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "canada-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "canada", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "canada", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "cape_verde-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "cape_verde", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "cape_verde", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "cape_verde-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "cape_verde", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "cape_verde", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "colombia-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "colombia", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "colombia", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "colombia-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "colombia", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "colombia", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "croatia-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "croatia", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "croatia", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "croatia-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "croatia", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "croatia", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "curacao-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "curacao", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "curacao", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "curacao-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "curacao", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "curacao", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "czechia-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "czechia", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "czechia", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "czechia-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "czechia", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "czechia", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "dr_congo-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "dr_congo", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "dr_congo", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "dr_congo-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "dr_congo", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "dr_congo", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "ecuador-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "ecuador", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "ecuador", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "ecuador-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "ecuador", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "ecuador", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "egypt-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "egypt", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "egypt", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "egypt-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "egypt", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "egypt", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "england-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "england", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "england", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "england-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "england", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "england", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "france-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "france", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "france", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "france-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "france", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "france", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "germany-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "germany", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "germany", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "germany-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "germany", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "germany", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "ghana-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "ghana", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "ghana", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "ghana-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "ghana", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "ghana", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "haiti-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "haiti", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "haiti", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "haiti-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "haiti", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "haiti", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "iran-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "iran", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "iran", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "iran-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "iran", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "iran", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "iraq-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "iraq", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "iraq", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "iraq-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "iraq", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "iraq", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "ivory_coast-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "ivory_coast", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "ivory_coast", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "ivory_coast-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "ivory_coast", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "ivory_coast", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "japan-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "japan", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "japan", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "japan-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "japan", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "japan", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "jordan-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "jordan", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "jordan", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "jordan-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "jordan", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "jordan", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "mexico-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "mexico", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "mexico", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "mexico-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "mexico", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "mexico", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "morocco-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "morocco", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "morocco", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "morocco-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "morocco", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "morocco", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "netherlands-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "netherlands", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "netherlands", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "netherlands-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "netherlands", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "netherlands", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "new_zealand-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "new_zealand", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "new_zealand", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "new_zealand-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "new_zealand", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "new_zealand", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "norway-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "norway", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "norway", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "norway-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "norway", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "norway", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "panama-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "panama", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "panama", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "panama-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "panama", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "panama", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "paraguay-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "paraguay", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "paraguay", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "paraguay-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "paraguay", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "paraguay", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "portugal-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "portugal", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "portugal", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "portugal-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "portugal", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "portugal", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "qatar-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "qatar", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "qatar", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "qatar-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "qatar", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "qatar", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "saudi_arabia-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "saudi_arabia", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "saudi_arabia", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "saudi_arabia-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "saudi_arabia", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "saudi_arabia", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "scotland-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "scotland", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "scotland", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "scotland-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "scotland", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "scotland", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "senegal-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "senegal", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "senegal", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "senegal-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "senegal", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "senegal", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "south_africa-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "south_africa", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "south_africa", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "south_africa-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "south_africa", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "south_africa", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "south_korea-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "south_korea", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "south_korea", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "south_korea-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "south_korea", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "south_korea", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "spain-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "spain", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "spain", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "spain-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "spain", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "spain", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "sweden-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "sweden", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "sweden", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "sweden-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "sweden", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "sweden", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "switzerland-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "switzerland", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "switzerland", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "switzerland-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "switzerland", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "switzerland", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "tunisia-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "tunisia", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "tunisia", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "tunisia-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "tunisia", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "tunisia", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "turkiye-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "turkiye", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "turkiye", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "turkiye-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "turkiye", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "turkiye", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "uruguay-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "uruguay", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "uruguay", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "uruguay-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "uruguay", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "uruguay", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "usa-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "usa", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "usa", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "usa-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "usa", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "usa", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "uzbekistan-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "uzbekistan", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "uzbekistan", "side": "lay"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "uzbekistan-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "uzbekistan", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "uzbekistan", "side": "no"},
                "min_edge_pct": 1.0,
                "risk_class":   "cross_platform",
            },

        ],
    },

}


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

def list_events() -> list[str]:
    """Return all registered event keys, sorted."""
    return sorted(SPECIALS_EVENTS.keys())


def get_event(key: str) -> dict[str, Any] | None:
    """Return the event entry for a given key, or None if not registered."""
    return SPECIALS_EVENTS.get(key)


def enabled_strategies(event_key: str) -> list[dict[str, Any]]:
    """Return only the strategies with enabled=True for the given event."""
    event = SPECIALS_EVENTS.get(event_key)
    if event is None:
        return []
    return [s for s in event.get("strategies", []) if s.get("enabled")]
