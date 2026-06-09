"""/, /parse_now, /save_parsed — главная форма + ручной парсинг + ручная заливка в БД."""

import asyncio

import pandas as pd
from flask import Blueprint, render_template, request
from sqlalchemy import text

import config
import parser
import storage
from logging_setup import get_logger


logger = get_logger()
bp = Blueprint("main", __name__)


def _counts():
    if config.USE_DB_FOR_RESOURCES and storage.SessionLocal:
        with storage.SessionLocal() as s:
            sites_count = s.execute(text("SELECT COUNT(*) FROM sites")).scalar() or 0
    else:
        sites_count = len(storage.load_resources())

    if config.USE_DB_FOR_ARTICLES and storage.SessionLocal:
        with storage.SessionLocal() as s:
            links_count = s.execute(text("SELECT COUNT(*) FROM links")).scalar() or 0
    else:
        last = storage.load_last_results()
        links_count = sum(len(a) for a in last.values())
    return sites_count, links_count


@bp.route("/", methods=["GET", "POST"])
def index():
    logger.info("Запрос к /")

    edit_index = request.args.get("edit", type=int)
    parse_now = request.args.get("parse_now", type=int)

    resource = {}
    error = success = table = count = None
    parsed_articles = None
    parsed_resource_name = None
    sites_count, links_count = _counts()

    link_id = None
    if edit_index is not None or parse_now is not None:
        target_id = edit_index if edit_index is not None else parse_now
        all_sites = storage.get_all_sites()
        target = next((s for s in all_sites if s["id"] == target_id), None)
        if target:
            link_id = target["id"]
            resource = {
                "name": target["site_key"],
                "url": target["url"],
                "item_selector": target.get("articles_selector", ""),
                "title_selector": target.get("title_selector", ""),
                "link_selector": target.get("url_selector", ""),
                "active": target.get("active", False),
            }
            if parse_now is not None:
                try:
                    data, perr = asyncio.run(parser.parse_resource(resource))
                    if perr:
                        error = f"Ошибка парсинга: {perr}"
                    elif not data:
                        error = "Ничего не найдено по указанным селекторам"
                    else:
                        df = pd.DataFrame([
                            {"Заголовок": a["title"], "Ссылка": f"<a href='{a['url']}' target='_blank' rel='noopener'>{a['url']}</a>"}
                            for a in data
                        ])
                        table = df.to_html(escape=False, index=False)
                        count = len(data)
                        success = f"Успешно спаршено {count} статей с {resource['name']}!"
                        parsed_articles = [{"title": a["title"], "url": a["url"]} for a in data]
                        parsed_resource_name = resource["name"]
                except Exception as e:
                    error = f"Ошибка парсинга: {e}"

    if request.method == "POST":
        action = request.form.get("action")
        link_id_form = request.form.get("link_id", type=int)

        current = {
            "name": request.form["name"].strip(),
            "url": request.form["url"].strip(),
            "item_selector": request.form["item_selector"].strip(),
            "title_selector": request.form["title_selector"].strip(),
            "link_selector": request.form["link_selector"].strip(),
            "active": resource.get("active", False),
        }

        if action == "save":
            if config.READONLY_DB and config.USE_DB_FOR_RESOURCES:
                success = f"[READONLY] would save: {current['name']}"
                resource = current
            elif config.USE_DB_FOR_RESOURCES and storage.SessionLocal:
                with storage.SessionLocal() as session:
                    existing = session.execute(text("""
                        SELECT id FROM sites
                        WHERE (site_key = :site_key OR site_url = :site_url) AND id != :id
                    """), {
                        "site_key": current["name"], "site_url": current["url"],
                        "id": link_id_form or 0,
                    }).scalar()
                    if existing:
                        error = "Сайт с таким site_key или site_url уже существует"
                    else:
                        domain_id = storage.find_or_create_domain(storage.extract_domain(current["url"]))
                        if not domain_id:
                            error = "Ошибка создания домена"
                        elif link_id_form:
                            session.execute(text("""
                                UPDATE sites SET
                                    site_key = :site_key, site_url = :site_url,
                                    articles_selector = :articles_selector,
                                    title_selector = :title_selector,
                                    url_selector = :url_selector,
                                    domain_id = :domain_id,
                                    updated_at = NOW()
                                WHERE id = :id
                            """), {
                                "site_key": current["name"], "site_url": current["url"],
                                "articles_selector": current["item_selector"],
                                "title_selector": current["title_selector"],
                                "url_selector": current["link_selector"],
                                "domain_id": domain_id, "id": link_id_form,
                            })
                            success = f"Сайт обновлён: {current['name']}"
                        else:
                            session.execute(text("""
                                INSERT INTO sites
                                (site_key, site_url, articles_selector, title_selector, url_selector,
                                 domain_id, active, created_at, updated_at)
                                VALUES (:site_key, :site_url, :articles_selector, :title_selector,
                                        :url_selector, :domain_id, 0, NOW(), NOW())
                            """), {
                                "site_key": current["name"], "site_url": current["url"],
                                "articles_selector": current["item_selector"],
                                "title_selector": current["title_selector"],
                                "url_selector": current["link_selector"],
                                "domain_id": domain_id,
                            })
                            success = f"Сайт добавлен: {current['name']}"
                        session.commit()
            else:
                # JSON-режим
                resources_list = storage.load_resources()
                if edit_index is not None and 1 <= edit_index <= len(resources_list):
                    resources_list[edit_index - 1] = current
                    storage.save_resources(resources_list)
                    success = f"Обновлён: {current['name']}"
                else:
                    resources_list.append(current)
                    storage.save_resources(resources_list)
                    success = f"Добавлен новый ресурс: {current['name']}"
                    edit_index = len(resources_list)

            if success:
                resource = current
                sites_count, links_count = _counts()

    return render_template(
        "index.html",
        resource=resource, edit_index=edit_index, link_id=link_id,
        error=error, success=success, table=table, count=count,
        sites_count=sites_count, links_count=links_count,
        parsed_articles=parsed_articles,
        parsed_resource_name=parsed_resource_name,
        readonly_db=bool(config.READONLY_DB),
        current_page="main",
    )


@bp.route("/parse_now", methods=["POST"])
async def parse_now():
    try:
        link_id = request.form.get("link_id", type=int)
        resource = None
        if link_id:
            target = next((s for s in storage.get_all_sites() if s["id"] == link_id), None)
            if target:
                resource = {
                    "name": target["site_key"], "url": target["url"],
                    "item_selector": target.get("articles_selector", ""),
                    "title_selector": target.get("title_selector", ""),
                    "link_selector": target.get("url_selector", ""),
                }
        if not resource:
            resource = {
                "name": request.form["name"].strip(),
                "url": request.form["url"].strip(),
                "item_selector": request.form["item_selector"].strip(),
                "title_selector": request.form["title_selector"].strip(),
                "link_selector": request.form["link_selector"].strip(),
            }

        async with parser.parse_lock:
            data, perr = await parser.parse_resource(resource)

        if perr:
            return {"success": False, "error": perr}
        if not data:
            return {"success": False, "error": "Ничего не найдено по указанным селекторам"}

        df = pd.DataFrame([
            {"Заголовок": a["title"], "Ссылка": f"<a href='{a['url']}' target='_blank' rel='noopener'>{a['url']}</a>"}
            for a in data
        ])
        return {
            "success": True, "count": len(data),
            "table": df.to_html(escape=False, index=False),
            "resource_name": resource.get("name"),
            "articles": [{"title": a["title"], "url": a["url"]} for a in data],
            "readonly": bool(config.READONLY_DB),
            "db_articles": bool(config.USE_DB_FOR_ARTICLES),
        }
    except Exception as e:
        logger.error(f"/parse_now: {e}")
        return {"success": False, "error": str(e)}


@bp.route("/save_parsed", methods=["POST"])
def save_parsed():
    """Ручная заливка уже спаршенных статей в БД. Дедуп по links.url встроен
    в storage.save_new_articles_db. Доступно только в local-write режиме
    (READONLY_DB=false), иначе вернёт ошибку.
    """
    try:
        payload = request.get_json(silent=True) or {}
        resource_name = (payload.get("resource_name") or "").strip()
        articles = payload.get("articles") or []

        if not resource_name:
            return {"success": False, "error": "Не указан resource_name"}
        if not isinstance(articles, list) or not articles:
            return {"success": False, "error": "Пустой список статей"}

        if config.READONLY_DB:
            return {"success": False, "error": "БД в READONLY-режиме — запусти write-compose"}
        if not config.USE_DB_FOR_ARTICLES or not storage.SessionLocal:
            return {"success": False, "error": "БД для статей не подключена"}

        clean = []
        for a in articles:
            url = (a.get("url") or "").strip()
            title = (a.get("title") or "").strip()
            if url and title:
                clean.append({"url": url, "title": title})
        if not clean:
            return {"success": False, "error": "Нет валидных {title,url} в payload"}

        known = storage.get_known_urls()
        new_items = [a for a in clean if a["url"] not in known]
        skipped_dup = len(clean) - len(new_items)

        if not new_items:
            return {"success": True, "saved": 0, "skipped_dup": skipped_dup,
                    "message": f"Все {skipped_dup} статей уже в БД"}

        links_before = 0
        links_after = 0
        with storage.SessionLocal() as s:
            links_before = s.execute(text("SELECT COUNT(*) FROM links")).scalar() or 0

        storage.save_new_articles(resource_name, new_items)

        with storage.SessionLocal() as s:
            links_after = s.execute(text("SELECT COUNT(*) FROM links")).scalar() or 0

        saved = max(0, links_after - links_before)
        logger.info(
            f"/save_parsed: {resource_name} → submitted={len(clean)} "
            f"skipped_dup={skipped_dup} saved={saved}"
        )
        return {
            "success": True, "saved": saved,
            "skipped_dup": skipped_dup, "submitted": len(clean),
            "resource_name": resource_name,
        }
    except Exception as e:
        logger.error(f"/save_parsed: {e}")
        return {"success": False, "error": str(e)}