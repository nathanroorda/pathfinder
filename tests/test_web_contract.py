import os
import re
import unittest

from tests import support

REPO = support.REPO_ROOT
SCRIPT_JS = os.path.join(REPO, "web", "script.js")
INDEX_HTML = os.path.join(REPO, "web", "index.html")
APP_PY = os.path.join(REPO, "app", "app.py")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def normalise(path):
    path = path.split("?")[0]
    path = re.sub(r"\$\{[^}]*\}", "{}", path)   # JS interpolation
    path = re.sub(r"\{[^}]*\}", "{}", path)     # FastAPI path param
    return path.rstrip("/") or "/"


class ApiRoutes(unittest.TestCase):
    def setUp(self):
        self.js = read(SCRIPT_JS)
        self.py = read(APP_PY)

    def client_paths(self):
        return {normalise(p)
                for p in re.findall(r"""["'`](/api/[^"'`]*)["'`]""", self.js)}

    def server_routes(self):
        return {normalise(path) for _, path in re.findall(
            r"""@app\.(get|post|put|patch|delete)\(\s*["']([^"']+)["']""", self.py)}

    def test_the_parsers_found_something(self):
        self.assertGreaterEqual(len(self.client_paths()), 8)
        self.assertGreaterEqual(len(self.server_routes()), 8)

    def test_every_endpoint_the_browser_calls_exists(self):
        missing = self.client_paths() - self.server_routes()
        self.assertEqual(missing, set(),
                         f"web/script.js calls routes app/app.py does not define: {missing}")

    def test_the_known_client_endpoints_are_all_wired_up(self):
        self.assertEqual(self.client_paths(), {
            "/api/status", "/api/capture", "/api/bulb", "/api/liveview",
            "/api/record/start", "/api/record/stop", "/api/autofocus",
            "/api/focus", "/api/afpoint", "/api/magnifier", "/api/telemetry",
            "/api/settings", "/api/settings/{}",
        })

    def test_server_routes_not_used_by_the_browser_are_accounted_for(self):
        self.assertEqual(self.server_routes() - self.client_paths(),
                         {"/api/connect"})


class DomIds(unittest.TestCase):
    def setUp(self):
        self.js = read(SCRIPT_JS)
        self.html = read(INDEX_HTML)

    def test_every_element_the_script_grabs_exists_in_the_page(self):
        wanted = set(re.findall(r"""getElementById\(["']([^"']+)["']\)""", self.js))
        present = set(re.findall(r"""\bid=["']([^"']+)["']""", self.html))

        self.assertGreaterEqual(len(wanted), 10)
        self.assertEqual(wanted - present, set())

    def test_the_script_is_loaded_by_the_page(self):
        self.assertIn("script.js", self.html)
        self.assertIn("style.css", self.html)


class MagnifierControl(unittest.TestCase):
    def setUp(self):
        self.html = read(INDEX_HTML)
        self.js = read(SCRIPT_JS)

    def tag(self):
        found = re.search(r"<select[^>]*id=[\"']magnifier[\"'][^>]*>.*?</select>",
                          self.html, re.DOTALL)
        self.assertIsNotNone(found, "the magnifier select has gone missing")
        return found.group(0)

    def test_the_page_ships_no_magnifications_of_its_own(self):
        # The levels are the body's; a hardcoded <option> would offer a
        # magnification some other camera does not have.
        self.assertNotIn("<option", self.tag())

    def test_it_starts_hidden_so_an_unsupported_body_shows_no_control(self):
        self.assertIn("hidden", self.tag())

    def test_no_level_is_named_anywhere_in_the_client(self):
        block = self.js[self.js.index("function magnifierLabel"):
                        self.js.index("bulbBtn.addEventListener")]
        for level in ('"Off"', "'Off'", '"5.5"', '"11"'):
            with self.subTest(level=level):
                self.assertNotIn(level, block)


class SettingKinds(unittest.TestCase):
    def test_the_browser_renders_every_kind_the_backend_emits(self):
        from camera import gp2

        js = read(SCRIPT_JS)
        block = js[js.index("const settingRenderers"):js.index("function renderSettings")]
        renderers = set(re.findall(r"^\s{2}(\w+):", block, re.MULTILINE))

        self.assertEqual(renderers, set(gp2._KIND.values()))


class BulbLimits(unittest.TestCase):
    def setUp(self):
        self.html = read(INDEX_HTML)
        self.py = read(APP_PY)

    def bulb_input_attr(self, attr):
        tag = re.search(r"<input[^>]*id=[\"']bulbSeconds[\"'][^>]*>", self.html)
        self.assertIsNotNone(tag, "the bulb seconds input has gone missing")
        found = re.search(rf"""\b{attr}=["']([^"']+)["']""", tag.group(0))
        self.assertIsNotNone(found, f"bulb input has no {attr} attribute")
        return float(found.group(1))

    def server_default_ceiling(self):
        found = re.search(r"^DEFAULT_MAX_BULB_SECONDS\s*=\s*([\d.]+)", self.py,
                          re.MULTILINE)
        self.assertIsNotNone(found)
        return float(found.group(1))

    def test_the_input_ceiling_matches_the_server_default(self):
        self.assertEqual(self.bulb_input_attr("max"), self.server_default_ceiling())

    def test_the_input_floor_is_above_the_servers_exclusive_zero(self):
        self.assertGreater(self.bulb_input_attr("min"), 0)


if __name__ == "__main__":
    unittest.main()
