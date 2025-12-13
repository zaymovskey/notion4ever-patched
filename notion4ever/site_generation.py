import json
import logging
import os
import re
import shutil
from pathlib import Path
from urllib.parse import quote

import dateutil.parser as dt_parser
import jinja2
import markdown
import sass
from markupsafe import Markup  # ✅ важно

from notion4ever.structuring import clean_url_string

_WIN_ABS = re.compile(r"^[a-zA-Z]:[\\/]")
_POSIX_ABS = re.compile(r"^/")


# ---------------------------
# URL helpers
# ---------------------------

def _as_url_path(p: str) -> str:
    p = p.replace("\\", "/")
    return quote(p, safe="/:._-~")


def _is_remote_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "data:"))


def _assets_prefix(html_path: Path, output_dir: Path) -> str:
    """
    Возвращает префикс до корня output_dir (где лежат css/, search_index.json, и т.п.)
    Примеры:
      - output_dir/index.html            -> ""
      - output_dir/Folder/index.html     -> "../"
      - output_dir/A/B/page.html         -> "../../"
    """
    out_dir = output_dir.resolve()
    html_dir = html_path.parent.resolve()

    rel = os.path.relpath(str(out_dir), start=str(html_dir)).replace(os.sep, "/")
    if rel == ".":
        return ""
    return rel.rstrip("/") + "/"


def to_rel_url(from_html_path: Path, target: str | None, output_dir: Path) -> str | None:
    if not target:
        return target

    s = str(target).strip()
    if not s:
        return target

    if _is_remote_url(s):
        return s

    out_dir = output_dir.resolve()
    html_dir = from_html_path.parent.resolve()

    # 1) абсолютный FS путь -> relpath от html_dir
    if _WIN_ABS.match(s) or _POSIX_ABS.match(s) or Path(s).is_absolute():
        rel = os.path.relpath(s, start=str(html_dir))
        return _as_url_path(rel)

    # 2) иначе считаем, что это путь внутри output_dir
    fs_target = (out_dir / s.lstrip("/")).resolve()
    rel = os.path.relpath(str(fs_target), start=str(html_dir))
    return _as_url_path(rel)


def rewrite_abs_src_href(html: str, html_path: Path, output_dir: Path) -> str:
    def repl(m):
        attr = m.group(1)
        url = m.group(2)
        fixed = to_rel_url(html_path, url, output_dir)
        return f'{attr}="{fixed}"'
    return re.sub(r'(src|href)\s*=\s*"([^"]+)"', repl, html)


# ---------------------------
# build helpers
# ---------------------------

def verify_templates(config: dict):
    sass_dir = Path(config["sass_dir"])
    templates_dir = Path(config["templates_dir"])

    if sass_dir.is_dir() and any(sass_dir.iterdir()):
        logging.debug("🤖 Sass directory is OK")
    else:
        logging.critical("🤖 Sass directory is not found or empty.")

    if templates_dir.is_dir() and any(templates_dir.iterdir()):
        logging.debug("🤖 Templates directory is OK")
    else:
        logging.critical("🤖 Templates directory is not found or empty.")


def generate_css(config: dict):
    out_css = Path(config["output_dir"]) / "css"
    out_css.mkdir(parents=True, exist_ok=True)
    sass.compile(dirname=(config["sass_dir"], out_css))


def _jinja_env(templates_dir: str | Path) -> jinja2.Environment:
    loader = jinja2.FileSystemLoader(str(templates_dir))
    # ✅ autoescape оставляем (это правильно), а контент помечаем Markup
    return jinja2.Environment(loader=loader, autoescape=True)


def str_to_dt(structured_notion: dict):
    for page_id, page in structured_notion["pages"].items():
        for field in ["date", "date_end", "last_edited_time"]:
            if field in page and page[field]:
                structured_notion["pages"][page_id][field] = dt_parser.isoparse(page[field])


def _render_template(template_name: str, *, templates_dir: str, **ctx) -> str:
    env = _jinja_env(templates_dir)
    tml = (Path(templates_dir) / template_name).read_text(encoding="utf-8")
    tpl = env.from_string(tml)
    return tpl.render(**ctx)


def generate_404(structured_notion: dict, config: dict):
    out_dir = Path(config["output_dir"]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    path_404 = out_dir / "404.html"
    path_404.parent.mkdir(parents=True, exist_ok=True)

    assets_prefix = _assets_prefix(path_404, out_dir)

    html_page = _render_template(
        "404.html",
        templates_dir=config["templates_dir"],
        content=Markup(""),
        site=structured_notion,
        assets_prefix=assets_prefix,
    )
    path_404.write_text(html_page, encoding="utf-8")


def generate_archive(structured_notion: dict, config: dict):
    out_dir = Path(config["output_dir"]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    archive_rel = "Archive/index.html"
    structured_notion["archive_url"] = archive_rel

    archive_path = out_dir / archive_rel
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    assets_prefix = _assets_prefix(archive_path, out_dir)

    html_page = _render_template(
        "archive.html",
        templates_dir=config["templates_dir"],
        content=Markup(""),
        site=structured_notion,
        assets_prefix=assets_prefix,
    )
    archive_path.write_text(html_page, encoding="utf-8")

    # плоская совместимость (не обязательно)
    if config.get("build_locally", True):
        flat = out_dir / "Archive.html"
        flat.write_text(
            '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=Archive/index.html">',
            encoding="utf-8",
        )


# ---------------------------
# page generation
# ---------------------------

def generate_page(page_id: str, structured_notion: dict, config: dict):
    page = structured_notion["pages"][page_id]

    output_dir = Path(config["output_dir"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    page_url = page.get("url")
    if not page_url:
        raise RuntimeError(f"Page {page_id} has no url")

    # 🔥 пишем строго по page["url"]
    html_path = output_dir / page_url
    html_path.parent.mkdir(parents=True, exist_ok=True)

    # md рядом с html
    md_path = html_path.with_suffix(".md")

    metadata = (
        "---\n"
        f"title: {page.get('title')}\n"
        f"cover: {page.get('cover')}\n"
        f"icon: {page.get('icon')}\n"
        f"emoji: {page.get('emoji')}\n"
    )
    if "properties_md" in page:
        for p_title, p_md in page["properties_md"].items():
            metadata += f"{p_title}: {p_md}\n"
    metadata += "---\n\n"

    md_content = metadata + (page.get("md_content") or "")
    md_path.write_text(md_content, encoding="utf-8")

    # markdown -> html body
    html_content = markdown.markdown(
        md_content,
        extensions=[
            "meta",
            "tables",
            "mdx_truly_sane_lists",
            "markdown_captions",
            "pymdownx.tilde",
            "pymdownx.tasklist",
            "pymdownx.superfences",
        ],
        extension_configs={
            "mdx_truly_sane_lists": {
                "nested_indent": 4,
                "truly_sane": True,
            },
            "pymdownx.tasklist": {
                "clickable_checkbox": True,
            },
        },
    )

    # чинит абсолютные src/href, если они протекли
    html_content = rewrite_abs_src_href(html_content, html_path, output_dir)

    # cover/icon делаем относительными к текущей html
    page_for_template = dict(page)
    page_for_template["cover"] = to_rel_url(html_path, page.get("cover"), output_dir)
    page_for_template["icon"] = to_rel_url(html_path, page.get("icon"), output_dir)

    assets_prefix = _assets_prefix(html_path, output_dir)

    html_page = _render_template(
        "page.html",
        templates_dir=config["templates_dir"],
        content=Markup(html_content),  # ✅ чтобы не печатались <h1> как текст
        page=page_for_template,
        site=structured_notion,
        assets_prefix=assets_prefix,   # ✅ для css/js/search
    )
    html_path.write_text(html_page, encoding="utf-8")


def generate_pages(structured_notion: dict, config: dict):
    for page_id in structured_notion["pages"].keys():
        try:
            generate_page(page_id, structured_notion, config)
        except Exception as e:
            logging.error(f"🤖 Failed to generate page {page_id}: {e}", exc_info=True)


def generate_search_index(structured_notion: dict, config: dict):
    if not structured_notion.get("search_index"):
        return

    out_dir = Path(config["output_dir"]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    search_index_path = out_dir / "search_index.json"
    search_index_path.write_text(
        json.dumps(structured_notion["search_index"], ensure_ascii=False),
        encoding="utf-8",
    )

    structured_notion["search_index"] = "search_index.json"


def generate_site(structured_notion: dict, config: dict):
    verify_templates(config)
    logging.debug("🤖 SASS and templates are verified.")

    generate_css(config)
    logging.debug("🤖 SASS translated to CSS folder.")

    if config.get("include_search"):
        generate_search_index(structured_notion, config)
        logging.debug("🤖 Generated search index file.")
    else:
        structured_notion["search_index"] = ""

    out_dir = Path(config["output_dir"]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Fonts
    fonts_dst = out_dir / "css" / "fonts"
    if fonts_dst.exists():
        shutil.rmtree(fonts_dst)

    fonts_src = Path(config["sass_dir"]) / "fonts"
    if fonts_src.exists():
        shutil.copytree(fonts_src, fonts_dst)
        logging.debug("🤖 Copied fonts.")
    else:
        logging.warning("🤖 Fonts folder not found, skipped copying.")

    str_to_dt(structured_notion)
    logging.debug("🤖 Changed string in dates to datetime objects.")

    generate_archive(structured_notion, config)
    logging.info("🤖 Archive page generated.")

    generate_404(structured_notion, config)
    logging.info("🤖 404.html page generated.")

    generate_pages(structured_notion, config)
    logging.info("🤖 All html and md pages generated.")
