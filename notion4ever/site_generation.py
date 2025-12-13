import json
import logging
import shutil
from pathlib import Path
from urllib.parse import urljoin

import dateutil.parser as dt_parser
import jinja2
import markdown
import sass

from notion4ever.structuring import clean_url_string

import os
import re
from pathlib import Path
from urllib.parse import quote

_WIN_ABS = re.compile(r"^[a-zA-Z]:[\\/]")
_POSIX_ABS = re.compile(r"^/")

def _as_url_path(p: str) -> str:
    # filesystem -> url (слэши + безопасный url-encode)
    p = p.replace("\\", "/")
    return quote(p, safe="/:._-~")

def _strip_output_dir_prefix(target: str, output_dir: Path) -> str:
    """
    Если target уже содержит output_dir (абсолютно или как хвост), отрезаем его.
    Возвращаем путь ВНУТРИ output_dir.
    """
    t = str(target).replace("\\", "/").strip()
    out = str(output_dir.resolve()).replace("\\", "/").rstrip("/")

    # abs: C:/.../_site/root_x/download.png -> root_x/download.png (если output_dir=_site)
    if t.startswith(out + "/"):
        return t[len(out) + 1 :]

    # fallback: если где-то встречается "/<output_dir.name>/"
    needle = "/" + output_dir.name.strip("/\\") + "/"
    pos = t.find(needle)
    if pos != -1:
        return t[pos + len(needle) :]

    return t

def to_rel_url(from_html_path: Path, target: str | None, output_dir: Path) -> str | None:
    """
    Делает корректный относительный URL от html файла до target.
    Работает и на Windows, и на Linux.
    """
    if not target:
        return target

    s = str(target).strip()

    # remote/data — не трогаем
    if s.startswith(("http://", "https://", "data:")):
        return s

    out_dir = output_dir.resolve()
    html_dir = from_html_path.parent.resolve()

    # если target содержит output_dir — отрежем
    s2 = _strip_output_dir_prefix(s, out_dir)

    # если target абсолютный FS путь — relpath от html_dir
    if _WIN_ABS.match(s2) or _POSIX_ABS.match(s2) or Path(s2).is_absolute():
        rel = os.path.relpath(s2, start=str(html_dir))
        return _as_url_path(rel)

    # иначе считаем, что это путь внутри output_dir
    fs_target = (out_dir / s2.lstrip("/")).resolve()
    rel = os.path.relpath(str(fs_target), start=str(html_dir))
    return _as_url_path(rel)

def rewrite_abs_src_href(html: str, html_path: Path, output_dir: Path) -> str:
    """
    Чинит src/href в html_content, если markdown->html уже вставил абсолютные FS пути.
    """
    def repl(m):
        attr = m.group(1)
        url = m.group(2)
        fixed = to_rel_url(html_path, url, output_dir)
        return f'{attr}="{fixed}"'

    return re.sub(r'(src|href)\s*=\s*"([^"]+)"', repl, html)



def verify_templates(config: dict):
    """Verifies existense and content of sass and templates dirs."""
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
    """Generates css file (compiling sass files in the output_dir folder)."""
    out_css = Path(config["output_dir"]) / "css"
    out_css.mkdir(parents=True, exist_ok=True)
    sass.compile(dirname=(config["sass_dir"], out_css))


def generate_404(structured_notion: dict, config: dict):
    """Generates 404 html page."""
    out_dir = Path(config["output_dir"]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    tml = (Path(config["templates_dir"]) / "404.html").read_text(encoding="utf-8")
    jinja_loader = jinja2.FileSystemLoader(config["templates_dir"])
    jtml = jinja2.Environment(loader=jinja_loader).from_string(tml)
    html_page = jtml.render(content="", site=structured_notion)

    path_404 = out_dir / "404.html"
    path_404.parent.mkdir(parents=True, exist_ok=True)
    with open(path_404, "w+", encoding="utf-8") as f:
        f.write(html_page)


def generate_archive(structured_notion: dict, config: dict):
    """Generates archive page."""
    out_dir = Path(config["output_dir"]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if config["build_locally"]:
        archive_link = "Archive.html"
        structured_notion["archive_url"] = str(out_dir / archive_link)
        archive_path = out_dir / archive_link
    else:
        archive_link = "Archive/index.html"
        structured_notion["archive_url"] = urljoin(structured_notion["base_url"], archive_link)
        archive_path = out_dir / "Archive" / "index.html"

    archive_path.parent.mkdir(parents=True, exist_ok=True)

    tml = (Path(config["templates_dir"]) / "archive.html").read_text(encoding="utf-8")
    jinja_loader = jinja2.FileSystemLoader(config["templates_dir"])
    jtemplate = jinja2.Environment(loader=jinja_loader).from_string(tml)
    html_page = jtemplate.render(content="", site=structured_notion)

    with open(archive_path, "w+", encoding="utf-8") as f:
        f.write(html_page)


def str_to_dt(structured_notion: dict):
    for page_id, page in structured_notion["pages"].items():
        for field in ["date", "date_end", "last_edited_time"]:
            if field in page:
                structured_notion["pages"][page_id][field] = dt_parser.isoparse(page[field])


def generate_page(page_id: str, structured_notion: dict, config: dict):
    page = structured_notion["pages"][page_id]
    page_url = page["url"]

    # ✅ Сейвим md-имя от Windows/URL-символов и пустых тайтлов
    md_filename = clean_url_string(page.get("title"), fallback=f"untitled_{page_id[:8]}") + ".md"

    output_dir = Path(config["output_dir"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # page["url"] в локальном режиме — абсолютный путь к HTML-файлу
    page_path = Path(page_url)

    # Иногда page_url может быть странным — страхуемся
    try:
        folder_path = page_path.parent
    except Exception:
        folder_path = output_dir

    try:
        rel_folder = folder_path.relative_to(output_dir)
    except ValueError:
        rel_folder = Path(".")

    local_file_location = str(rel_folder)
    html_filename = clean_url_string(page_path.name, fallback="index")  # на всякий

    logging.debug(
        f"🤖 MD {Path(local_file_location) / md_filename}; "
        f"HTML {Path(local_file_location) / html_filename}"
    )

    base_dir = (output_dir / Path(local_file_location)).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)

    # ✅ Markdown
    md_path = (base_dir / md_filename).resolve()
    md_path.parent.mkdir(parents=True, exist_ok=True)

    with open(md_path, "w+", encoding="utf-8") as f:
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
        f.write(md_content)

    # ✅ HTML
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

    tml = (Path(config["templates_dir"]) / "page.html").read_text(encoding="utf-8")
    html_path = (base_dir / html_filename).resolve()
    html_path.parent.mkdir(parents=True, exist_ok=True)

    output_dir = Path(config["output_dir"]).resolve()

    # ✅ чинит <img src="C:\..."> и <a href="C:\..."> внутри контента
    html_content = rewrite_abs_src_href(html_content, html_path, output_dir)

    # ✅ чинит cover/icon, если они были filesystem path
    page_for_template = dict(page)
    page_for_template["cover"] = to_rel_url(html_path, page.get("cover"), output_dir)
    page_for_template["icon"]  = to_rel_url(html_path, page.get("icon"),  output_dir)

    with open(html_path, "w+", encoding="utf-8") as f:
        jinja_loader = jinja2.FileSystemLoader(config["templates_dir"])
        jtemplate = jinja2.Environment(loader=jinja_loader).from_string(tml)
        html_page = jtemplate.render(content=html_content, page=page_for_template, site=structured_notion)
        f.write(html_page)



def generate_pages(structured_notion: dict, config: dict):
    # ✅ Чтобы один сломанный документ не убивал весь бэкап
    for page_id in structured_notion["pages"].keys():
        try:
            generate_page(page_id, structured_notion, config)
        except Exception as e:
            logging.error(f"🤖 Failed to generate page {page_id}: {e}", exc_info=True)


def generate_search_index(structured_notion: dict, config: dict):
    """Generates search index file if building for server"""
    if not config["build_locally"] and structured_notion.get("search_index"):
        out_dir = Path(config["output_dir"]).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        search_index_path = out_dir / "search_index.json"
        with open(search_index_path, "w", encoding="utf-8") as f:
            json.dump(structured_notion["search_index"], f, ensure_ascii=False)

        # Update the search_index to just contain the path
        structured_notion["search_index"] = "search_index.json"


def generate_site(structured_notion: dict, config: dict):
    verify_templates(config)
    logging.debug("🤖 SASS and templates are verified.")

    generate_css(config)
    logging.debug("🤖 SASS translated to CSS folder.")

    generate_search_index(structured_notion, config)
    logging.debug("🤖 Generated search index file.")

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
