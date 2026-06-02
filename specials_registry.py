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
    # 2026 Women's French Open Winner
    # =========================================================================
    # Polymarket slug: 2026-womens-french-open-winner (event_id=139065, negRisk=True)
    # Matchbook: Women's French Open 2026 - Winner
    #            (event_id=33336461304200023, market_id=33336483472700023)
    # Women's final: Saturday 6 June 2026.
    #
    # 16 players appear on both books.
    # Name normalisations:
    #   iga_swiatek — PM "Iga Świątek" / MB "Iga Swiatek"
    "womens_french_open_2026": {
        "key":         "womens_french_open_2026",
        "title":       "2026 Women's French Open Winner",
        "resolves_by": "2026-06-06T22:00:00Z",

        "active": {
            "cutoff_minutes_before_resolution": 120,
        },

        "providers": {
            "polymarket": {
                "market_id": "139065",
                "outcomes": {
                    "iga_swiatek":       {"clob_token_id": "88603054819378546305547499774257067568154168540584244303424965367310627732749",  "clob_token_id_no": "15794969977654606723096885621240038850729723189965749889983509152879578313656",  "candidate": "Iga Swiatek"},
                    "aryna_sabalenka":   {"clob_token_id": "16275896169094661804528410927491126903724662221757491879878026518825462794524",  "clob_token_id_no": "96451020867181181948758970516523380407164375139905734140739541396473194590510",  "candidate": "Aryna Sabalenka"},
                    "coco_gauff":        {"clob_token_id": "7483136985335965995944848781720973540564435654392716537856679121636388306913",   "clob_token_id_no": "84843893658417641437437293879561550183758700156586423114073477776920895362992",  "candidate": "Coco Gauff"},
                    "mirra_andreeva":    {"clob_token_id": "65864993112088117121032639786797460433742834296100112599678569104066469817216",  "clob_token_id_no": "100740021649651476717322834445625805591528301476372566374502876626372742904474", "candidate": "Mirra Andreeva"},
                    "elina_svitolina":   {"clob_token_id": "115015682815694243043174278571532477417946682697988407042205590190645621816754", "clob_token_id_no": "6928743393168143228150946345275241936101555942620142830138552729252366784594",   "candidate": "Elina Svitolina"},
                    "marta_kostyuk":     {"clob_token_id": "88170471012504008027343174919540713297923716927636988904392758801364499347537",  "clob_token_id_no": "62555337048478786613483576301367230746731868046416562500187026423413644636724",  "candidate": "Marta Kostyuk"},
                    "victoria_mboko":    {"clob_token_id": "55101251180090146939507983503675195255354360224060966087611782232423782856530",  "clob_token_id_no": "86962593398664815752989717541231148080848292663075978845956251325596157338924",  "candidate": "Victoria Mboko"},
                    "amanda_anisimova":  {"clob_token_id": "41372942602702958771517489370185127356781920208553450039412352261314645834452",  "clob_token_id_no": "3642487323018180090096881150988866418658534138430685761015429892817784818287",   "candidate": "Amanda Anisimova"},
                    "naomi_osaka":       {"clob_token_id": "308825329925338622852400019801112897883193700633944759574766050903819812776",    "clob_token_id_no": "83419845266558735048907215125900181575142725259908228260548157326854814912806",  "candidate": "Naomi Osaka"},
                    "anastasia_potapova":{"clob_token_id": "86625718762372289807189194128236107494576905932500290083552256932934260171258",  "clob_token_id_no": "88671321688014323284920204525551623567468357140465269886885183493725964216448",  "candidate": "Anastasia Potapova"},
                    "madison_keys":      {"clob_token_id": "66712950410528278085511570349734753454760643212724204597004541322930172223862",  "clob_token_id_no": "80382271476817452054215031298486351221974636665997015870133458854066438452264",  "candidate": "Madison Keys"},
                    "anna_kalinskaya":   {"clob_token_id": "43596995931644722427581680442603870320374812338629073443206215560381772929677",  "clob_token_id_no": "34146377629770950999500062522369581230608149708301257279519944577368311047484",  "candidate": "Anna Kalinskaya"},
                    "diana_shnaider":    {"clob_token_id": "59168815012231365816413102450487383576411744733553651689630262934696260878660",  "clob_token_id_no": "11184027323991933889976068519548789607792523603166172757537693378706944394504",  "candidate": "Diana Shnaider"},
                    "belinda_bencic":    {"clob_token_id": "103932920928456450247574116257289001545646674117691410364944385716802918647430", "clob_token_id_no": "6927760773458767699799751989859433899075447900822753829838983395776238690092",   "candidate": "Belinda Bencic"},
                    "daria_kasatkina":   {"clob_token_id": "9475122021719581128540230172372572584393335198637356664793686172893511417685",   "clob_token_id_no": "9212944903104358273206730598009720900396120316707294243554202102078752479691",   "candidate": "Daria Kasatkina"},
                    "maria_sakkari":     {"clob_token_id": "98318405268528987594074430379944317585086871855938177047868285291691499946350",  "clob_token_id_no": "76334332320226224302376129605000948410148032595340398296530780574109391202833",  "candidate": "Maria Sakkari"},
                },
            },
            "matchbook": {
                "event_id":  33336461304200023,
                "market_id": 33336483472700023,
                "outcomes": {
                    "iga_swiatek":        {"runner_id": 33336483472900023,  "candidate": "Iga Swiatek"},
                    "aryna_sabalenka":    {"runner_id": 33336483473201023,  "candidate": "Aryna Sabalenka"},
                    "coco_gauff":         {"runner_id": 33336483473701023,  "candidate": "Coco Gauff"},
                    "mirra_andreeva":     {"runner_id": 33336483474000023,  "candidate": "Mirra Andreeva"},
                    "elina_svitolina":    {"runner_id": 33336483474200023,  "candidate": "Elina Svitolina"},
                    "marta_kostyuk":      {"runner_id": 33336483474400023,  "candidate": "Marta Kostyuk"},
                    "victoria_mboko":     {"runner_id": 33336483475301023,  "candidate": "Victoria Mboko"},
                    "amanda_anisimova":   {"runner_id": 33336483475001023,  "candidate": "Amanda Anisimova"},
                    "naomi_osaka":        {"runner_id": 33336483475600023,  "candidate": "Naomi Osaka"},
                    "anastasia_potapova": {"runner_id": 33336483475901023,  "candidate": "Anastasia Potapova"},
                    "madison_keys":       {"runner_id": 33336483476501023,  "candidate": "Madison Keys"},
                    "anna_kalinskaya":    {"runner_id": 33336483477300023,  "candidate": "Anna Kalinskaya"},
                    "diana_shnaider":     {"runner_id": 33339796570100023,  "candidate": "Diana Shnaider"},
                    "belinda_bencic":     {"runner_id": 33339797340300023,  "candidate": "Belinda Bencic"},
                    "daria_kasatkina":    {"runner_id": 33339805267300023,  "candidate": "Daria Kasatkina"},
                    "maria_sakkari":      {"runner_id": 33339821140400023,  "candidate": "Maria Sakkari"},
                },
            },
        },

        "strategies": [
            strat
            for player in [
                "iga_swiatek", "aryna_sabalenka", "coco_gauff", "mirra_andreeva",
                "elina_svitolina", "marta_kostyuk", "victoria_mboko", "amanda_anisimova",
                "naomi_osaka", "anastasia_potapova", "madison_keys", "anna_kalinskaya",
                "diana_shnaider", "belinda_bencic", "daria_kasatkina", "maria_sakkari",
            ]
            for strat in [
                {
                    "type":         "cross_hedge",
                    "name":         f"{player}-pm-mb",
                    "enabled":      True,
                    "alert_only":   True,
                    "back":         {"provider": "polymarket", "outcome": player, "side": "yes"},
                    "lay":          {"provider": "matchbook",  "outcome": player, "side": "lay"},
                    "min_edge_pct": 1.0,
                    "risk_class":   "cross_platform",
                },
                {
                    "type":         "cross_hedge",
                    "name":         f"{player}-mb-pm",
                    "enabled":      True,
                    "alert_only":   True,
                    "back":         {"provider": "matchbook",  "outcome": player, "side": "back"},
                    "lay":          {"provider": "polymarket", "outcome": player, "side": "no"},
                    "min_edge_pct": 1.0,
                    "risk_class":   "cross_platform",
                },
            ]
        ],
    },

    # =========================================================================
    # 2026 Men's French Open Winner
    # =========================================================================
    # Polymarket slug: 2026-mens-french-open-winner (event_id=139020)
    # Matchbook: Men's French Open 2026 - Winner (event_id=33202418229600081)
    # Men's final: Sunday 7 June 2026.
    #
    # 22 players appear on both books.  Carlos Alcaraz is Polymarket-only
    # (not listed on Matchbook at time of setup).
    #
    # Strategies auto-generated for all players present on both platforms.
    # Direction 1: PM YES back + MB lay  (fires when PM misprices high vs MB)
    # Direction 2: MB back + PM NO back  (fires when MB misprices high vs PM)
    "mens_french_open_2026": {
        "key":         "mens_french_open_2026",
        "title":       "2026 Men's French Open Winner",
        "resolves_by": "2026-06-07T22:00:00Z",   # end of day after final (Paris = UTC+2)

        "active": {
            "cutoff_minutes_before_resolution": 120,
        },

        "providers": {
            "polymarket": {
                "market_id": "139020",
                "outcomes": {
                    "jannik_sinner": {
                        "clob_token_id":    "16684003019585301187556121691683382680088297222373235105315599633539481594481",
                        "clob_token_id_no": "78630665703702147442323042260825934682761220107443468388673692640534750252675",
                        "candidate": "Jannik Sinner",
                    },
                    "alexander_zverev": {
                        "clob_token_id":    "115259565230340488752829539798594464058371211765181621978428825324607974616548",
                        "clob_token_id_no": "91227079565323740489149665882935731761545965964585149183052266289715357896109",
                        "candidate": "Alexander Zverev",
                    },
                    "novak_djokovic": {
                        "clob_token_id":    "1979947410388952527068111831294959059382502792001850410092785592817381174855",
                        "clob_token_id_no": "91182766494579364754096086333986509593328990908612144980729690326885088097029",
                        "candidate": "Novak Djokovic",
                    },
                    "rafael_jodar": {
                        "clob_token_id":    "14015910437167924758966508984417091185661773411990026956449288809528753921799",
                        "clob_token_id_no": "41264387624827130215197168341956252027542167959946085068623104736291771390578",
                        "candidate": "Rafael Jodar",
                    },
                    "casper_ruud": {
                        "clob_token_id":    "39873677932135799317548958861266459868369810331442415369961308669359055665132",
                        "clob_token_id_no": "109687488329977372588961999276691632857978580647392345152502267095195233159315",
                        "candidate": "Casper Ruud",
                    },
                    "joao_fonseca": {
                        "clob_token_id":    "46086539644694970479807918237455015045032058557841816156754554096171613449623",
                        "clob_token_id_no": "25807664059134459647782665982832078789973190748198547595351734783440510664863",
                        "candidate": "Joao Fonseca",
                    },
                    "felix_auger_aliassime": {
                        "clob_token_id":    "99857742045328565758225231447461701191522066046833558311629893624861854011179",
                        "clob_token_id_no": "114938761738893737104455536886663341173117122010486817038550368237987755554986",
                        "candidate": "Felix Auger-Aliassime",
                    },
                    "stefanos_tsitsipas": {
                        "clob_token_id":    "60916320638379766249050600205245876108889557951417368099639743999488682702403",
                        "clob_token_id_no": "38863381725153713683180351334091939842560371333986592967090965744953163289168",
                        "candidate": "Stefanos Tsitsipas",
                    },
                    "jakub_mensik": {
                        "clob_token_id":    "72074265101134648258653724960747252897385986577218276434240022323653359594923",
                        "clob_token_id_no": "5321798908087425190415328002274593923964093694206412063161215169424633300250",
                        "candidate": "Jakub Mensik",
                    },
                    "learner_tien": {
                        "clob_token_id":    "32012484915669257031368808514670044756523844181202884944556432329754534325995",
                        "clob_token_id_no": "50765581157314194155634364554494928374364655405503769500502960629721097354909",
                        "candidate": "Learner Tien",
                    },
                    "tommy_paul": {
                        "clob_token_id":    "30399019950572647900802127747648122242426494585288749437711385334303213250594",
                        "clob_token_id_no": "92456572328244807803404113883330181753233746128782337923137625395305360747701",
                        "candidate": "Tommy Paul",
                    },
                    "alejandro_tabilo": {
                        "clob_token_id":    "5060481592656303108642826631889405356467363752494328352272433897772728422744",
                        "clob_token_id_no": "23458418386987673124819752694219990398269929495056094237834220563289639055823",
                        "candidate": "Alejandro Tabilo",
                    },
                    "hubert_hurkacz": {
                        "clob_token_id":    "65037840298977795104490458262837934576683857062458647504353175339296004634222",
                        "clob_token_id_no": "37946892090862413710469900683596562875200200733457886608679041782491643606441",
                        "candidate": "Hubert Hurkacz",
                    },
                    "frances_tiafoe": {
                        "clob_token_id":    "58170339742056553501517293494403487507979423233224863392402617061876029052401",
                        "clob_token_id_no": "78700181935707141891689158372466367740903428167893884272084782274709949525031",
                        "candidate": "Frances Tiafoe",
                    },
                    "flavio_cobolli": {
                        "clob_token_id":    "79662038476784187655139141202118647258070463846236734860184210627168694449989",
                        "clob_token_id_no": "57388230542554532081092902480733556416011761654906059054286796401148911659304",
                        "candidate": "Flavio Cobolli",
                    },
                    "ben_shelton": {
                        "clob_token_id":    "73263948683999323997546458549916720569990521096648617003105652858504602207932",
                        "clob_token_id_no": "48138496663829603514479178012554815697452353496189500612847760384986868637922",
                        "candidate": "Ben Shelton",
                    },
                    "alex_de_minaur": {
                        "clob_token_id":    "109645930136892656042530312585439796204404178106233565681677033285063302496841",
                        "clob_token_id_no": "34208610343349858938782860343303049028081144513864596012222589672077199288228",
                        "candidate": "Alex De Minaur",
                    },
                    "andrey_rublev": {
                        "clob_token_id":    "71583595024712856665324283713136126810489986670548537741098441131457736407778",
                        "clob_token_id_no": "104605450729895892698565746519729865397258312781126103964700524190736881299175",
                        "candidate": "Andrey Rublev",
                    },
                    "francisco_cerundolo": {
                        "clob_token_id":    "60759570301530412591281266530989286908711026488101566621636067762245167566047",
                        "clob_token_id_no": "40855355848542602811855353096080352873741598108831832080400039391682605629786",
                        "candidate": "Francisco Cerundolo",
                    },
                    "karen_khachanov": {
                        "clob_token_id":    "60572731431008278453744206835810150526182109035464783610184827511777447274155",
                        "clob_token_id_no": "46485934980467502129911019063946107136199464466185200514581853951828362661434",
                        "candidate": "Karen Khachanov",
                    },
                    "matteo_berrettini": {
                        "clob_token_id":    "52200628646060617789530947094644682629291660060539712354190112151029800529859",
                        "clob_token_id_no": "72653965813686767141380582864604587419336607166342191731159026226210782562953",
                        "candidate": "Matteo Berrettini",
                    },
                    "alex_michelsen": {
                        "clob_token_id":    "61839935745217963731428623687112868313044245749539119906791131761216454394619",
                        "clob_token_id_no": "9319843234253196798203844070362157065413847266691882883695893485103164510148",
                        "candidate": "Alex Michelsen",
                    },
                },
            },
            "matchbook": {
                "event_id":  33202418229600081,
                "market_id": 33202476880800081,
                "outcomes": {
                    "jannik_sinner":        {"runner_id": 33202476881000081},
                    "alexander_zverev":     {"runner_id": 33202476881201081},
                    "novak_djokovic":       {"runner_id": 33202476881800081},
                    "rafael_jodar":         {"runner_id": 33202476882501081},
                    "casper_ruud":          {"runner_id": 33202476882400081},
                    "joao_fonseca":         {"runner_id": 33202476882701081},
                    "felix_auger_aliassime": {"runner_id": 33202476883800081},
                    "stefanos_tsitsipas":   {"runner_id": 33202476884301081},
                    "jakub_mensik":         {"runner_id": 33202476884500081},
                    "learner_tien":         {"runner_id": 33202476886100081},
                    "tommy_paul":           {"runner_id": 33202476885901081},
                    "alejandro_tabilo":     {"runner_id": 33202476887301081},
                    "hubert_hurkacz":       {"runner_id": 33202476886901081},
                    "frances_tiafoe":       {"runner_id": 33202476888201081},
                    "flavio_cobolli":       {"runner_id": 33202476883100081},
                    "ben_shelton":          {"runner_id": 33202476883201081},
                    "alex_de_minaur":       {"runner_id": 33202476883600081},
                    "andrey_rublev":        {"runner_id": 33202476883901081},
                    "francisco_cerundolo":  {"runner_id": 33202476885201081},
                    "karen_khachanov":      {"runner_id": 33202476885501081},
                    "matteo_berrettini":    {"runner_id": 33202476885701081},
                    "alex_michelsen":       {"runner_id": 33202476888001081},
                },
            },
        },

        "strategies": [
            strat
            for player in [
                "jannik_sinner", "alexander_zverev", "novak_djokovic", "rafael_jodar",
                "casper_ruud", "joao_fonseca", "felix_auger_aliassime", "stefanos_tsitsipas",
                "jakub_mensik", "learner_tien", "tommy_paul", "alejandro_tabilo",
                "hubert_hurkacz", "frances_tiafoe", "flavio_cobolli", "ben_shelton",
                "alex_de_minaur", "andrey_rublev", "francisco_cerundolo", "karen_khachanov",
                "matteo_berrettini", "alex_michelsen",
            ]
            for strat in [
                {
                    "type":         "cross_hedge",
                    "name":         f"{player}-pm-mb",
                    "enabled":      True,
                    "alert_only":   True,
                    "back":         {"provider": "polymarket", "outcome": player, "side": "yes"},
                    "lay":          {"provider": "matchbook",  "outcome": player, "side": "lay"},
                    "min_edge_pct": 1.0,
                    "risk_class":   "cross_platform",
                },
                {
                    "type":         "cross_hedge",
                    "name":         f"{player}-mb-pm",
                    "enabled":      True,
                    "alert_only":   True,
                    "back":         {"provider": "matchbook",  "outcome": player, "side": "back"},
                    "lay":          {"provider": "polymarket", "outcome": player, "side": "no"},
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
    # 2026 IPL Champion
    # =========================================================================
    # Polymarket slug: 2026-ipl-champion (negRisk, endDate 2026-05-31)
    # Matchbook event 32616570276400045 "Indian Premier League - Winner"
    # Final on 2026-05-31: Royal Challengers Bengaluru vs Gujarat Titans.
    # Only two teams remain — enables both cross hedges and cross-provider
    # sure-bet baskets (back RCB on one platform + GT on the other).
    #
    # MB has lay quotes for both finalists; all other teams are excluded from
    # the registry as they are mathematically eliminated.
    "ipl_champion_2026": {
        "key":         "ipl_champion_2026",
        "title":       "2026 IPL Champion",
        "resolves_by": "2026-05-31T20:00:00Z",   # final expected to finish ~20:00 UTC

        "active": {
            "cutoff_minutes_before_resolution": 60,
        },

        "providers": {
            "polymarket": {
                "market_id": "2026-ipl-champion",
                "outcomes": {
                    "royal_challengers_bengaluru": {
                        "clob_token_id":    "78489029628428171560629478176830507283782902711569001590212217854909060316276",
                        "clob_token_id_no": "61180451104713524934141103157351662251889250238167886858569783096844647164714",
                        "candidate": "Royal Challengers Bengaluru",
                    },
                    "gujarat_titans": {
                        "clob_token_id":    "29681638867490911167690573050913031383655640364491338575196153371741176054013",
                        "clob_token_id_no": "87923764095661561994181960036551013442816670161639373435284123748693042592712",
                        "candidate": "Gujarat Titans",
                    },
                },
            },
            "matchbook": {
                "event_id":  32616570276400045,
                "market_id": 32616585419900045,
                "outcomes": {
                    "royal_challengers_bengaluru": {"runner_id": 32616585422301045, "candidate": "Royal Challengers Bengaluru"},
                    "gujarat_titans":              {"runner_id": 32616585420900045, "candidate": "Gujarat Titans"},
                },
            },
        },

        "strategies": [

            # ── Cross hedges ──────────────────────────────────────────────
            {
                "type":         "cross_hedge",
                "name":         "rcb-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "royal_challengers_bengaluru", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "royal_challengers_bengaluru", "side": "lay"},
                "min_edge_pct": 0.5,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "rcb-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "royal_challengers_bengaluru", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "royal_challengers_bengaluru", "side": "no"},
                "min_edge_pct": 0.5,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "gt-pm-mb",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "polymarket", "outcome": "gujarat_titans", "side": "yes"},
                "lay":          {"provider": "matchbook",  "outcome": "gujarat_titans", "side": "lay"},
                "min_edge_pct": 0.5,
                "risk_class":   "cross_platform",
            },
            {
                "type":         "cross_hedge",
                "name":         "gt-mb-pm",
                "enabled":      True,
                "alert_only":   True,
                "back":         {"provider": "matchbook",  "outcome": "gujarat_titans", "side": "back"},
                "lay":          {"provider": "polymarket", "outcome": "gujarat_titans", "side": "no"},
                "min_edge_pct": 0.5,
                "risk_class":   "cross_platform",
            },

            # ── Cross-provider sure bets ──────────────────────────────────
            # Back one finalist on PM and the other on MB.
            # Fires when Σ(1/eff_back_odds) < 1 — guaranteed profit regardless
            # of which team wins.
            {
                "type":       "cross_provider_basket",
                "name":       "sure-bet-pm-rcb-mb-gt",
                "enabled":    True,
                "alert_only": True,
                "legs": [
                    {"provider": "polymarket", "outcome": "royal_challengers_bengaluru", "side": "yes"},
                    {"provider": "matchbook",  "outcome": "gujarat_titans",              "side": "back"},
                ],
                "min_edge_pct": 0.3,
                "risk_class":   "sure_bet_final",
            },
            {
                "type":       "cross_provider_basket",
                "name":       "sure-bet-mb-rcb-pm-gt",
                "enabled":    True,
                "alert_only": True,
                "legs": [
                    {"provider": "matchbook",  "outcome": "royal_challengers_bengaluru", "side": "back"},
                    {"provider": "polymarket", "outcome": "gujarat_titans",              "side": "yes"},
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
