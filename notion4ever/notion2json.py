from notion_client import APIResponseError
import notion_client
import json
import logging
from notion4ever.log_context import CURRENT_PAGE

def update_notion_file(filename:str, notion_json:dict):
    """Writes notion_json dictionary to a json file."""
    with open(filename, 'w+', encoding='utf-8') as f:
        json.dump(notion_json, f, ensure_ascii=False, indent=4)

def block_parser(block: dict, notion: "notion_client.client.Client", filename: str = None, notion_json: dict = None)-> dict:
    """Parses block for obtaining all nested blocks

    This function does recursive search over all nested blocks in a given block.

    Args:
        block (dict): Notion block, which is obtained from a list returned by
            function notion.blocks.children.list().
        notion (notion_client.client.Client): Client for python API for
            Notion from https://github.com/ramnes/notion-sdk-py is used here.

    Returns:
        block (dict): Notion block, which contains additional "children" key,
            which is a list of nested blocks of a given block.
    """

    if block["has_children"]:
        block["children"] = []
        start_cursor = None
        while True:
            if start_cursor is None:
                blocks = notion.blocks.children.list(block["id"])
            start_cursor = blocks["next_cursor"]
            block["children"].extend(blocks['results'])
            if start_cursor is None:
                break

        for child_block in block["children"]:
            # If nested block is a child_page/child_database, fetch it as a page
            if child_block.get("type") in ['child_page', 'child_database'] and filename and notion_json:
                notion_page_parser(child_block['id'], notion, filename, notion_json)
            else:
                block_parser(child_block, notion, filename, notion_json)
    return block

def notion_page_parser(
        page_id: str,
        notion: "notion_client.client.Client",
        filename: str,
        notion_json: dict,
):
    """Parses notion page with all its nested content and subpages.

    Recursive search over all nested subpages and databases.
    Saves results incrementally into 'notion_json' and into file 'filename'.
    """

    token = None
    try:
        # ---- Retrieve metadata: page or database ----
        try:
            page = notion.pages.retrieve(page_id)
            page_type = "page"
        except APIResponseError:
            page = notion.databases.retrieve(page_id)
            page_type = "database"

        # ---- Set CURRENT_PAGE context for logging ----
        title = None

        if page_type == "page":
            title_prop = page.get("properties", {}).get("title", {})
            title_items = title_prop.get("title", [])
            if title_items:
                title = "".join(t.get("plain_text", "") for t in title_items).strip()

        else:  # database
            # У баз данных обычно имя лежит в 'title' на верхнем уровне
            title_items = page.get("title", [])
            if title_items:
                title = "".join(t.get("plain_text", "") for t in title_items).strip()

        token = CURRENT_PAGE.set(title or f"untitled_{page_id[:8]}")

        # ---- Save retrieved object ----
        notion_json[page["id"]] = page
        logging.debug(f"🤖 Retrieved {page['id']} of type {page_type}.")
        update_notion_file(filename, notion_json)

        start_cursor = None
        notion_json[page["id"]]["blocks"] = []

        # ---- Fetch children/entries ----
        while True:
            if page_type == "page":
                if start_cursor is None:
                    blocks = notion.blocks.children.list(page_id)
                else:
                    blocks = notion.blocks.children.list(page_id, start_cursor=start_cursor)
            else:  # database
                if start_cursor is None:
                    blocks = notion.databases.query(page_id)
                else:
                    blocks = notion.databases.query(page_id, start_cursor=start_cursor)

            start_cursor = blocks.get("next_cursor")
            notion_json[page["id"]]["blocks"].extend(blocks.get("results", []))
            update_notion_file(filename, notion_json)

            if start_cursor is None:
                break

        logging.debug(f"🤖 Parsed content of {page['id']}.")

        # ---- Parse blocks recursively ----
        for i_block, block in enumerate(notion_json[page["id"]]["blocks"]):
            if page_type == "page":
                if block.get("type") in ["page", "child_page", "child_database"]:
                    notion_page_parser(block["id"], notion, filename, notion_json)
                else:
                    parsed = block_parser(block, notion, filename, notion_json)
                    notion_json[page["id"]]["blocks"][i_block] = parsed
                    update_notion_file(filename, notion_json)

            else:  # database
                # В выдаче query элементы — страницы (db entries)
                block["type"] = "db_entry"
                notion_json[page["id"]]["blocks"][i_block] = block
                update_notion_file(filename, notion_json)

                # object у query-элемента обычно "page"
                if block.get("object") in ["page", "child_page", "child_database"]:
                    notion_page_parser(block["id"], notion, filename, notion_json)

    finally:
        # Сбрасываем контекст текущей страницы даже если упали/прервали
        if token is not None:
            CURRENT_PAGE.reset(token)

