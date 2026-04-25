#!/usr/bin/env python3
"""prepperpi-usb-markdown — render Markdown files from
/srv/prepperpi/user-usb/ as styled HTML.

Caddy reverse_proxies any request matching /usb/*.md to this daemon
(127.0.0.1:8089). We map the URL back to a path under USB_ROOT,
render with python-markdown, and wrap in a small HTML shell that
inherits the landing-page stylesheet so rendered MD looks like the
rest of the site.
"""

from __future__ import annotations

import html
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import markdown

USB_ROOT = os.environ.get("PREPPERPI_USB_ROOT", "/srv/prepperpi/user-usb")
LISTEN_HOST = os.environ.get("PREPPERPI_USB_MD_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("PREPPERPI_USB_MD_PORT", "8089"))

# Compile the renderer once. `extra` enables tables, fenced code,
# footnotes, etc.; `sane_lists` makes mixed bullet types behave; we
# leave codehilite off by default to avoid pulling in pygments.
_md = markdown.Markdown(extensions=["extra", "sane_lists", "toc"])

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} &mdash; PrepperPi</title>
<link rel="stylesheet" href="/style.css">
<style>
.usb-page-wrap {{ max-width: 50rem; margin: 1rem auto; padding: 0 1rem; }}
.usb-breadcrumb {{ font-size: 0.85rem; color: var(--ink-dim); margin-bottom: 1rem; }}
.usb-breadcrumb a {{ color: var(--ink-dim); }}
.usb-md h1, .usb-md h2, .usb-md h3 {{ color: var(--ink); }}
.usb-md a {{ color: var(--accent); }}
.usb-md pre, .usb-md code {{ background: var(--panel); padding: 0.1rem 0.3rem; border-radius: 3px; }}
.usb-md pre {{ padding: 0.75rem; overflow: auto; }}
.usb-md table {{ border-collapse: collapse; }}
.usb-md th, .usb-md td {{ border: 1px solid var(--border); padding: 0.25rem 0.5rem; }}
.usb-md img {{ max-width: 100%; height: auto; }}
.usb-md blockquote {{ border-left: 3px solid var(--accent); padding-left: 1rem; color: var(--ink-dim); margin-left: 0; }}
</style>
</head>
<body>
<header class="site-header"><h1><a href="/" style="color:inherit;text-decoration:none;">PrepperPi</a></h1></header>
<div class="usb-page-wrap">
<p class="usb-breadcrumb"><a href="/">home</a> &raquo; {breadcrumb}</p>
<article class="usb-md">
{rendered}
</article>
</div>
</body>
</html>
"""


def _safe_resolve(rel: str) -> str | None:
    """Map a URL-relative path under /usb/ back to an absolute file
    path under USB_ROOT. Returns None for traversal attempts or
    paths that resolve outside the USB root."""
    if "\x00" in rel or ".." in rel.split("/"):
        return None
    full = os.path.realpath(os.path.join(USB_ROOT, rel))
    root = os.path.realpath(USB_ROOT)
    if full != root and not full.startswith(root + os.sep):
        return None
    return full


def _breadcrumb(rel: str) -> str:
    """Return an HTML breadcrumb trail for the given relative path,
    each segment a link back up the tree."""
    parts = [p for p in rel.split("/") if p]
    if not parts:
        return html.escape(rel)
    accum = "/usb"
    crumbs = [f'<a href="{accum}/">USB</a>']
    for i, p in enumerate(parts):
        accum += "/" + p
        if i == len(parts) - 1:
            crumbs.append(html.escape(p))
        else:
            crumbs.append(f'<a href="{html.escape(accum)}/">{html.escape(p)}</a>')
    return " &raquo; ".join(crumbs)


class MdHandler(BaseHTTPRequestHandler):
    server_version = "prepperpi-usb-md/1.0"

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        url_path = unquote(urlparse(self.path).path)
        if not url_path.startswith("/usb/"):
            self.send_error(HTTPStatus.NOT_FOUND, "not in /usb/")
            return
        rel = url_path[len("/usb/"):]
        full = _safe_resolve(rel)
        if full is None:
            self.send_error(HTTPStatus.FORBIDDEN, "path traversal")
            return
        if not os.path.isfile(full):
            self.send_error(HTTPStatus.NOT_FOUND, "no such file")
            return
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError as e:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"read failed: {e}")
            return

        _md.reset()
        rendered = _md.convert(src)

        title = os.path.basename(full)
        breadcrumb = _breadcrumb(rel)
        body = PAGE_TEMPLATE.format(
            title=html.escape(title),
            breadcrumb=breadcrumb,
            rendered=rendered,
        )
        payload = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):  # noqa: A003 (overriding builtin name)
        sys.stderr.write("[prepperpi-usb-md] " + fmt % args + "\n")


def main():
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), MdHandler)
    sys.stderr.write(
        f"[prepperpi-usb-md] listening on {LISTEN_HOST}:{LISTEN_PORT}, "
        f"root={USB_ROOT}\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
