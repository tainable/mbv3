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
    max_requests_per_min: int = 400  # hard limit is 700; keep headroom below it


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
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass(frozen=True)
class SxBetSettings:
    base_url: str
    base_token: str
    explorer_url: str = "https://explorerl2.sx.technology/api"  # block explorer for balance queries
    api_key: str | None = None
    realtime_url: str = "wss://realtime.sx.bet/connection/websocket"
    realtime_token_url: str = "https://api.sx.bet/user/realtime-token/api-key"


@dataclass(frozen=True)
class AzuroSettings:
    api_url: str
    environment: str = "PolygonUSDT"


@dataclass(frozen=True)
class KellySettings:
    enabled: bool
    low_profit: float
    high_profit: float
    low_fraction: float
    high_fraction: float
    max_stake_usdc: float
    min_stake_usdc: float
    min_bankroll_usdc: float
    # SX/PM-specific kinked curve: steeper ramp once profit clears the bridge fee
    sx_pm_kink_profit: float   # kink point = bridge fee threshold (default 0.44%)
    sx_pm_kink_fraction: float # fraction at kink (default 0.15)
    sx_pm_high_fraction: float # fraction at high_profit for sx/pm arbs (default 0.40)


@dataclass(frozen=True)
class RebalancerSettings:
    enabled: bool
    min_balance_usdc: float


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
    kelly: KellySettings = None  # type: ignore[assignment]  populated by load_settings
    rebalancer: RebalancerSettings = None  # type: ignore[assignment]  populated by load_settings


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
            max_requests_per_min=int(os.getenv("MATCHBOOK_MAX_REQUESTS_PER_MIN", "400")),
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
            api_key=os.getenv("SX_BET_API_KEY") or None,
        ),
        azuro=AzuroSettings(
            api_url=os.getenv(
                "AZURO_API_URL",
                "https://api.onchainfeed.org/api/v1/public",
            ),
            environment=os.getenv("AZURO_ENVIRONMENT", "PolygonUSDT"),
        ),
        vpn_proxy_url=os.getenv("VPN_PROXY_URL") or None,
        alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
        alert_enabled=os.getenv("ALERT_ENABLED", "true").lower() in ("true", "1", "yes"),
        kelly=KellySettings(
            enabled=os.getenv("KELLY_ENABLED", "true").lower() in ("true", "1", "yes"),
            low_profit=float(os.getenv("KELLY_LOW_PROFIT", "0.2")),
            high_profit=float(os.getenv("KELLY_HIGH_PROFIT", "1.5")),
            low_fraction=float(os.getenv("KELLY_LOW_FRACTION", "0.10")),
            high_fraction=float(os.getenv("KELLY_HIGH_FRACTION", "0.25")),
            max_stake_usdc=float(os.getenv("MAX_STAKE_USDC", "200")),
            min_stake_usdc=float(os.getenv("MIN_STAKE_USDC", "2")),
            min_bankroll_usdc=float(os.getenv("MIN_BANKROLL_USDC", "20")),
            sx_pm_kink_profit=float(os.getenv("SX_PM_KINK_PROFIT", "0.44")),
            sx_pm_kink_fraction=float(os.getenv("SX_PM_KINK_FRACTION", "0.15")),
            sx_pm_high_fraction=float(os.getenv("SX_PM_HIGH_FRACTION", "0.40")),
        ),
        commission=CommissionSettings(
            matchbook=float(os.getenv("MATCHBOOK_COMMISSION", "0.02")),
            smarkets=0.0 if smarkets_zero else float(os.getenv("SMARKETS_COMMISSION", "0.02")),
            sx_bet=float(os.getenv("SX_BET_COMMISSION", "0.0")),
            smarkets_zero_commission_period=smarkets_zero,
        ),
        rebalancer=RebalancerSettings(
            enabled=os.getenv("REBALANCER_ENABLED", "true").lower() in ("true", "1", "yes"),
            min_balance_usdc=float(os.getenv("MIN_BALANCE_USDC", "10")),
        ),
    )
