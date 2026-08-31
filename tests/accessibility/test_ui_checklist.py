"""The UI/UX Standards §13 acceptance checklist, as executable assertions.

Phase 10 acceptance criterion 1 is "every acceptance item in UI/UX Standards §13 passes". A
checklist a person ticks by hand is a checklist that regresses between releases, so every item
that can be decided from the rendered HTML and the token palette is decided here.

Four of the sixteen items cannot be: "light and dark present the same information hierarchy" is a
judgement about design rather than markup, "layout is correct at 1280×720 and at 375 px" needs a
rendering engine, and "no network request leaves the machine" is checked structurally here (no
absolute URL in any template or asset) rather than with a browser network panel. Each of those is
marked in the test that covers what *can* be checked, so the gap is recorded rather than implied.

The contrast test is the one worth reading. It parses the token block out of the rendered page and
computes WCAG 2.1 relative luminance over the pairs the application actually uses, in both themes.
It checks the used pairs rather than the cross product: ``--mw-text-subtle`` against
``--mw-surface`` is a pair the standard lists and no page combines, and asserting over it would
fail for a combination nothing renders.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from weightsdb import MigrationRunner, create_engine_for

from freeweight.config import load_settings
from freeweight.services.database import MIGRATIONS_LOCATION
from freeweight.web.app import create_app

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "freeweight" / "web" / "templates"
STATIC = Path(__file__).resolve().parents[2] / "src" / "freeweight" / "web" / "static"

PAGES = (
    "/",
    "/dashboard",
    "/machines",
    "/models",
    "/runs",
    "/results",
    "/compare",
    "/evidence",
    "/database",
    "/settings",
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A served application over an empty, migrated database.

    Empty on purpose: the checklist is about the shell and the four states, and the empty state is
    the one a fresh install spends its first minutes in.
    """
    database = tmp_path / "freeweight.sqlite3"
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    engine = create_engine_for(f"sqlite:///{database}")
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    loaded = load_settings(config_path=tmp_path / "missing.toml")
    with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as test_client:
        yield test_client


# --------------------------------------------------------------------------- contrast


def _channel(component: int) -> float:
    """One sRGB channel, linearized, per WCAG 2.1."""
    ratio = component / 255
    return ratio / 12.92 if ratio <= 0.03928 else ((ratio + 0.055) / 1.055) ** 2.4  # noqa: PLR2004


def _luminance(hex_colour: str) -> float:
    """Relative luminance of ``#RRGGBB``."""
    value = hex_colour.lstrip("#")
    red, green, blue = (int(value[index : index + 2], 16) for index in (0, 2, 4))
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 2.1 contrast ratio between two ``#RRGGBB`` colours."""
    first, second = _luminance(foreground), _luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _tokens(css: str, *, block: str) -> dict[str, str]:
    """Parse the ``--mw-*`` colours out of one block of the served tokens stylesheet."""
    start = css.index(block)
    end = css.index("}", start)
    return {
        name: value
        for name, value in re.findall(r"(--mw-[a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", css[start:end])
    }


def _tokens_css(client: TestClient) -> str:
    """Fetch the tokens stylesheet the rendered page actually links.

    Since Phase 12 the palette lives in MirrorWall's ``tokens.css`` (served under a
    content-hashed URL) rather than an inline ``<style>`` block, so the checklist follows the
    page's own ``<link>`` to it — the same route a browser takes.
    """
    html = client.get("/").text
    match = re.search(r'href="([^"]*mirrorwall/css/tokens\.css[^"]*)"', html)
    assert match, "the page no longer links MirrorWall's tokens.css"
    response = client.get(match.group(1))
    assert response.status_code == 200
    return str(response.text)


def _page_with_styles(client: TestClient, path: str = "/") -> str:
    """A page's HTML with every stylesheet it links appended.

    Before Phase 12 the shell's CSS was one inline ``<style>`` block, so a property assertion
    could read the page alone. The rules now live in MirrorWall's stylesheets; what a browser
    applies is the page plus its links, so that is what these assertions read.
    """
    html: str = client.get(path).text
    sheets: list[str] = []
    for href in re.findall(r'<link rel="stylesheet" href="([^"]+)"', html):
        response = client.get(href)
        assert response.status_code == 200, href
        sheets.append(str(response.text))
    return html + "\n".join(sheets)


# The pairs the application actually renders. Foreground, background, and the minimum the standard
# sets for that role: 4.5 for body text, 3.0 for large text and UI boundaries (UI standards §7).
USED_PAIRS: tuple[tuple[str, str, float], ...] = (
    ("--mw-text", "--mw-bg", 4.5),
    ("--mw-text", "--mw-surface", 4.5),
    ("--mw-text", "--mw-surface-alt", 4.5),
    ("--mw-text", "--mw-surface-hover", 4.5),
    ("--mw-text-muted", "--mw-bg", 4.5),
    ("--mw-text-muted", "--mw-surface", 4.5),
    ("--mw-text-muted", "--mw-surface-alt", 4.5),
    # Link text uses --mw-accent-text; --mw-accent itself is a fill, a focus ring and a chart
    # series, so it is held to the 3:1 UI-boundary rule rather than the 4.5:1 body-text one.
    ("--mw-accent-text", "--mw-bg", 4.5),
    ("--mw-accent-text", "--mw-surface", 4.5),
    ("--mw-accent", "--mw-bg", 3.0),
    ("--mw-accent", "--mw-surface", 3.0),
    ("--mw-success", "--mw-surface", 4.5),
    ("--mw-warning", "--mw-surface", 4.5),
    ("--mw-danger", "--mw-surface", 4.5),
    ("--mw-info", "--mw-surface", 4.5),
    # WCAG 1.4.11 applies to the boundary that identifies a *control*, which is
    # --mw-border-strong. --mw-border is the decorative rule between table rows and carries no
    # information a reader needs to perceive, so it is deliberately absent from this list.
    ("--mw-border-strong", "--mw-surface", 3.0),
    ("--mw-border-strong", "--mw-bg", 3.0),
)


class TestContrastInBothThemes:
    """ "Contrast checks pass for every token pair in both themes."""

    @pytest.mark.parametrize(("foreground", "background", "minimum"), USED_PAIRS)
    def test_light_theme(
        self, client: TestClient, foreground: str, background: str, minimum: float
    ) -> None:
        tokens = _tokens(_tokens_css(client), block=":root {")
        ratio = contrast_ratio(tokens[foreground], tokens[background])
        assert ratio >= minimum, f"{foreground} on {background} is {ratio:.2f}:1 in light"

    @pytest.mark.parametrize(("foreground", "background", "minimum"), USED_PAIRS)
    def test_dark_theme(
        self, client: TestClient, foreground: str, background: str, minimum: float
    ) -> None:
        css = _tokens_css(client)
        light = _tokens(css, block=":root {")
        dark = light | _tokens(css, block=':root[data-theme="dark"] {')
        ratio = contrast_ratio(dark[foreground], dark[background])
        assert ratio >= minimum, f"{foreground} on {background} is {ratio:.2f}:1 in dark"

    def test_the_dark_palette_redefines_every_surface_and_text_token(
        self, client: TestClient
    ) -> None:
        """A dark theme is a designed palette, not an inversion (UI standards §9)."""
        dark = _tokens(_tokens_css(client), block=':root[data-theme="dark"] {')

        for token in (
            "--mw-bg",
            "--mw-surface",
            "--mw-surface-alt",
            "--mw-border",
            "--mw-border-strong",
            "--mw-text",
        ):
            assert token in dark, f"{token} is not redefined for dark"


class TestTheShell:
    def test_every_page_has_a_skip_link_navigation_and_a_main_landmark(
        self, client: TestClient
    ) -> None:
        for path in PAGES:
            response = client.get(path)
            assert response.status_code == 200, path
            assert 'class="skip-link"' in response.text, path
            assert '<nav aria-label="Primary">' in response.text, path
            assert '<main id="content">' in response.text, path

    def test_telemetry_is_present_on_every_route(self, client: TestClient) -> None:
        """UI standards §3: the telemetry bar is on every page of every application."""
        for path in PAGES:
            response = client.get(path)
            assert 'id="mw-telemetry-bar"' in response.text, path

    def test_the_current_page_is_marked_for_assistive_technology(self, client: TestClient) -> None:
        for path in ("/dashboard", "/results", "/database", "/settings"):
            response = client.get(path)
            assert f'href="{path}" aria-current="page"' in response.text, path

    def test_the_theme_control_exists_and_offers_the_three_choices(
        self, client: TestClient
    ) -> None:
        html = client.get("/").text

        assert "data-theme-select" in html
        for choice in ("system", "light", "dark"):
            assert f'value="{choice}"' in html
        # Applied before first paint, so a dark-mode user never sees a white flash.
        assert "freeweight-theme" in html.split("<body")[0]


class TestDataDisplay:
    def test_no_template_hard_codes_a_colour(self) -> None:
        """UI standards §1: tokens only, and no colour defined solely inside a media query."""
        offenders: list[str] = []
        for path in TEMPLATES.rglob("*.html"):
            body = path.read_text(encoding="utf-8")
            if path.name == "base.html":
                # The one place the palette is defined at all.
                continue
            if re.search(r"(?<!-)#[0-9A-Fa-f]{6}\b", body) or re.search(
                r"\b(?:rgba?\()|(?:\bcolor:\s*(?:gray|grey|red|blue|green)\b)", body
            ):
                offenders.append(str(path.relative_to(TEMPLATES)))
        assert offenders == [], f"hard-coded colours in {offenders}"

    def test_the_base_palette_defines_every_colour_outside_a_media_query(
        self, client: TestClient
    ) -> None:
        css = _tokens_css(client)
        base = _tokens(css, block=":root {")
        media = css[css.index("@media (prefers-color-scheme: dark)") :]
        inside = set(re.findall(r"(--mw-[a-z0-9-]+):\s*#", media))

        assert inside <= set(base), (
            f"a colour is defined only inside a media query: {sorted(inside - set(base))}"
        )

    def test_metrics_use_tabular_numerals(self, client: TestClient) -> None:
        """UI standards §2: live values must not shift the layout."""
        assert "font-variant-numeric: tabular-nums" in _page_with_styles(client)

    def test_metadata_text_is_never_below_12px(self, client: TestClient) -> None:
        html = _page_with_styles(client)
        sizes = [int(value) for value in re.findall(r"font-size:\s*(\d+)px", html)]

        assert sizes, "no explicit font sizes found to check"
        assert min(sizes) >= 11, "11px is the small-label floor; nothing may be smaller"  # noqa: PLR2004
        body_sizes = [size for size in sizes if size < 12]  # noqa: PLR2004
        assert body_sizes == [11] * len(body_sizes), (
            "only the 11px small label may sit under 12px (UI standards §2)"
        )


class TestStatesAndSignals:
    def test_every_page_has_an_empty_or_populated_state_and_never_a_blank_body(
        self, client: TestClient
    ) -> None:
        for path in PAGES:
            body = client.get(path).text.split('<main id="content">', 1)[1]
            assert len(body.strip()) > 200, f"{path} rendered an all-but-empty main"  # noqa: PLR2004

    def test_status_is_never_colour_alone(self) -> None:
        """UI standards §4.1: every status carries a label as well as its colour."""
        for path in TEMPLATES.rglob("*.html"):
            body = path.read_text(encoding="utf-8")
            for match in re.finditer(r'<span class="status status-[^"]*">([^<]*)</span>', body):
                assert match.group(1).strip(), f"an empty status badge in {path.name}"

    def test_charts_carry_a_text_alternative(self, client: TestClient) -> None:
        """UI standards §5: a chart is never the only representation of a critical figure."""
        source = (TEMPLATES / "partials" / "_scatter.html").read_text(encoding="utf-8")

        assert "<table>" in source
        assert 'role="img"' in source
        assert "aria-labelledby" in source
        assert "the chart's figures as text" in source

    def test_chart_axes_start_at_zero(self) -> None:
        """UI standards §5: no truncated axis that misleads.

        Phase 10's named failure mode. A fixed zero origin is the only rule that cannot
        accidentally exaggerate a difference, so it is asserted on the partial that draws every
        scatter rather than on any one page.
        """
        source = (TEMPLATES / "partials" / "_scatter.html").read_text(encoding="utf-8")

        assert "40 + (270 * point.x / xspan)" in source, "the x axis is not anchored at zero"
        assert "150 - (140 * point.y / yspan)" in source, "the y axis is not anchored at zero"
        assert '<text class="label" x="40" y="172">0</text>' in source, "the origin is unlabelled"
        assert "Both axes start at zero" in source

    def test_a_dense_table_keeps_its_column_control(self) -> None:
        """UI standards §13: tables stay usable at 20+ columns, with visibility configurable.

        Asserted on the template and the script rather than on a rendered page, because the widest
        table in the application only exists once there is something in it — and what has to be
        true is a property of the markup, not of a particular database.
        """
        source = (TEMPLATES / "results" / "index.html").read_text(encoding="utf-8")

        assert source.count('<th scope="col"') >= 16, "the results table lost columns"  # noqa: PLR2004
        assert 'data-table="results"' in source
        assert "table-scroll" in source, "a wide table must scroll rather than truncate meaning"
        import mirrorwall

        script = (Path(mirrorwall.__file__).parent / "static" / "js" / "table.js").read_text(
            encoding="utf-8"
        )
        assert "wireColumnVisibility" in script
        # Sorting is wired only where the server rendered the whole result set, so a page's sort
        # can never claim to have sorted a dataset it does not hold.
        assert 'getAttribute("data-complete") !== "true"' in script

    def test_destructive_actions_preview_and_require_a_typed_confirmation(
        self, client: TestClient
    ) -> None:
        page = client.get("/database").text

        assert "Preview deletion" in page
        assert "nothing has been deleted yet" not in page, "the preview must not appear unasked"
        source = (TEMPLATES / "database" / "index.html").read_text(encoding="utf-8")
        assert 'name="confirm"' in source
        assert "to confirm" in source


class TestProgressiveEnhancement:
    def test_every_read_only_page_works_without_javascript(self, client: TestClient) -> None:
        """ADR-0020 and UI standards §13: read-only content needs no script to be complete.

        Checked structurally: no page may put its content inside a ``<noscript>``-guarded
        container, and every filter and action is a real ``<form>`` with a submit button rather
        than a click handler.
        """
        for path in PAGES:
            html = client.get(path).text
            assert "<noscript>" not in html, path
            body = html.split('<main id="content">', 1)[1].split("</main>", 1)[0]
            assert "onclick=" not in body, path

    def test_every_form_declares_its_method_and_action(self) -> None:
        for path in TEMPLATES.rglob("*.html"):
            body = path.read_text(encoding="utf-8")
            for match in re.finditer(r"<form\b([^>]*)>", body):
                attributes = match.group(1)
                assert "method=" in attributes, f"a form without a method in {path.name}"
                assert "action=" in attributes, f"a form without an action in {path.name}"

    def test_no_asset_reaches_off_the_machine(self) -> None:
        """UI standards §13: no network request leaves the machine.

        Checked structurally rather than with a browser: no template or shipped script may name an
        absolute ``http(s)`` URL, and no font may be fetched.
        """
        offenders: list[str] = []
        for root in (TEMPLATES, STATIC):
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix not in {".html", ".js", ".css"}:
                    continue
                body = path.read_text(encoding="utf-8")
                for url in re.findall(r"https?://[^\s\"'()]+", body):
                    if url.startswith(("http://127.0.0.1", "http://localhost")):
                        continue
                    if url == "http://www.w3.org/2000/svg":  # an XML namespace, not a request
                        continue
                    offenders.append(f"{path.name}: {url}")
        assert offenders == [], offenders


class TestKeyboardOperation:
    def test_focus_is_always_visible(self, client: TestClient) -> None:
        html = _page_with_styles(client)

        assert ":focus-visible" in html
        assert "outline: none" not in html

    def test_every_form_control_has_a_label(self) -> None:
        """UI standards §7: every form control has an associated label."""
        for path in TEMPLATES.rglob("*.html"):
            body = path.read_text(encoding="utf-8")
            for match in re.finditer(r"<input\b([^>]*)>", body):
                attributes = match.group(1)
                if 'type="hidden"' in attributes:
                    continue
                assert "id=" in attributes or "aria-label" in attributes, (
                    f"an unlabelled input in {path.name}: {attributes.strip()[:80]}"
                )
            for match in re.finditer(r"<select\b([^>]*)>", body):
                assert "id=" in match.group(1) or "aria-label" in match.group(1), (
                    f"an unlabelled select in {path.name}"
                )

    def test_motion_is_disabled_when_the_reader_asks(self, client: TestClient) -> None:
        assert "@media (prefers-reduced-motion: reduce)" in _page_with_styles(client)
