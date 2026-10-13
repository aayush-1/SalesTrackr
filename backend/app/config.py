import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    mode: str
    data_dir: Path
    root_id: str
    token_json: str
    iap_audience: str
    public_origin: str
    seed_demo: bool

    @classmethod
    def load(cls):
        mode = os.getenv("APP_MODE", "demo")
        if mode not in ("demo", "production"):
            raise RuntimeError("APP_MODE must be demo or production.")
        if os.getenv("K_SERVICE") and mode != "production":
            raise RuntimeError("Demo mode is forbidden on Cloud Run.")
        settings = cls(
            mode=mode,
            data_dir=Path(os.getenv("DEMO_DATA_DIR", ".local-data")),
            root_id=os.getenv("GOOGLE_FOLDER_ID", ""),
            token_json=os.getenv("GOOGLE_TOKEN_JSON", ""),
            iap_audience=os.getenv("IAP_AUDIENCE", ""),
            public_origin=os.getenv("PUBLIC_ORIGIN", "").rstrip("/"),
            seed_demo=os.getenv("DEMO_SEED", "true").lower() == "true",
        )
        if mode == "production" and not all(
            (
                settings.root_id,
                settings.token_json,
                settings.iap_audience,
                settings.public_origin,
            )
        ):
            raise RuntimeError(
                "Production requires GOOGLE_FOLDER_ID, GOOGLE_TOKEN_JSON, IAP_AUDIENCE, PUBLIC_ORIGIN."
            )
        if mode == "production" and not settings.public_origin.startswith("https://"):
            raise RuntimeError("PUBLIC_ORIGIN must be an HTTPS origin.")
        return settings
