import dateutil.parser as dt_parser
import logging
from urllib.parse import urljoin, urlparse, unquote
from urllib.error import HTTPError
from pathlib import Path
from notion4ever import markdown_parser
from urllib import request
from itertools import groupby
import re
import html
import os

def _to_site_rel_url(path: Path, *, root_out: Path) -> str:
    # path и root_out — filesystem пути
    rel = os.path.relpath(path, start=root_out)
    return rel.replace(os.sep, "/")


def strip_html_tags(text: str) -> str:
    """Remove HTML tags and normalize whitespace while preserving Unicode characters."""
    if not text:
        return ""

    clean = re.compile("<.*?>")
    text = re.sub(clean, " ", text)
    text = html.unescape(text)
    text = " ".join(text.split())
    return text


def clean_url_string(value, fallback="untitled") -> str:
    """
    Make a safe filename/url slug from title.
    Accepts None and non-string values.
    """
    if value is None:
        value = fallback
    value = str(value).strip()
    if not value:
        value = fallback

    for ch in ["$", "\\", ":", " "]:
        value = value.replace(ch, "_")

    # Remove forbidden chars (Windows + URL safety)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", value)

    # Windows hates trailing dots/spaces
    value = value.strip(" .")

    return value or fallback


def recursive_search(key, dictionary):
    """Recursive search for `key` in nested dictionaries/lists."""
    if hasattr(dictionary, "items"):
        for k, v in dictionary.items():
            if k == key:
                yield v
            if isinstance(v, dict):
                yield from recursive_search(key, v)
            elif isinstance(v, list):
                for d in v:
                    yield from recursive_search(key, d)


def _extract_notion_file_url(file_obj: dict) -> str | None:
    """
    Notion returns files in 2 variants:
      - {"type": "file", "file": {"url": ...}}
      - {"type": "external", "external": {"url": ...}}
    """
    if not isinstance(file_obj, dict):
        return None

    ftype = file_obj.get("type")
    if ftype == "file" and file_obj.get("file"):
        return file_obj["file"].get("url")
    if ftype == "external" and file_obj.get("external"):
        return file_obj["external"].get("url")
    return None


def parse_headers(raw_notion: dict) -> dict:
    """
    Parses raw notion dict and returns dict where keys are page_id,
    values contain:
      type, files, title, last_edited_time, date/date_end, parent, children, cover, emoji, icon
    """
    notion_pages: dict = {}

    for page_id, page in raw_notion.items():
        notion_pages[page_id] = {"files": [], "children": []}

        # Page type. Could be "page", "database" or "db_entry"
        notion_pages[page_id]["type"] = page["object"]
        if page["parent"]["type"] in ["database_id"]:
            notion_pages[page_id]["type"] = "db_entry"

        # Title
        ptype = notion_pages[page_id]["type"]
        if ptype == "page":
            title_arr = page.get("properties", {}).get("title", {}).get("title", [])
            notion_pages[page_id]["title"] = (
                title_arr[0].get("plain_text") if len(title_arr) > 0 else None
            )
        elif ptype == "database":
            db_title = page.get("title", [])
            notion_pages[page_id]["title"] = (
                db_title[0]["text"]["content"] if len(db_title) > 0 else None
            )
        elif ptype == "db_entry":
            res = list(recursive_search("title", page.get("properties", {})))
            res = res[0] if res else []
            if len(res) > 0:
                notion_pages[page_id]["title"] = markdown_parser.richtext_convertor(
                    res, title_mode=True
                )
            else:
                notion_pages[page_id]["title"] = None
                logging.warning("🤖Empty database entries could break the site building 😫.")

        # Time
        notion_pages[page_id]["last_edited_time"] = page.get("last_edited_time")

        # Optional Date property for db_entry
        if ptype == "db_entry":
            props = page.get("properties", {})
            if "Date" in props and props["Date"].get("date") is not None:
                notion_pages[page_id]["date"] = props["Date"]["date"].get("start")
                if props["Date"]["date"].get("end") is not None:
                    notion_pages[page_id]["date_end"] = props["Date"]["date"].get("end")

        # Parent
        parent = page.get("parent", {})
        parent_id = None

        if "workspace" in parent.keys():
            parent_id = None
        elif ptype in ["page", "database"]:
            parent_id = parent.get("page_id")
        elif ptype == "db_entry":
            parent_id = parent.get("database_id")

        notion_pages[page_id]["parent"] = parent_id

        # Attach as child
        if parent_id is not None:
            # parent might not have been parsed yet — but in raw_notion it should exist.
            if parent_id not in notion_pages:
                notion_pages[parent_id] = {"files": [], "children": []}
            notion_pages[parent_id]["children"].append(page_id)

        # Cover
        if page.get("cover") is not None:
            cover = list(recursive_search("url", page["cover"]))[0]
            notion_pages[page_id]["cover"] = cover
            notion_pages[page_id]["files"].append(cover)
        else:
            notion_pages[page_id]["cover"] = None

        # Icon (emoji / file / external)
        icon_obj = page.get("icon")
        if isinstance(icon_obj, dict):
            if "emoji" in icon_obj:
                notion_pages[page_id]["emoji"] = icon_obj["emoji"]
                notion_pages[page_id]["icon"] = None
            elif "file" in icon_obj:
                icon_url = icon_obj["file"].get("url")
                notion_pages[page_id]["icon"] = icon_url
                notion_pages[page_id]["emoji"] = None
                if icon_url:
                    notion_pages[page_id]["files"].append(icon_url)
            elif "external" in icon_obj:
                icon_url = icon_obj["external"].get("url")
                notion_pages[page_id]["icon"] = icon_url
                notion_pages[page_id]["emoji"] = None
                if icon_url:
                    notion_pages[page_id]["files"].append(icon_url)
            else:
                notion_pages[page_id]["icon"] = None
                notion_pages[page_id]["emoji"] = None
        else:
            notion_pages[page_id]["icon"] = None
            notion_pages[page_id]["emoji"] = None

    return notion_pages


def find_lists_in_dbs(structured_notion: dict):
    """Treat database as list if any child has no cover."""
    for page_id, page in structured_notion["pages"].items():
        if page["type"] == "database":
            for child_id in page["children"]:
                if structured_notion["pages"][child_id].get("cover") is None:
                    structured_notion["pages"][page_id]["db_list"] = True
                    break


def parse_family_line(page_id: str, family_line: list, structured_notion: dict):
    """Parses the whole parental line for page with 'page_id'."""
    if structured_notion["pages"][page_id]["parent"] is not None:
        par_id = structured_notion["pages"][page_id]["parent"]
        family_line.insert(0, par_id)
        family_line = parse_family_line(par_id, family_line, structured_notion)
    return family_line


def parse_family_lines(structured_notion: dict):
    for page_id, page in structured_notion["pages"].items():
        page["family_line"] = parse_family_line(page_id, [], structured_notion)


def _ensure_posix(rel_path: Path | str) -> str:
    return Path(rel_path).as_posix()


def _unique_url(candidate: str, structured_notion: dict) -> str:
    """
    Делает URL уникальным в пределах structured_notion["urls"].
    Добавляет суффикс _2, _3 ... перед расширением.
    """
    if candidate not in structured_notion["urls"]:
        return candidate

    p = Path(candidate)
    stem = p.stem
    suffix = p.suffix  # ".html" обычно
    parent = p.parent

    i = 2
    while True:
        new_name = f"{stem}_{i}{suffix}"
        new_url = _ensure_posix(parent / new_name) if str(parent) != "." else new_name
        if new_url not in structured_notion["urls"]:
            return new_url
        i += 1


def _is_container_page(page: dict) -> bool:
    """
    Контейнер = то, что имеет детей, или database.
    Для контейнеров делаем папку/<index.html>
    """
    if page.get("type") == "database":
        return True
    return bool(page.get("children"))


def generate_urls(page_id: str, structured_notion: dict, config: dict):
    """
    Генерит ОТНОСИТЕЛЬНЫЕ urls внутри output_dir.
    Вложенность восстанавливаем:
      - контейнеры: <parent_dir>/<slug>/index.html
      - листья:     <parent_dir>/<slug>.html
      - root:       index.html
    """
    root_id = structured_notion["root_page_id"]
    pages = structured_notion["pages"]

    if page_id == root_id:
        url = "index.html"
    else:
        page = pages[page_id]
        parent_id = page.get("parent")
        parent_page = pages.get(parent_id) if parent_id else None

        # Если у родителя нет url — считаем, что родитель это корень
        parent_url = parent_page.get("url") if parent_page else "index.html"
        parent_dir = Path(parent_url).parent  # важно: это URL-логика, не FS

        title = page.get("title")
        slug = clean_url_string(title, fallback=f"untitled_{page_id[:8]}")

        if _is_container_page(page):
            url = _ensure_posix(parent_dir / slug / "index.html")
        else:
            url = _ensure_posix(parent_dir / f"{slug}.html")

    url = _unique_url(url, structured_notion)
    pages[page_id]["url"] = url
    structured_notion["urls"].append(url)

    for child_id in pages[page_id].get("children", []):
        generate_urls(child_id, structured_notion, config)


# ======================
# Properties handlers
# ======================

def p_rich_text(prop: dict) -> str:
    return markdown_parser.richtext_convertor(prop.get("rich_text", []))


def p_number(prop: dict) -> str:
    if prop.get("number") is not None:
        return str(prop["number"])
    return ""


def p_select(prop: dict) -> str:
    if prop.get("select") is not None:
        return str(prop["select"].get("name", ""))
    return ""


def p_multi_select(prop: dict) -> str:
    tags = [t.get("name", "") for t in prop.get("multi_select", []) if t.get("name")]
    return "; ".join(tags)


def p_date(prop: dict) -> str:
    if prop.get("date") is None:
        return ""
    start = prop["date"].get("start")
    end = prop["date"].get("end")
    if not start:
        return ""

    out = dt_parser.isoparse(start).strftime("%d %b, %Y")
    if end:
        out += " - " + dt_parser.isoparse(end).strftime("%d %b, %Y")
    return out


def p_people(prop: dict) -> str:
    names = [p.get("name", "") for p in prop.get("people", []) if p.get("name")]
    return "; ".join(names)


def p_files(prop: dict) -> str:
    links = []
    for f in prop.get("files", []):
        url = _extract_notion_file_url(f)
        if url:
            links.append(f"[📎]({url})")
    return "; ".join(links)


def p_checkbox(prop: dict) -> str:
    return f"- {'[x]' if prop.get('checkbox') else '[ ]'}"


def p_url(prop: dict) -> str:
    if prop.get("url"):
        return f"[🕸]({prop['url']})"
    return ""


def p_email(prop: dict) -> str:
    return prop.get("email") or ""


def p_phone_number(prop: dict) -> str:
    return prop.get("phone_number") or ""


def p_created_time(prop: dict) -> str:
    if prop.get("created_time"):
        return dt_parser.isoparse(prop["created_time"]).strftime("%d %b, %Y")
    return ""


def p_last_edited_time(prop: dict) -> str:
    if prop.get("last_edited_time"):
        return dt_parser.isoparse(prop["last_edited_time"]).strftime("%d %b, %Y")
    return ""


def parse_db_entry_properties(raw_notion: dict, structured_notion: dict):
    properties_map = {
        "rich_text": p_rich_text,
        "number": p_number,
        "select": p_select,
        "multi_select": p_multi_select,
        "date": p_date,
        "people": p_people,
        "files": p_files,
        "checkbox": p_checkbox,
        "url": p_url,
        "email": p_email,
        "phone_number": p_phone_number,
        "created_time": p_created_time,
        "last_edited_time": p_last_edited_time,
    }

    for page_id, page in structured_notion["pages"].items():
        if page["type"] != "db_entry":
            continue

        structured_notion["pages"][page_id]["properties"] = raw_notion[page_id].get("properties", {})
        structured_notion["pages"][page_id]["properties_md"] = {}

        for property_title, prop in structured_notion["pages"][page_id]["properties"].items():
            if prop.get("type") == "title":
                continue  # title already parsed

            structured_notion["pages"][page_id]["properties_md"][property_title] = ""

            ptype = prop.get("type")
            if ptype in properties_map:
                # collect files to download
                if ptype == "files":
                    for f in prop.get("files", []):
                        url = _extract_notion_file_url(f)
                        if url:
                            structured_notion["pages"][page_id]["files"].append(url)

                structured_notion["pages"][page_id]["properties_md"][property_title] = properties_map[ptype](prop)
            else:
                logging.debug(f"{ptype} is not supported yet")


def _unique_file_path(target: Path) -> Path:
    """
    Чтобы файлы не затирали друг друга: pic.png -> pic_2.png -> pic_3.png ...
    """
    if not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    parent = target.parent

    i = 2
    while True:
        cand = parent / f"{stem}_{i}{suffix}"
        if not cand.exists():
            return cand
        i += 1


def download_and_replace_paths(structured_notion: dict, config: dict):
    out_dir = Path(config["output_dir"]).resolve()

    for page_id, page in structured_notion["pages"].items():
        page_url = page.get("url")
        if not page_url:
            continue

        page_folder_rel = Path(page_url).parent  # URL-относительная папка страницы
        page_folder_fs = out_dir / page_folder_rel  # FS-папка на диске

        for i_file, file_url in enumerate(list(page.get("files", []))):
            clean_url = urljoin(file_url, urlparse(file_url).path)
            filename = unquote(Path(clean_url).name)

            # На всякий: чистим имя файла под винду
            filename = clean_url_string(filename, fallback="file")

            # Кладём файл рядом со страницей (как раньше было по логике)
            target_fs = page_folder_fs / filename
            target_fs.parent.mkdir(parents=True, exist_ok=True)
            target_fs = _unique_file_path(target_fs)

            # Относительный URL внутри сайта:
            target_rel_url = _ensure_posix(page_folder_rel / target_fs.name)

            if target_fs.exists():
                logging.debug(f"🤖 {target_fs.name} already exists.")
            else:
                try:
                    request.urlretrieve(file_url, target_fs)
                    logging.debug(f"🤖 Downloaded {target_fs.name}")
                except HTTPError:
                    logging.warning(f"🤖Cannot download {target_fs.name} from link {file_url}.")
                    continue
                except ValueError:
                    continue

            # ✅ structured_data: меняем file_url на ОТНОСИТЕЛЬНЫЙ URL
            structured_notion["pages"][page_id]["files"][i_file] = target_rel_url

            # ✅ markdown: заменяем ссылку на относительную
            md_content = structured_notion["pages"][page_id].get("md_content", "")
            structured_notion["pages"][page_id]["md_content"] = md_content.replace(file_url, target_rel_url)

            # Add short description
            clean_content = strip_html_tags(structured_notion["pages"][page_id].get("md_content", ""))
            structured_notion["pages"][page_id]["description"] = clean_content[:150]

            # ✅ header assets
            for asset in ["icon", "cover"]:
                if page.get(asset) == file_url:
                    structured_notion["pages"][page_id][asset] = target_rel_url

            # ✅ files property in db_entry
            if page.get("type") == "db_entry":
                for prop_name, prop_value in structured_notion["pages"][page_id].get("properties_md", {}).items():
                    if file_url in prop_value:
                        structured_notion["pages"][page_id]["properties_md"][prop_name] = prop_value.replace(
                            file_url, target_rel_url
                        )



def sorting_db_entries(structured_notion: dict):
    for page_id, page in structured_notion["pages"].items():
        if page.get("type") != "database":
            continue

        children = page.get("children", [])
        if len(children) <= 1:
            continue

        # сортируем только если в базе вообще есть хоть одна дата
        has_any_date = any(structured_notion["pages"].get(cid, {}).get("date") for cid in children)
        if not has_any_date:
            continue

        def sort_key(cid: str):
            d = structured_notion["pages"].get(cid, {}).get("date")
            # (0, date) — у кого есть дата, (1, "") — у кого нет -> в конец
            return (0, d) if d else (1, "")

        structured_notion["pages"][page_id]["children"] = sorted(children, key=sort_key)



def sorting_page_by_year(structured_notion: dict):
    structured_notion["sorted_pages"] = {
        k: dt_parser.isoparse(v["date"])
        for k, v in structured_notion["pages"].items()
        if "date" in v.keys() and v.get("date")
    }
    structured_notion["sorted_pages"] = dict(
        sorted(structured_notion["sorted_pages"].items(), key=lambda item: item[1], reverse=True)
    )

    structured_notion["sorted_id_by_year"] = {}
    for year, year_pages in groupby(structured_notion["sorted_pages"].items(), key=lambda item: item[1].year):
        structured_notion["sorted_id_by_year"][year] = [page_id for page_id, _ in year_pages]

    del structured_notion["sorted_pages"]


def create_search_index(structured_notion: dict):
    search_index = []
    for page_id, page in structured_notion["pages"].items():
        if "md_content" in page:
            clean_content = strip_html_tags(page["md_content"])
            search_index.append({"title": page.get("title"), "content": clean_content, "url": page.get("url")})
    structured_notion["search_index"] = search_index


def structurize_notion_content(raw_notion: dict, config: dict) -> dict:
    structured_notion = {"pages": {}, "urls": []}
    structured_notion["root_page_id"] = list(raw_notion.keys())[0]
    structured_notion["pages"] = parse_headers(raw_notion)
    structured_notion["include_footer"] = config["include_footer"]
    structured_notion["include_search"] = config["include_search"]
    structured_notion["build_locally"] = config["build_locally"]

    find_lists_in_dbs(structured_notion)
    logging.debug("🤖 Structurized headers")

    parse_family_lines(structured_notion)
    logging.debug("🤖 Structurized family lines")

    generate_urls(structured_notion["root_page_id"], structured_notion, config)
    logging.debug("🤖 Generated urls")

    markdown_parser.parse_markdown(raw_notion, structured_notion)
    logging.debug("🤖 Parsed markdown content")

    parse_db_entry_properties(raw_notion, structured_notion)
    logging.debug("🤖 Parsed db_entries properties")

    if config["download_files"]:
        download_and_replace_paths(structured_notion, config)
        logging.debug("🤖 Downloaded files and replaced paths")

    sorting_db_entries(structured_notion)
    sorting_page_by_year(structured_notion)
    logging.debug("🤖 Sorted pages by date and grouped by year.")

    if config["include_search"]:
        create_search_index(structured_notion)
        logging.debug("🤖 Created search index.")
    else:
        structured_notion["search_index"] = []

    return structured_notion
