"""Shared lighting vocabulary for mock providers and the mock catalog generator."""
from __future__ import annotations

TYPES = ["pendant", "floor", "table", "wall_sconce", "ceiling", "arc", "chandelier", "linear"]
STYLES = ["mid_century", "industrial", "modern", "scandinavian", "rustic", "art_deco",
          "minimalist", "coastal"]
FINISHES = ["brass", "matte_black", "chrome", "brushed_nickel", "bronze", "gold",
            "matte_white", "natural_wood"]
COLORS = ["black", "white", "brass", "gold", "silver", "grey", "green", "blue", "natural"]
MATERIALS = ["metal", "glass", "wood", "fabric", "ceramic", "rattan"]
SIZE_CLASSES = ["small", "medium", "large"]

CATEGORY_BY_TYPE = {
    "pendant": "pendant_lamp",
    "floor": "floor_lamp",
    "table": "table_lamp",
    "wall_sconce": "wall_light",
    "ceiling": "ceiling_light",
    "arc": "arc_lamp",
    "chandelier": "chandelier",
    "linear": "linear_pendant",
}

# visual design words used in generated images + reason lines
DESIGNS = ["dome", "cone", "cylinder", "orb", "drum", "globe"]

FINISH_TO_RGB = {
    "brass": (196, 158, 88),
    "matte_black": (44, 44, 48),
    "chrome": (200, 205, 212),
    "brushed_nickel": (168, 170, 172),
    "bronze": (128, 92, 60),
    "gold": (214, 175, 80),
    "matte_white": (238, 236, 230),
    "natural_wood": (176, 137, 104),
}

COLOR_TO_RGB = {
    "black": (44, 44, 48),
    "white": (238, 236, 230),
    "brass": (196, 158, 88),
    "gold": (214, 175, 80),
    "silver": (192, 194, 200),
    "grey": (122, 124, 130),
    "green": (74, 103, 65),
    "blue": (52, 84, 120),
    "natural": (176, 137, 104),
}

CRITICAL_TAG_TYPES = ["type", "style", "finish", "color", "size_class"]

TITLE_ADJECTIVES = ["Aurora", "Nordwood", "Marlow", "Halden", "Sable", "Vesper", "Orin",
                    "Lumen", "Kestrel", "Barlow", "Juniper", "Ardent", "Pellow", "Wren",
                    "Cascade", "Meridian", "Talon", "Osprey", "Vale", "Flint"]


def title_for(design: str, lamp_type: str, finish: str, adjective: str) -> str:
    return f"{adjective} {design.title()} {lamp_type.replace('_', ' ').title()}, {finish.replace('_', ' ').title()}"
