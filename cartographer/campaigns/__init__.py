"""One catalog of every campaign the cartographer can run.

`registry.REGISTRY` names each campaign `<province>.<stressor>` (e.g.
`II.plan_length`, `IV.traps`) and maps every older name -- /launch presets,
`scripts/run_all_provinces.py` stages, ticket rays -- onto exactly one id.
`tiers.TIERS` is the single evidence dial (scout / standard / rigorous).
`consistency.run_consistency_campaign` is the pass^k reliability campaign.
"""

# Lazy re-exports, so `python3 -m cartographer.campaigns.registry` does not
# import the module twice (runpy warns when the package pre-imports it).
_EXPORTS = {
    "REGISTRY": "registry", "CampaignSpec": "registry", "by_province": "registry",
    "next_run_id": "registry", "resolve": "registry",
    "TIERS": "tiers", "Tier": "tiers", "apply_tier": "tiers",
}


def __getattr__(name: str):
    if name in _EXPORTS:
        import importlib

        return getattr(importlib.import_module(f".{_EXPORTS[name]}", __name__), name)
    raise AttributeError(name)


__all__ = ["REGISTRY", "CampaignSpec", "by_province", "next_run_id", "resolve", "TIERS", "Tier", "apply_tier"]
