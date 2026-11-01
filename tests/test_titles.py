import pytest

from clipbot import titles


@pytest.mark.parametrize("name,expected", [
    ("mirage1v3clutchmp9.mp4", "Mirage 1v3 Clutch MP9"),
    ("infernopitappsecorush3k.mp4", "Inferno Pit Apps Eco Rush 3K"),
    ("mac101v3miragebsite.mp4", "MAC-10 1v3 Mirage B Site"),
    ("dust2 usp 4k headshots.mp4", "Dust 2 USP 4K Headshots"),
    ("officeakaceallheadshots.mp4", "Office AK ACE All Headshots"),
    ("miragebholdm44k.mp4", "Mirage B Hold M4 4K"),
    ("youcantbethatretarded.mp4", "You Cant Be That Retarded"),
    ("ancientidkwhyiclippedthisjunk.mp4", "Ancient IDK Why I Clipped This Junk"),
])
def test_rule_titles(name, expected):
    assert titles.rule_title(name) == expected


def test_make_title_without_key_uses_rules(monkeypatch):
    monkeypatch.setattr(titles, "ai_title", lambda *a, **k: pytest.fail("should not call the API"))
    assert titles.make_title("anubis4kecofrags.mp4") == "Anubis 4K Eco Frags"


def test_make_title_falls_back_when_api_fails(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(titles, "ai_title", boom)
    assert titles.make_title("anubis4kecofrags.mp4", api_key="sk-test") == "Anubis 4K Eco Frags"


def test_make_title_uses_ai_when_available(monkeypatch):
    monkeypatch.setattr(titles, "ai_title", lambda f, k, m: "Eco 4K on Anubis")
    assert titles.make_title("anubis4kecofrags.mp4", api_key="sk-test") == "Eco 4K on Anubis"


def test_slang_words():
    assert titles.rule_title("1v4deagclutchmirage") == "1v4 Deag Clutch Mirage"
    assert titles.rule_title("1v4notimesadge") == "1v4 No Time Sadge"


@pytest.mark.parametrize("name,title", [
    ("2deagsmirage", "2 Deags Mirage"),
    ("awpsonly", "AWPs Only"),
    ("3kdeags", "3K Deags"),
    ("acesoninferno", "ACEs On Inferno"),
    ("deags", "Deags"),
    ("taps", "Taps"),
    ("uspheadtaps", "USP Head Taps"),
])
def test_plurals_of_cs2_terms(name, title):
    assert titles.rule_title(name) == title
