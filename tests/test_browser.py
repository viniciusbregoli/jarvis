import time
from dataclasses import dataclass

import pytest

from jarvis import browser
from jarvis.browser import BROWSERS, ZEN, Node, Rect

ZEN_BROWSER = BROWSERS[ZEN]
CHROME = BROWSERS["com.google.Chrome"]


@dataclass
class FakeNode(Node):
    pressed: bool = False

    def press(self) -> bool:
        self.pressed = True
        return True


def text(value, y=0.0, **kwargs):
    return FakeNode(role="AXStaticText", value=value, frame=Rect(0, y, 100, 20), **kwargs)


def link(label, y=0.0, **kwargs):
    return FakeNode(role="AXLink", title=label, frame=Rect(0, y, 100, 20), **kwargs)


def page(*children):
    return FakeNode(role="AXWebArea", frame=Rect(0, 0, 800, 600), children=list(children))


# matching


def test_matching_ignores_case_and_accents():
    assert browser.score_match("configuracoes", "Configurações | Conta") == 3
    assert browser.score_match("conta", "Configurações | Conta") == 2
    assert browser.score_match("CAFÉ", "cafe") == 4
    assert browser.score_match("slack geral", "Geral | Slack") == 1
    assert browser.score_match("github", "Slack") == 0


def test_rank_prefers_exact_then_prefix_then_contains_then_shorter():
    labels = ["Pricing plans and more", "Our pricing", "Pricing", "Pricing plans", "Contact"]
    ranked = [labels[i] for _, i in browser.rank_matches("pricing", labels)]
    assert ranked == ["Pricing", "Pricing plans", "Pricing plans and more", "Our pricing"]


def test_close_labels_suggests_similar_and_skips_unrelated():
    assert browser.close_labels("pricin", ["Pricing", "Contact us", "Pricing"]) == ["Pricing"]
    assert browser.close_labels("zzz", ["Pricing"]) == []


# urls and keys


def test_search_url_encodes_the_query():
    assert browser.search_url(" café com leite & mel ") == "https://www.google.com/search?q=caf%C3%A9+com+leite+%26+mel"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://example.com/a?b=1", "https://example.com/a?b=1"),
        ("example.com", "https://example.com"),
        ("en.wikipedia.org/wiki/HAL_9000", "https://en.wikipedia.org/wiki/HAL_9000"),
        ("youtube", None),
        ("slack://open", None),
        ("file:///etc/passwd", None),
        ("javascript:alert(1)", None),
        ("two words.com", None),
    ],
)
def test_normalize_url_only_lets_web_addresses_through(text, expected):
    assert browser.normalize_url(text) == expected


def test_key_script_targets_the_process_and_pauses_between_commands():
    script = browser.key_script("Zen", 'keystroke "l" using command down', 'keystroke "% slack"')
    assert script.splitlines() == [
        'tell application "System Events" to tell process "Zen"',
        '    keystroke "l" using command down',
        "    delay 0.15",
        '    keystroke "% slack"',
        "end tell",
    ]


def test_applescript_string_escapes_quotes_and_backslashes():
    assert browser.applescript_string('% say "hi" \\') == '"% say \\"hi\\" \\\\"'


def test_every_key_action_is_a_single_system_events_command():
    for command, done in browser.KEYS.values():
        assert command.startswith(("keystroke ", "key code ")) and done
    assert browser.KEYS["scroll_down"][0] == "key code 121"
    assert browser.KEYS["close_tab"][0] == 'keystroke "w" using command down'


def test_trim_text_collapses_whitespace_and_cuts_on_a_word():
    assert browser.trim_text("a   b\n\n\n c") == "a b\n c"
    trimmed = browser.trim_text("word " * 2000, limit=100)
    assert trimmed.endswith("word [truncated]") and len(trimmed) <= 116


def test_choose_browser_prefers_the_front_one_then_zen():
    assert browser.choose_browser("com.google.Chrome", [ZEN, "com.google.Chrome"]) == CHROME
    assert browser.choose_browser("com.apple.finder", ["com.google.Chrome", ZEN]) == ZEN_BROWSER
    assert browser.choose_browser("com.apple.finder", ["com.google.Chrome"]) == CHROME
    assert browser.choose_browser("com.apple.finder", []) == ZEN_BROWSER


def test_window_label_drops_the_browser_suffix():
    assert browser.window_label("Slack | general - Zen Browser", ZEN_BROWSER) == "Slack | general"
    assert browser.window_label("HAL 9000 — Wikipedia — Zen Browser", ZEN_BROWSER) == "HAL 9000 — Wikipedia"
    assert browser.window_label("Example Domain", ZEN_BROWSER) == "Example Domain"


# AppleScript tabs


def test_parse_tab_lines_keeps_tabs_inside_urls_and_skips_noise():
    output = "1\t1\tInbox\thttps://mail.test/\n1\t2\tA\tB\thttps://x.test/?a\tb\nnoise\n2\t1\tDocs\thttps://docs.test\n"
    assert browser.parse_tab_lines(output) == [
        (1, 1, "Inbox", "https://mail.test/"),
        (1, 2, "A", "B\thttps://x.test/?a\tb"),
        (2, 1, "Docs", "https://docs.test"),
    ]


def test_tab_scripts_differ_for_safari_and_chromium():
    safari = BROWSERS["com.apple.Safari"]
    assert "name of tabs" in browser.tabs_script(safari) and "title of tabs" in browser.tabs_script(CHROME)
    assert "set current tab of window 2 to tab 3 of window 2" in browser.select_tab_script(safari, 2, 3)
    assert "set active tab index of window 2 to 3" in browser.select_tab_script(CHROME, 2, 3)
    assert 'tell application "Google Chrome"' in browser.select_tab_script(CHROME, 2, 3)


def test_switch_scripted_picks_the_best_tab(monkeypatch):
    calls = []

    class Result:
        returncode, stderr = 0, ""
        stdout = "1\t1\tInbox\thttps://mail.test/\n1\t2\tSlack | geral\thttps://app.slack.com/x\n"

    monkeypatch.setattr(browser, "osascript", lambda script, **_: calls.append(script) or Result())
    assert browser.switch_scripted(CHROME, "slack") == "Switched to the tab 'Slack | geral'"
    assert "set active tab index of window 1 to 2" in calls[1]
    assert browser.switch_scripted(CHROME, "jira").startswith("No open tab matches 'jira'")


# AX tree walking


def test_page_text_reads_visible_text_in_order_and_skips_hidden_and_offscreen():
    tree = page(
        FakeNode(role="AXHeading", value="Title", frame=Rect(0, 0, 100, 20)),
        FakeNode(role="AXGroup", children=[text("Hello", 30), text("secret", 50, hidden=True)]),
        FakeNode(role="AXGroup", frame=Rect(0, 5000, 100, 100), children=[text("far below", 5000)]),
        text("below the fold", 900),
        FakeNode(role="AXImage", description="logo", frame=Rect(0, 60, 10, 10)),
        text("Bye", 80),
    )
    assert browser.page_text(tree, tree.frame) == "Title\nHello\nBye"


def test_page_text_joins_pieces_on_the_same_line():
    tree = page(text("Price:", 10), text("$5", 10), text("Next line", 40))
    assert browser.page_text(tree, tree.frame) == "Price: $5\nNext line"


def test_page_text_without_frames_still_returns_everything():
    tree = FakeNode(role="AXWebArea", children=[FakeNode(role="AXStaticText", value=v) for v in "ab"])
    assert browser.page_text(tree, None) == "a\nb"


def test_walk_stops_at_the_node_cap_and_the_deadline():
    tree = page(*[text(f"t{i}", i) for i in range(50)])
    walk = browser.Walk(tree, None, max_nodes=10)
    assert len(list(walk.nodes())) == 10 and walk.truncated
    expired = browser.Walk(tree, None, budget=-1)
    assert list(expired.nodes()) == [] and expired.truncated


def test_walk_deadline_is_checked_while_walking(monkeypatch):
    tree = page(*[text(f"t{i}", i) for i in range(5)])
    ticks = iter([0, 0, 0, 100, 100, 100, 100, 100])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    walk = browser.Walk(tree, None, budget=1)
    assert len(list(walk.nodes())) == 2 and walk.truncated


def test_label_of_falls_back_to_inner_text():
    assert browser.label_of(link("Pricing")) == "Pricing"
    wrapped = FakeNode(role="AXLink", children=[text("Read"), text("more")])
    assert browser.label_of(wrapped) == "Read more"
    assert browser.label_of(FakeNode(role="AXButton", description="Close")) == "Close"


def test_clickables_collects_links_and_buttons_only():
    button = FakeNode(role="AXButton", title="Send", frame=Rect(0, 40, 50, 20))
    tree = page(link("Docs"), text("plain"), button, FakeNode(role="AXLink", frame=Rect(0, 70, 5, 5)))
    assert [label for label, _ in browser.clickables(tree, tree.frame)] == ["Docs", "Send"]


def test_pick_click_prefers_exact_and_flags_ambiguity():
    items = [(label, link(label)) for label in ["Pricing plans", "Pricing", "Our pricing", "Contact"]]
    verdict, best = browser.pick_click("pricing", items)
    assert (verdict, best[0][0]) == ("click", "Pricing")
    verdict, best = browser.pick_click("plans", items)
    assert (verdict, best[0][0]) == ("click", "Pricing plans")
    ambiguous = [(label, link(label)) for label in ["Buy now", "Buy later"]]
    verdict, best = browser.pick_click("buy", ambiguous)
    assert verdict == "ambiguous" and [label for label, _ in best] == ["Buy now", "Buy later"]
    assert browser.pick_click("zzz", items) == ("none", [])


def test_pick_click_treats_duplicate_labels_as_one_target():
    first, second = link("Home"), link("home")
    verdict, best = browser.pick_click("Home", [("Home", first), ("home", second)])
    assert verdict == "click" and best[0][1] is first


def test_find_role_is_breadth_first_and_ignores_hidden():
    deep = FakeNode(role="AXWebArea", title="deep")
    shallow = FakeNode(role="AXWebArea", title="shallow")
    hidden = FakeNode(role="AXWebArea", title="hidden", hidden=True)
    root = FakeNode(role="AXWindow", children=[FakeNode(role="AXGroup", children=[deep]), hidden, shallow])
    assert browser.find_role(root, "AXWebArea") is shallow
    assert browser.find_role(root, "AXTable") is None


def test_find_url_field_skips_the_page_itself():
    area = page(FakeNode(role="AXTextField", value="typed in the page"))
    toolbar = FakeNode(role="AXToolbar", children=[FakeNode(role="AXTextField", value="example.com")])
    assert browser.find_url_field(FakeNode(role="AXWindow", children=[toolbar, area]), area) == "example.com"
    assert browser.find_url_field(FakeNode(role="AXWindow", children=[area]), area) == ""


# handlers


@pytest.fixture
def zen(monkeypatch):
    monkeypatch.setattr(browser, "pick_browser", lambda: ZEN_BROWSER)
    return ZEN_BROWSER


def test_read_page_reports_title_url_and_text(monkeypatch, zen):
    area = page(text("Hello world", 10))
    area.url = "https://example.com/"
    window = FakeNode(role="AXWindow", title="Example Domain - Zen Browser")
    monkeypatch.setattr(browser, "page_area", lambda b: (window, area, None))
    assert browser.read_page({}) == "Page: Example Domain\nURL: https://example.com/\n\nHello world"


def test_read_page_falls_back_to_the_address_bar(monkeypatch, zen):
    area = page(text("Body", 10))
    window = FakeNode(role="AXWindow", title="T", children=[FakeNode(role="AXTextField", value="example.org"), area])
    monkeypatch.setattr(browser, "page_area", lambda b: (window, area, None))
    assert "URL: example.org" in browser.read_page({})


def test_read_page_passes_problems_through(monkeypatch, zen):
    monkeypatch.setattr(browser, "page_area", lambda b: (None, None, browser.NEEDS_ACCESSIBILITY))
    assert browser.read_page({}) == browser.NEEDS_ACCESSIBILITY


def test_click_presses_the_best_match_and_reports_it(monkeypatch, zen):
    pricing = link("Pricing", 10)
    area = page(link("Pricing plans", 40), pricing)
    monkeypatch.setattr(browser, "page_area", lambda b: (None, area, None))
    assert browser.click({"text": "pricing"}) == "Clicked the link 'Pricing'"
    assert pricing.pressed


def test_click_lists_candidates_when_ambiguous_or_missing(monkeypatch, zen):
    area = page(link("Buy now", 10), link("Buy later", 40))
    monkeypatch.setattr(browser, "page_area", lambda b: (None, area, None))
    assert browser.click({"text": "buy"}) == "Several match 'buy': 'Buy now'; 'Buy later'. Which one?"
    assert browser.click({"text": "refund"}).startswith("No link or button matches 'refund'")
    assert not any(node.pressed for node in area.children)


def test_click_reports_a_failed_press(monkeypatch, zen):
    stuck = link("Go", 10)
    stuck.press = lambda: False
    monkeypatch.setattr(browser, "page_area", lambda b: (None, page(stuck), None))
    assert browser.click({"text": "go"}) == "Found the link 'Go' but pressing it failed."


class Done:
    returncode, stderr, stdout = 0, "", ""


def test_open_and_search_use_open_with_the_browser_bundle(monkeypatch, zen):
    calls = []
    monkeypatch.setattr(browser.subprocess, "run", lambda cmd, **_: calls.append(cmd) or Done())
    assert browser.browser_tool({"action": "open", "url": "https://example.com"}) == "Opened https://example.com in Zen"
    assert browser.browser_tool({"action": "search", "query": "hal 9000"}) == "Searched Google for 'hal 9000' in Zen"
    assert calls == [
        ["open", "-b", ZEN, "https://example.com"],
        ["open", "-b", ZEN, "https://www.google.com/search?q=hal+9000"],
    ]
    assert browser.browser_tool({"action": "open", "url": "slack://x"}).startswith("That isn't a web address")
    assert len(calls) == 2


def test_key_actions_send_one_command_to_the_browser(monkeypatch, zen):
    sent = []
    monkeypatch.setattr(browser, "send_keys", lambda b, *commands: sent.append((b.name, commands)))
    assert browser.browser_tool({"action": "scroll_down"}) == "Scrolled down in Zen"
    assert browser.browser_tool({"action": "reload"}) == "Reloaded the page in Zen"
    assert sent == [("Zen", ("key code 121",)), ("Zen", ('keystroke "r" using command down',))]
    assert browser.browser_tool({"action": "dance"}) == "Unknown browser action: dance"


def test_send_keys_never_types_when_the_browser_is_not_in_front(monkeypatch):
    monkeypatch.setattr(browser, "bring_to_front", lambda b: "Couldn't bring Zen to the front")
    monkeypatch.setattr(browser, "osascript", lambda *a, **k: pytest.fail("typed into another app"))
    assert browser.send_keys(ZEN_BROWSER, "key code 121") == "Couldn't bring Zen to the front"


def test_send_keys_runs_the_script_once_the_browser_is_in_front(monkeypatch):
    scripts = []
    monkeypatch.setattr(browser, "bring_to_front", lambda b: None)
    monkeypatch.setattr(browser, "osascript", lambda script, **_: scripts.append(script) or Done())
    assert browser.send_keys(ZEN_BROWSER, "key code 121") is None
    assert scripts == ['tell application "System Events" to tell process "Zen"\n    key code 121\nend tell']


def test_bring_to_front_skips_activation_when_already_front(monkeypatch):
    monkeypatch.setattr(browser, "frontmost_bundle", lambda: ZEN)
    monkeypatch.setattr(browser, "running_app", lambda b: pytest.fail("looked up the app"))
    assert browser.bring_to_front(ZEN_BROWSER) is None


def test_bring_to_front_activates_a_running_browser(monkeypatch):
    front = ["com.apple.finder"]

    class App:
        def activateWithOptions_(self, options):
            front[0] = ZEN

    monkeypatch.setattr(browser, "frontmost_bundle", lambda: front[0])
    monkeypatch.setattr(browser, "running_app", lambda b: App())
    assert browser.bring_to_front(ZEN_BROWSER) is None
    assert front == [ZEN]


def test_switch_firefox_types_the_tab_search_then_confirms_by_title(monkeypatch):
    sent, titles = [], iter(["Example Domain", "Slack | general"])
    monkeypatch.setattr(browser, "ax_trusted", lambda: True)
    monkeypatch.setattr(browser, "send_keys", lambda b, *commands: sent.append(commands))
    monkeypatch.setattr(browser, "current_title", lambda b: next(titles))
    monkeypatch.setattr(browser, "TAB_SEARCH_WAIT", 0)
    assert browser.switch_firefox(ZEN_BROWSER, "slack") == "Switched to the tab 'Slack | general'"
    assert sent == [('keystroke "l" using command down', 'keystroke "% slack"'), ("key code 36",)]


def test_switch_firefox_says_when_nothing_changed(monkeypatch):
    monkeypatch.setattr(browser, "ax_trusted", lambda: True)
    monkeypatch.setattr(browser, "send_keys", lambda b, *commands: None)
    monkeypatch.setattr(browser, "current_title", lambda b: "Example Domain")
    monkeypatch.setattr(browser, "TAB_SEARCH_WAIT", 0)
    assert "No tab seemed to match 'jira'" in browser.switch_firefox(ZEN_BROWSER, "jira")


def test_switch_firefox_needs_accessibility(monkeypatch):
    monkeypatch.setattr(browser, "ax_trusted", lambda: False)
    monkeypatch.setattr(browser, "send_keys", lambda *a: pytest.fail("typed without permission"))
    assert browser.switch_firefox(ZEN_BROWSER, "slack") == browser.NEEDS_ACCESSIBILITY


def test_spec_is_a_valid_client_tool():
    (spec,) = browser.SPECS
    assert spec["name"] == "browser" and callable(spec["handler"])
    actions = set(spec["parameters"]["properties"]["action"]["enum"])
    assert actions == set(browser.KEYS) | {"open", "search", "switch_tab", "read_page", "click"}
    description = spec["description"]
    for phrase in ("INSTANT", "abre o YouTube", "pesquisa X", "aba do Slack", "fecha essa aba", "volta", "desce"):
        assert phrase in description
    for phrase in ("lê essa", "resume essa", "clica em X"):
        assert phrase in description
