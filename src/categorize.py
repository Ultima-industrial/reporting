import yaml

from .config import CONFIG_DIR


def _load_yaml(name):
    path = CONFIG_DIR / name
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Categorizer:
    def __init__(self):
        self.keyword_rules = _load_yaml("categories.yaml")
        counterparty_map = _load_yaml("counterparty_map.yaml") or {}
        self.counterparty_map = {k.strip().lower(): v for k, v in counterparty_map.items()}

    def categorize(self, description, counterparty_name=None):
        text = f"{description or ''} {counterparty_name or ''}".lower()

        if counterparty_name:
            override = self.counterparty_map.get(counterparty_name.strip().lower())
            if override:
                return override

        for category, keywords in self.keyword_rules.items():
            for keyword in keywords or []:
                if keyword.lower() in text:
                    return category

        return "Other"
