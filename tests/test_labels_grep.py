"""Copy rule (D12): user-facing code never says the banned two-word phrase."""
from pathlib import Path

BANNED = ("exact", "match")
APP_DIR = Path(__file__).resolve().parent.parent / "app"


def test_no_banned_phrase_in_app_code():
    offenders: list[str] = []
    for path in APP_DIR.rglob("*"):
        if path.suffix.lower() not in (".py", ".js", ".html", ".css"):
            continue
        # normalize whitespace so line-breaks can't hide the phrase
        text = " ".join(path.read_text(encoding="utf-8", errors="ignore").lower().split())
        if " ".join(BANNED) in text:
            offenders.append(str(path.relative_to(APP_DIR)))
    assert not offenders, (
        f"banned phrase {' '.join(BANNED)!r} found in user-facing code: {offenders}"
    )
