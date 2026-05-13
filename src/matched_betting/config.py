from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


def _load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class MatchbookSettings:
    username: str | None
    password: str | None
    base_url: str


@dataclass(frozen=True)
class SmarketsSettings:
    username: str | None
    password: str | None
    api_token: str | None
    base_url: str


@dataclass(frozen=True)
class PolymarketSettings:
    gamma_base_url: str
    clob_base_url: str
    private_key: str | None
    polygon_rpc_url: str | None


@dataclass(frozen=True)
class SxBetSettings:
    base_url: str
    base_token: str
    explorer_url: str = "https://explorerl2.sx.technology/api"  # block explorer for balance queries


@dataclass(frozen=True)
class AzuroSettings:
    subgraph_url: str


@dataclass(frozen=True)
class CommissionSettings:
    matchbook: float
    smarkets: float
    sx_bet: float
    smarkets_zero_commission_period: bool


@dataclass(frozen=True)
class Settings:
    project_root: Path
    default_output_path: Path
    matchbook: MatchbookSettings
    smarkets: SmarketsSettings
    polymarket: PolymarketSettings
    sx_bet: SxBetSettings
    azuro: AzuroSettings
    commission: CommissionSettings
    vpn_proxy_url: str | None = None  # e.g. "socks5h://10.64.0.1:1080" (in-tunnel) or "socks5h://nl-ams-wg-socks5-001.relays.mullvad.net:1080" (multihop)
    alert_webhook_url: str | None = None
    alert_enabled: bool = True


def load_settings(project_root: Path) -> Settings:
    _load_dotenv(project_root / ".env")

    output_path = os.getenv("MATCHED_BETTING_OUTPUT_PATH", "outputs/latest_odds.json")
    smarkets_zero = os.getenv("SMARKETS_ZERO_COMMISSION", "true").lower() in ("true", "1", "yes")

    return Settings(
        project_root=project_root,
        default_output_path=(project_root / output_path).resolve(),
        matchbook=MatchbookSettings(
            username=os.getenv("MATCHBOOK_USERNAME") or None,
            password=os.getenv("MATCHBOOK_PASSWORD") or None,
            base_url=os.getenv("MATCHBOOK_BASE_URL", "https://api.matchbook.com"),
        ),
        smarkets=SmarketsSettings(
            username=os.getenv("SMARKETS_USERNAME") or None,
            password=os.getenv("SMARKETS_PASSWORD") or None,
            api_token=os.getenv("SMARKETS_API_TOKEN") or None,
            base_url=os.getenv("SMARKETS_BASE_URL", "https://api.smarkets.com"),
        ),
        polymarket=PolymarketSettings(
            gamma_base_url=os.getenv(
                "POLYMARKET_GAMMA_BASE_URL", "https://gamma-api.polymarket.com"
            ),
            clob_base_url=os.getenv(
                "POLYMARKET_CLOB_BASE_URL", "https://clob.polymarket.com"
            ),
            private_key=os.getenv("POLYMARKET_PRIVATE_KEY") or None,
            polygon_rpc_url=os.getenv("POLYGON_RPC_URL") or None,
        ),
        sx_bet=SxBetSettings(
            base_url=os.getenv("SX_BET_BASE_URL", "https://api.sx.bet"),
            # USDC on SX Network; override with SX_BET_BASE_TOKEN if needed
            base_token=os.getenv(
                "SX_BET_BASE_TOKEN", "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"
            ),
            explorer_url=os.getenv("SX_EXPLORER_URL") or "https://explorerl2.sx.technology/api",
        ),
        azuro=AzuroSettings(
            subgraph_url=os.getenv(
                "AZURO_SUBGRAPH_URL",
                "https://thegraph-1.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-data-feed-polygon",
            ),
        ),
        vpn_proxy_url=os.getenv("VPN_PROXY_URL") or None,
        alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
        alert_enabled=os.getenv("ALERT_ENABLED", "true").lower() in ("true", "1", "yes"),
        commission=CommissionSettings(
            matchbook=float(os.getenv("MATCHBOOK_COMMISSION", "0.02")),
            smarkets=0.0 if smarkets_zero else float(os.getenv("SMARKETS_COMMISSION", "0.02")),
            sx_bet=float(os.getenv("SX_BET_COMMISSION", "0.0")),
            smarkets_zero_commission_period=smarkets_zero,
        ),
    )
