"""Instant browser control by voice: open pages, switch tabs, press navigation keys, read the page, click links.
Everything here finishes in well under a second, unlike a computer-use job. Zen (Firefox based) has no AppleScript
tab API, so it goes through keystrokes and the macOS Accessibility tree; Chrome, Helium and Safari use AppleScript
for tabs. Handlers write English, the voice agent speaks Portuguese."""

import difflib
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import quote_plus

from AppKit import NSApplicationActivateIgnoringOtherApps, NSWorkspace
from ApplicationServices import (
    AXIsProcessTrusted,
    AXUIElementCopyAttributeValue,
    AXUIElementCopyMultipleAttributeValues,
    AXUIElementCreateApplication,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
    AXValueGetType,
    AXValueGetValue,
)
from Foundation import NSDate, NSRunLoop

READ_LIMIT = 4000
READ_BUDGET = 2.0  # seconds spent walking the accessibility tree
MAX_NODES = 6000
TAB_SEARCH_TIMEOUT = 1.5
CLICK_ROLES = {"AXLink", "AXButton"}
TEXT_ROLES = {"AXStaticText", "AXHeading", "AXTextArea"}
# Menus and search boxes would fill the read-out before the article starts.
CHROME_LANDMARKS = {"AXLandmarkNavigation", "AXLandmarkSearch"}
AX_ATTRS = ["AXRole", "AXSubrole", "AXTitle", "AXValue", "AXDescription", "AXHidden", "AXFrame", "AXURL", "AXChildren"]
AX_RECT_TYPE = 3  # kAXValueCGRectType

ZEN = "app.zen-browser.zen"


@dataclass(frozen=True)
class Browser:
    bundle_id: str
    name: str  # what AppleScript and System Events call it
    kind: str  # "firefox", "chromium" or "safari"


BROWSERS = {
    b.bundle_id: b
    for b in (
        Browser(ZEN, "Zen", "firefox"),
        Browser("org.mozilla.firefox", "Firefox", "firefox"),
        Browser("com.google.Chrome", "Google Chrome", "chromium"),
        Browser("net.imput.helium", "Helium", "chromium"),
        Browser("com.apple.Safari", "Safari", "safari"),
    )
}

# action -> (System Events command, label for the reply)
KEYS = {
    "close_tab": ('keystroke "w" using command down', "Closed the tab"),
    "reload": ('keystroke "r" using command down', "Reloaded the page"),
    "back": ('keystroke "[" using command down', "Went back"),
    "forward": ('keystroke "]" using command down', "Went forward"),
    "scroll_down": ("key code 121", "Scrolled down"),  # Page Down
    "scroll_up": ("key code 116", "Scrolled up"),  # Page Up
    "top": ("key code 115", "Jumped to the top of the page"),  # Home
    "bottom": ("key code 119", "Jumped to the bottom of the page"),  # End
    "zoom_in": ('keystroke "=" using command down', "Zoomed in"),  # Cmd+= works without Shift in every browser
    "zoom_out": ('keystroke "-" using command down', "Zoomed out"),
    "zoom_reset": ('keystroke "0" using command down', "Reset the zoom"),
}


# Pure helpers


def normalize(text: str) -> str:
    """Lowercase, accent-free, single-spaced, for matching what the user said against titles and links."""
    stripped = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


def score_match(query: str, label: str) -> int:
    """0 no match, 1 every word appears somewhere, 2 contains the phrase, 3 starts with it, 4 equal."""
    q, label = normalize(query), normalize(label)
    if not q or not label:
        return 0
    if q == label:
        return 4
    if label.startswith(q):
        return 3
    if q in label:
        return 2
    return 1 if all(word in label for word in q.split()) else 0


def rank_matches(query: str, labels: list[str]) -> list[tuple[int, int]]:
    """(score, index) of every label that matches, best first; among equal scores the shorter label wins."""
    scored = [(score_match(query, label), i) for i, label in enumerate(labels)]
    return sorted(((s, i) for s, i in scored if s), key=lambda si: (-si[0], len(labels[si[1]]), si[1]))


def close_labels(query: str, labels: list[str], limit: int = 3) -> list[str]:
    """Labels that look somewhat like the query, to offer when nothing matched."""
    q = normalize(query)
    scored = sorted(
        ((difflib.SequenceMatcher(None, q, normalize(label)).ratio(), label) for label in labels), reverse=True
    )
    seen: list[str] = []
    for ratio, label in scored:
        if ratio >= 0.4 and label not in seen:
            seen.append(label)
    return seen[:limit]


def search_url(query: str) -> str:
    return "https://www.google.com/search?q=" + quote_plus(query.strip())


def normalize_url(text: str) -> str | None:
    """An http(s) URL for what was said, or None. Other schemes could launch other apps, so they never get through."""
    text = text.strip()
    if re.match(r"^https?://\S+$", text, re.IGNORECASE):
        return text
    if re.match(r"^[\w-]+(\.[\w-]+)+([/:?#]\S*)?$", text):
        return "https://" + text
    return None


def trim_text(text: str, limit: int = READ_LIMIT) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + " [truncated]"


def applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def key_script(process: str, *commands: str, delay: float = 0.15) -> str:
    """One System Events script that runs each command in turn, pausing between them."""
    body = f"\n    delay {delay}\n    ".join(commands)
    return f'tell application "System Events" to tell process {applescript_string(process)}\n    {body}\nend tell'


def choose_browser(frontmost: str, running: list[str]) -> Browser:
    """The browser in front, else Zen (or the first other browser running if Zen isn't), else Zen to launch."""
    if frontmost in BROWSERS:
        return BROWSERS[frontmost]
    if ZEN in running:
        return BROWSERS[ZEN]
    return next((BROWSERS[b] for b in running if b in BROWSERS), BROWSERS[ZEN])


def window_label(title: str, browser: Browser) -> str:
    """The page title from a window title like 'Page - Zen Browser'."""
    return re.sub(rf"\s+[-\u2013\u2014|]\s+({re.escape(browser.name)}( Browser)?|Mozilla Firefox)$", "", title).strip()


def parse_tab_lines(output: str) -> list[tuple[int, int, str, str]]:
    """(window, tab, title, url) from the AppleScript below, one tab per line, fields separated by tabs."""
    tabs = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4 and parts[0].isdigit() and parts[1].isdigit():
            tabs.append((int(parts[0]), int(parts[1]), parts[2], "\t".join(parts[3:])))
    return tabs


def tabs_script(browser: Browser) -> str:
    title = "name" if browser.kind == "safari" else "title"
    return f"""tell application {applescript_string(browser.name)}
    set out to ""
    repeat with wi from 1 to count of windows
        set ts to {title} of tabs of window wi
        set us to URL of tabs of window wi
        repeat with ti from 1 to count of ts
            set out to out & wi & tab & ti & tab & (item ti of ts) & tab & (item ti of us) & linefeed
        end repeat
    end repeat
    return out
end tell"""


def select_tab_script(browser: Browser, window: int, tab: int) -> str:
    select = (
        f"set current tab of window {window} to tab {tab} of window {window}"
        if browser.kind == "safari"
        else f"set active tab index of window {window} to {tab}"
    )
    return f"""tell application {applescript_string(browser.name)}
    {select}
    set index of window {window} to 1
    activate
end tell"""


# Accessibility tree. Real elements are wrapped in AXNode; the walkers only rely on its shape, so tests use fakes.


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float

    def overlaps(self, other: "Rect") -> bool:
        return (
            self.x < other.x + other.w
            and other.x < self.x + self.w
            and self.y < other.y + other.h
            and other.y < self.y + self.h
        )


@dataclass
class Node:
    """The fields the walkers read. AXNode fills them lazily from a live element."""

    role: str = ""
    subrole: str = ""
    title: str = ""
    value: str = ""
    description: str = ""
    url: str = ""
    hidden: bool = False
    frame: Rect | None = None
    children: list["Node"] = field(default_factory=list)

    def press(self) -> bool:
        raise NotImplementedError


class AXNode:
    """A live accessibility element with the same fields as Node, read in one call the first time any is needed."""

    def __init__(self, element):
        self.element = element
        self._data: Node | None = None

    def _load(self) -> Node:
        if self._data is None:
            _, values = AXUIElementCopyMultipleAttributeValues(self.element, AX_ATTRS, 0, None)
            role, subrole, title, value, description, hidden, frame, url, children = values or [None] * len(AX_ATTRS)
            self._data = Node(
                role=text_of(role),
                subrole=text_of(subrole),
                title=text_of(title),
                value=text_of(value),
                description=text_of(description),
                url=str(url.absoluteString()) if hasattr(url, "absoluteString") else "",
                hidden=hidden is True or hidden == 1,
                frame=rect_of(frame),
                children=[AXNode(c) for c in children] if is_array(children) else [],
            )
        return self._data

    role = property(lambda self: self._load().role)
    subrole = property(lambda self: self._load().subrole)
    title = property(lambda self: self._load().title)
    value = property(lambda self: self._load().value)
    description = property(lambda self: self._load().description)
    url = property(lambda self: self._load().url)
    hidden = property(lambda self: self._load().hidden)
    frame = property(lambda self: self._load().frame)
    children = property(lambda self: self._load().children)

    def press(self) -> bool:
        return AXUIElementPerformAction(self.element, "AXPress") == 0


def is_array(value) -> bool:
    """AX arrays arrive as NSArray proxies, which are neither list nor tuple."""
    return (
        value is not None
        and not isinstance(value, (str, bytes))
        and hasattr(value, "__len__")
        and hasattr(value, "__iter__")
    )


def text_of(value) -> str:
    """Attributes an element doesn't have come back as AXValue error objects, not strings."""
    return value if isinstance(value, str) else ""


def rect_of(value) -> Rect | None:
    try:
        if value is None or AXValueGetType(value) != AX_RECT_TYPE:
            return None
        ok, rect = AXValueGetValue(value, AX_RECT_TYPE, None)
        return Rect(rect.origin.x, rect.origin.y, rect.size.width, rect.size.height) if ok else None
    except (TypeError, ValueError):
        return None


def is_visible(node: Node, viewport: Rect | None) -> bool:
    """False for hidden nodes and for ones whose known frame lies outside the viewport (scrolled away)."""
    if node.hidden:
        return False
    frame = node.frame
    if viewport is None or viewport.w <= 0 or viewport.h <= 0 or frame is None or frame.w <= 0 or frame.h <= 0:
        return True
    return frame.overlaps(viewport)


class Walk:
    """Depth-first traversal in document order, stopping at a node count or a deadline, whichever comes first. With a
    viewport it yields only the nodes on screen."""

    def __init__(
        self,
        root: Node,
        viewport: Rect | None = None,
        budget: float = READ_BUDGET,
        max_nodes: int = MAX_NODES,
        skip: Node | None = None,
        skip_subroles: frozenset | set = frozenset(),
    ):
        self.root, self.viewport, self.skip, self.skip_subroles = root, viewport, skip, skip_subroles
        self.deadline = time.monotonic() + budget
        self.left = max_nodes
        self.truncated = False

    def nodes(self):
        stack = [self.root]
        while stack:
            if self.left <= 0 or time.monotonic() > self.deadline:
                self.truncated = True
                return
            self.left -= 1
            node = stack.pop()
            if node is not self.root and (node.hidden or node.subrole in self.skip_subroles):
                continue
            # Only hidden nodes prune their subtree: a container's frame can be far smaller than its content (the
            # page body reports one screen's height), so a scrolled-away frame says nothing about its children.
            if node is self.root or is_visible(node, self.viewport):
                yield node
            stack.extend(c for c in reversed(node.children) if c is not self.skip)


def node_text(node: Node) -> str:
    if node.role not in TEXT_ROLES:
        return ""
    return (node.value or node.title or node.description).strip()


def page_text(root: Node, viewport: Rect | None, budget: float = READ_BUDGET, max_nodes: int = MAX_NODES) -> str:
    """The visible text under a web area. Pieces on the same line are joined with a space, others with a newline."""
    lines: list[str] = []
    last: Rect | None = None
    for node in Walk(root, viewport, budget, max_nodes, skip_subroles=CHROME_LANDMARKS).nodes():
        text = node_text(node)
        if not text or (lines and lines[-1].endswith(text) and last == node.frame):
            continue
        same_line = bool(last and node.frame and abs(node.frame.y - last.y) < max(last.h, 1) / 2)
        if lines and same_line:
            lines[-1] += " " + text
        else:
            lines.append(text)
        last = node.frame or last
    return "\n".join(lines)


def label_of(node: Node) -> str:
    """What a link or button is called: its own title or description, else the text inside it."""
    own = (node.title or node.description or node.value).strip()
    if own:
        return own
    parts = [node_text(n) for n in Walk(node, None, 0.2, 40).nodes() if n is not node]
    return " ".join(p for p in parts if p)


def clickables(root: Node, viewport: Rect | None, budget: float = READ_BUDGET) -> list[tuple[str, Node]]:
    found = []
    for node in Walk(root, viewport, budget).nodes():
        if node.role in CLICK_ROLES:
            label = label_of(node)
            if label:
                found.append((label, node))
    return found


def pick_click(query: str, items: list[tuple[str, Node]]) -> tuple[str, list[tuple[str, Node]]]:
    """('click', [item]), ('ambiguous', top items) or ('none', []): click only when exactly one candidate is best."""
    ranked = rank_matches(query, [label for label, _ in items])
    if not ranked:
        return "none", []
    best = [items[i] for s, i in ranked if s == ranked[0][0]]
    if len(best) == 1 or len({normalize(label) for label, _ in best}) == 1:
        return "click", best[:1]
    return "ambiguous", best[:4]


def find_role(root: Node, role: str, limit: int = 4000, skip: Node | None = None) -> Node | None:
    """First node with this role, breadth first so the shallowest (the toplevel document) wins."""
    queue, seen = [root], 0
    while queue and seen < limit:
        node = queue.pop(0)
        seen += 1
        if node.role == role and not node.hidden:
            return node
        queue.extend(c for c in node.children if c is not skip)
    return None


def find_url_field(window: Node, web_area: Node | None) -> str:
    """The address bar's text: the first text field or combo box in the window outside the page itself."""
    for node in Walk(window, None, 0.5, 1500, skip=web_area).nodes():
        if node.role in ("AXTextField", "AXComboBox") and node.value:
            return node.value
    return ""


# Live system access


def pause(seconds: float):
    """Sleep while letting AppKit deliver its notifications: NSWorkspace's frontmost app is stale otherwise."""
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(seconds))


def frontmost_bundle() -> str:
    pause(0.001)
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return (app.bundleIdentifier() or "") if app else ""


def running_app(bundle_id: str):
    apps = NSWorkspace.sharedWorkspace().runningApplications()
    return next((a for a in apps if a.bundleIdentifier() == bundle_id), None)


def pick_browser() -> Browser:
    running = [a.bundleIdentifier() for a in NSWorkspace.sharedWorkspace().runningApplications()]
    return choose_browser(frontmost_bundle(), [b for b in running if b])


def osascript(script: str, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout, check=False)


def bring_to_front(browser: Browser) -> str | None:
    """Make the browser frontmost so keystrokes land in it. Returns a problem description, or None when it is."""
    if frontmost_bundle() == browser.bundle_id:
        return None
    app = running_app(browser.bundle_id)
    if app is None:
        subprocess.run(["open", "-b", browser.bundle_id], check=False, capture_output=True, timeout=10)
        pause(1.0)
        app = running_app(browser.bundle_id)
    if app is None:
        return f"{browser.name} isn't running"
    app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    for _ in range(60):  # up to 3 s: a browser on another space switches to it with an animation
        if frontmost_bundle() == browser.bundle_id:
            return None
        pause(0.05)
    return f"Couldn't bring {browser.name} to the front"


def send_keys(browser: Browser, *commands: str) -> str | None:
    """Run System Events commands in the browser. Never types anywhere else: the browser is checked right before."""
    problem = bring_to_front(browser)
    if problem:
        return problem
    result = osascript(key_script(browser.name, *commands))
    if result.returncode:
        return f"Couldn't send keys to {browser.name}: {result.stderr.strip()}"
    return None


def ax_trusted() -> bool:
    return bool(AXIsProcessTrusted())


NEEDS_ACCESSIBILITY = (
    "Accessibility permission is missing for the app running Jarvis; it must be enabled in System Settings, "
    "Privacy and Security, Accessibility."
)


def ax_get(element, attribute: str):
    err, value = AXUIElementCopyAttributeValue(element, attribute, None)
    return value if err == 0 else None


def front_window(browser: Browser, wake: bool = True) -> tuple[Node | None, object]:
    """(front window node, application element). Firefox-based browsers only build their web accessibility tree
    once an AX client asks for it, which these two attributes do."""
    app = running_app(browser.bundle_id)
    if app is None:
        return None, None
    app_element = AXUIElementCreateApplication(app.processIdentifier())
    if wake:
        for attribute in ("AXManualAccessibility", "AXEnhancedUserInterface"):
            AXUIElementSetAttributeValue(app_element, attribute, True)
    window = ax_get(app_element, "AXFocusedWindow") or ax_get(app_element, "AXMainWindow")
    return (AXNode(window) if window else None), app_element


def page_area(browser: Browser) -> tuple[Node | None, Node | None, str | None]:
    """(window, web area, problem). A freshly woken tree can take a moment to appear, so retry briefly."""
    if not ax_trusted():
        return None, None, NEEDS_ACCESSIBILITY
    window = None
    for attempt in range(6):
        window, _ = front_window(browser)
        area = find_role(window, "AXWebArea") if window else None
        if area:
            return window, area, None
        time.sleep(0.15 * (attempt + 1))
    if window is None:
        return None, None, f"{browser.name} has no open window"
    return window, None, f"Couldn't find the page in {browser.name}'s accessibility tree"


def current_title(browser: Browser) -> str:
    window, _ = front_window(browser, wake=False)
    return window_label(window.title, browser) if window else ""


# Actions


def open_in_browser(browser: Browser, url: str) -> str | None:
    """Open the URL in a new tab and bring the browser forward; returns the error text, or None on success."""
    result = subprocess.run(
        ["open", "-b", browser.bundle_id, url], capture_output=True, text=True, check=False, timeout=10
    )
    if result.returncode:
        return result.stderr.strip() or "open failed"
    bring_to_front(browser)  # `open` leaves a browser on another space or display in the background
    return None


def open_url(params: dict) -> str:
    url = normalize_url(params.get("url", ""))
    if not url:
        return "That isn't a web address; use the search action for plain words."
    browser = pick_browser()
    problem = open_in_browser(browser, url)
    return f"Couldn't open {url} in {browser.name}: {problem}" if problem else f"Opened {url} in {browser.name}"


def search(params: dict) -> str:
    query = params.get("query", "").strip()
    if not query:
        return "Nothing to search for."
    browser = pick_browser()
    problem = open_in_browser(browser, search_url(query))
    return (
        f"Couldn't search in {browser.name}: {problem}"
        if problem
        else f"Searched Google for '{query}' in {browser.name}"
    )


def switch_scripted(browser: Browser, query: str) -> str:
    result = osascript(tabs_script(browser))
    if result.returncode:
        return f"Couldn't list {browser.name}'s tabs: {result.stderr.strip()}"
    tabs = parse_tab_lines(result.stdout)
    ranked = rank_matches(query, [f"{title} {url}" for _, _, title, url in tabs])
    if not ranked:
        titles = close_labels(query, [title for _, _, title, _ in tabs])
        return f"No open tab matches '{query}'." + (f" Closest: {'; '.join(titles)}" if titles else "")
    window, tab, title, _ = tabs[ranked[0][1]]
    result = osascript(select_tab_script(browser, window, tab))
    if result.returncode:
        return f"Couldn't switch tabs in {browser.name}: {result.stderr.strip()}"
    return f"Switched to the tab '{title}'"


def focused_node(browser: Browser) -> AXNode | None:
    app = running_app(browser.bundle_id)
    focused = ax_get(AXUIElementCreateApplication(app.processIdentifier()), "AXFocusedUIElement") if app else None
    return AXNode(focused) if focused else None


def urlbar_value(browser: Browser) -> str:
    """The text of the focused element (the address bar while a tab search is being typed)."""
    node = focused_node(browser)
    return node.value if node else ""


def leave_urlbar(browser: Browser):
    """Escape out of the address bar. Only while it has focus: Escape on the page itself would, for instance, end a
    fullscreen presentation."""
    for _ in range(2):
        node = focused_node(browser)
        if not node or node.role != "AXComboBox":
            return
        send_keys(browser, "key code 53")
        time.sleep(0.1)


def wait_for_tab_search(browser: Browser, query: str) -> bool:
    """Firefox swaps the '% ' prefix for a 'Tabs' chip a moment after typing it, and only then lists the open tabs;
    Return before that does nothing. The address bar text becoming just the query marks the switch."""
    deadline = time.monotonic() + TAB_SEARCH_TIMEOUT
    while time.monotonic() < deadline:
        if normalize(urlbar_value(browser)) == normalize(query):
            time.sleep(0.1)  # the results are painted right with the chip; this only covers slower machines
            return True
        time.sleep(0.03)
    return False


def switch_firefox(browser: Browser, query: str) -> str:
    """Cmd+L, then '% query' filters the address bar to open tabs and Return picks the first."""
    if not ax_trusted():
        return NEEDS_ACCESSIBILITY
    before = current_title(browser)
    problem = send_keys(browser, 'keystroke "l" using command down', f"keystroke {applescript_string('% ' + query)}")
    if problem:
        return problem
    if not wait_for_tab_search(browser, query):
        leave_urlbar(browser)
        return "The tab search didn't open, so nothing was changed."
    problem = send_keys(browser, "key code 125", "key code 36")  # Down selects the first result, Return opens it
    if problem:
        return problem
    deadline = time.monotonic() + 1.0
    title = current_title(browser)
    while title == before and time.monotonic() < deadline:
        time.sleep(0.05)
        title = current_title(browser)
    if not title:
        return "Pressed Return on the tab search, but couldn't read the window title to confirm."
    if title == before:
        leave_urlbar(browser)  # with no result, Return leaves the typed search sitting in the address bar
        return f"No other tab seemed to match '{query}'; the tab in front is still '{title}'."
    suffix = "" if score_match(query, title) else f" (it doesn't obviously match '{query}')"
    return f"Switched to the tab '{title}'{suffix}"


def switch_tab(params: dict) -> str:
    query = params.get("query", "").strip()
    if not query:
        return "Which tab?"
    browser = pick_browser()
    return switch_firefox(browser, query) if browser.kind == "firefox" else switch_scripted(browser, query)


def press_key(action: str) -> str:
    command, done = KEYS[action]
    browser = pick_browser()
    problem = send_keys(browser, command)
    return problem or f"{done} in {browser.name}"


def read_page(_: dict) -> str:
    browser = pick_browser()
    window, area, problem = page_area(browser)
    if problem:
        return problem
    title = window_label(window.title, browser)
    url = (area.url or find_url_field(window, area)) if area else ""
    text = trim_text(page_text(area, area.frame))
    if not text:
        text = "(no visible text found)"
    return f"Page: {title or '(untitled)'}\nURL: {url or '(unknown)'}\n\n{text}"


def click(params: dict) -> str:
    query = params.get("text", "").strip()
    if not query:
        return "Click what?"
    browser = pick_browser()
    for attempt in range(2):
        _, area, problem = page_area(browser)
        if problem:
            return problem
        items = clickables(area, area.frame)
        verdict, best = pick_click(query, items)
        if verdict == "none":
            close = close_labels(query, [label for label, _ in items])
            return f"No link or button matches '{query}'." + (f" Closest: {'; '.join(close)}" if close else "")
        if verdict == "ambiguous":
            return f"Several match '{query}': " + "; ".join(f"'{label}'" for label, _ in best) + ". Which one?"
        label, node = best[0]
        kind = "link" if node.role == "AXLink" else "button"
        if node.press():
            return f"Clicked the {kind} '{label}'"
        if attempt == 0:
            time.sleep(0.4)  # a page that is still loading rebuilds its tree and invalidates the element
    return f"Found the {kind} '{label}' but pressing it failed."


def browser_tool(params: dict) -> str:
    action = params.get("action", "")
    if action == "open":
        return open_url(params)
    if action == "search":
        return search(params)
    if action == "switch_tab":
        return switch_tab(params)
    if action == "read_page":
        return read_page(params)
    if action == "click":
        return click(params)
    if action in KEYS:
        return press_key(action)
    return f"Unknown browser action: {action}"


SPECS = [
    {
        "name": "browser",
        "handler": browser_tool,
        "description": "Drive Vinicius's web browser (Zen by default, also Chrome, Helium, Safari). INSTANT, under a "
        "second: always use this for browser requests and never start a desktop task for them. Use it for 'abre o "
        "YouTube' (open, with a full https URL), 'pesquisa X' (search), 'vai pra aba do Slack' (switch_tab with a "
        "word from the tab's title or address), 'fecha essa aba', 'recarrega', 'volta pra página anterior' (back; a bare 'volta' "
        "during a presentation is the slide tool), 'avança', 'desce', 'sobe', 'vai pro topo', 'vai pro final', 'aumenta o zoom', 'diminui o zoom', 'zoom normal', 'lê essa "
        "página' or 'resume essa página' (read_page returns the visible text with title and URL, including "
        "logged-in pages; summarize it yourself), and 'clica em X' (click presses the link or button with that text). "
        "Every action returns a short factual line saying what happened; relay it briefly.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "open", "search", "switch_tab", "close_tab", "reload", "back", "forward",
                        "scroll_down", "scroll_up", "top", "bottom", "zoom_in", "zoom_out", "zoom_reset",
                        "read_page", "click",
                    ],
                    "description": "What to do",
                },
                "url": {"type": "string", "description": "Full web address starting with https://, for open"},
                "query": {"type": "string", "description": "Search words for search, or a word from the tab's title or address for switch_tab"},
                "text": {"type": "string", "description": "The visible text of the link or button, for click"},
            },
            "required": ["action"],
        },
    }
]  # fmt: skip
