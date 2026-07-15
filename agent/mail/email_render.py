"""HTML email rendering for Elixir — markdown body → styled HTML (ported from Oliver).

Pure render: the markdown renderer escapes any raw HTML the model emits, so a stray
`<script>` or `<tag>` can never become live HTML in the email.
"""

from __future__ import annotations

import html

import markdown as _markdown
from markdown.extensions import Extension as _Extension
from markdown.postprocessors import RawHtmlPostprocessor as _RawHtmlPostprocessor


class _EscapeRawHtmlPostprocessor(_RawHtmlPostprocessor):
    """Escape any raw HTML the model emitted instead of passing it through — so `<finished>`
    becomes literal `&lt;finished&gt;` and a stray `<script>` can never be live HTML."""

    def run(self, text: str) -> str:
        for i in range(self.md.htmlStash.html_counter):
            raw = self.md.htmlStash.rawHtmlBlocks[i]
            text = text.replace(
                self.md.htmlStash.get_placeholder(i), html.escape(str(raw))
            )
        return text


class _EscapeRawHtmlExtension(_Extension):
    def extendMarkdown(self, md) -> None:  # noqa: N802 (markdown API name)
        md.postprocessors.register(_EscapeRawHtmlPostprocessor(md), "raw_html", 30)


def _render_markdown(text: str) -> str:
    """Render Elixir's markdown body to email-safe HTML. Markdown does its own escaping
    (code spans render as proper <code>, bare angle brackets stay literal); the escape
    extension neutralizes raw HTML. No nl2br: single mid-sentence newlines collapse to a
    space, paragraphs come from blank lines."""
    return _markdown.markdown(
        text or "", extensions=["sane_lists", "tables", _EscapeRawHtmlExtension()]
    )


# Email CSS. Delivered as a <style> block (well-supported in Apple Mail, Gmail, most
# modern clients); the .elixir-email wrapper scopes it so it can't bleed into quoted
# replies. Tuned for readable long-form release notes: clear section headers, breathing
# room between points.
_EMAIL_CSS = (
    ".elixir-email{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,"
    "Arial,sans-serif;font-size:16px;line-height:1.55;color:#2a2a2a;max-width:640px;"
    "margin:0 auto;padding:8px 2px;}"
    ".elixir-email p{margin:0 0 16px;}"
    ".elixir-email h2{font-size:19px;font-weight:700;color:#111;margin:28px 0 10px;"
    "padding-bottom:5px;border-bottom:1px solid #ececec;}"
    ".elixir-email h3{font-size:18px;font-weight:700;color:#111;margin:16px 0 4px;}"
    ".elixir-email ol,.elixir-email ul{padding-left:24px;margin:0 0 16px;}"
    ".elixir-email li{margin-bottom:12px;padding-left:4px;}"
    ".elixir-email strong{font-weight:600;color:#111;}"
    ".elixir-email hr{border:0;border-top:1px solid #ececec;margin:24px 0;}"
    ".elixir-email a{color:#7b3fe4;}"  # elixir purple
    # Inline card/badge art (icons ~512px on the CDN) sized down to sit in a line.
    ".elixir-email img{max-height:44px;width:auto;vertical-align:middle;margin-right:8px;}"
    # Full battle-log + stat tables.
    ".elixir-email table{border-collapse:collapse;width:100%;font-size:14px;margin:12px 0;}"
    ".elixir-email th,.elixir-email td{border-bottom:1px solid #eee;padding:6px 8px;text-align:left;}"
    ".elixir-email th{color:#666;font-weight:600;border-bottom:2px solid #e4e4e4;}"
    ".elixir-sig{margin-top:28px;padding-top:12px;border-top:1px solid #e4e4e4;"
    "font-size:14px;color:#555;}"
    ".elixir-sig p{margin:0 0 4px;}"
    ".elixir-sig a{color:#7b3fe4;text-decoration:none;}"
)


def text_to_html(text: str, *, signature_html: str | None = None) -> str:
    """Render the markdown body to an email-safe HTML document. `signature_html`, when given,
    is appended inside the wrapper as its own styled footer (trusted HTML, NOT markdown-rendered)."""
    body = _render_markdown(text) or "<p></p>"
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<style>{_EMAIL_CSS}</style></head>"
        '<body><div class="elixir-email">'
        f"{body}{signature_html or ''}</div></body></html>"
    )
