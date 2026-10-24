"""Public asset caching must never cache private pages or issue owner cookies."""

from test_dashboard import DashboardTests


class WorkspaceCacheTests(DashboardTests):
    def test_only_versioned_static_assets_are_cacheable(self):
        self.owner()
        html = self.client.get("/")
        self.assertEqual(html.headers["Cache-Control"], "no-store")
        import html as html_parser
        import re

        # Parse the rendered asset URL rather than assuming the build version.
        css = html_parser.unescape(
            re.search(rb'<link[^>]+href="([^"]+)"', html.data)[1].decode()
        )
        anonymous = self.app.test_client()
        response = anonymous.get(css)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["Cache-Control"], "public, max-age=31536000, immutable"
        )
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(
            anonymous.get("/static/msj.css").headers["Cache-Control"], "no-store"
        )
        self.assertEqual(
            anonymous.get("/operations.json").headers["Cache-Control"], "no-store"
        )
