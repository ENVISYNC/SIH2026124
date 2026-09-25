"""Runtime configuration. Every tuning knob from CLAUDE.md's confirmation logic
lives here so thresholds can be changed without touching the SQL."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="UIP_", extra="ignore")

    database_url: str = "postgresql://uip:uip@localhost:5432/uip"

    #: shared bearer token for bus clients (stands in for the full design's device auth)
    ingest_token: str = "dev-token-change-me"

    # -- pothole confirmation ----------------------------------------------------------
    #: 20 m, not 15: GPS error plus the defect sitting ahead of the bus
    pothole_radius_m: float = 20.0
    pothole_window_days: int = 30
    #: 3, not 2, because the repeated-pass fallback below covers the low-frequency
    #: routes that a bar of 3 would otherwise strand. Measured on the 24 h Pune
    #: dataset: 3-buses alone finds 3/8 real defects; 3-buses + fallback finds 8/8
    #: with zero false positives, while 2-buses lets 10 false clusters through.
    pothole_min_confirmations: int = 3
    #: repeat reports from the SAME bus at the same spot inside this window do not count
    pothole_cooldown_minutes: int = 60
    #: Low-frequency-route fallback. A route served by one or two buses can never reach
    #: the distinct-bus threshold, so a pothole there would stay pending forever. One
    #: bus passing the same spot on many separate runs IS evidence - each pass is an
    #: independent look - it is just weaker than independent buses, so the bar is
    #: higher and the reports must span time. Both conditions must hold.
    pothole_single_bus_min_passes: int = 4
    #: reports must span at least this long, so a transient (a parked truck's shadow,
    #: a wet patch) seen repeatedly in one afternoon cannot confirm itself
    pothole_single_bus_min_span_hours: int = 6

    #: distinct buses that must pass the spot reporting nothing before it is 'resolved'
    pothole_resolve_passes: int = 3
    #: a pothole is only eligible for resolution once this stale
    pothole_resolve_after_days: int = 7

    # -- waterlogging confirmation -----------------------------------------------------
    waterlogging_radius_m: float = 20.0
    #: HOURS, not days - a single rain event. A long window would merge unrelated floods.
    waterlogging_window_hours: int = 3
    waterlogging_min_confirmations: int = 2
    waterlogging_cooldown_minutes: int = 30
    #: a confirmed patch goes stale this long after its last fresh report
    waterlogging_expire_after_hours: int = 6

    # -- congestion aggregation --------------------------------------------------------
    congestion_grid_m: int = 100
    congestion_bucket_minutes: int = 5

    # -- incidents ---------------------------------------------------------------------
    #: below this an incident is not rendered; there is no multi-report confirmation
    incident_min_confidence: float = 0.7

    #: Two confirmed clusters are merged when at least this many raw reports fall
    #: inside the confirmation radius of BOTH - i.e. the same evidence supports both,
    #: so they are one defect that fragmented. Distance alone cannot decide this:
    #: fragments sit 8-40 m apart depending on report scatter, and any threshold wide
    #: enough to catch them would also swallow genuinely distinct nearby potholes.
    cluster_merge_min_shared: int = 1

    #: how often the background reaper demotes stale potholes / expires waterlogging
    maintenance_interval_seconds: int = 300


settings = Settings()
