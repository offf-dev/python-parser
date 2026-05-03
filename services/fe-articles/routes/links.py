"""/links — список спарсенных + toggle is_send + delete."""

from flask import Blueprint, redirect, render_template, request
from sqlalchemy import text

import config
import storage
from logging_setup import get_logger
from routes import get_pagination


logger = get_logger()
bp = Blueprint("links", __name__)


@bp.route("/links")
def list_links():
    search_title = request.args.get("search_title", "").strip()
    search_site = request.args.get("search_site", "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", config.DEFAULT_PER_PAGE, type=int)
    if per_page not in config.PER_PAGE_OPTIONS:
        per_page = config.DEFAULT_PER_PAGE

    if config.USE_DB_FOR_ARTICLES and storage.SessionLocal:
        with storage.SessionLocal() as session:
            where = ""
            params = {}
            if search_title or search_site:
                clauses = []
                if search_title:
                    clauses.append("l.title LIKE :st")
                    params["st"] = f"%{search_title}%"
                if search_site:
                    clauses.append("COALESCE(d.name, '') LIKE :ss")
                    params["ss"] = f"%{search_site}%"
                where = " WHERE " + " AND ".join(clauses)

            total = session.execute(text(
                f"SELECT COUNT(*) FROM links l LEFT JOIN domains d ON l.domain_id = d.id{where}"
            ), params).scalar() or 0
            params.update({"limit": per_page, "offset": (page - 1) * per_page})
            rows = session.execute(text(f"""
                SELECT l.id, l.title, l.url, l.is_send, l.created_at,
                       COALESCE(d.name, '—') as site_name
                FROM links l LEFT JOIN domains d ON l.domain_id = d.id
                {where}
                ORDER BY l.id DESC
                LIMIT :limit OFFSET :offset
            """), params).mappings().fetchall()
            links = [dict(r) for r in rows]
            links_count = total
    else:
        all_links = storage.get_all_links()
        all_links.sort(key=lambda l: l["id"], reverse=True)
        filtered = [
            l for l in all_links
            if search_title.lower() in l["title"].lower()
            and search_site.lower() in (l["site_name"] or "").lower()
        ]
        links_count = len(filtered)
        pagination = get_pagination(links_count, page, per_page)
        start = pagination["offset"]
        links = filtered[start:start + per_page]

    if "pagination" not in dir():
        pagination = get_pagination(links_count, page, per_page)

    return render_template(
        "links.html",
        links=links, pagination=pagination,
        per_page_options=config.PER_PAGE_OPTIONS,
        links_count=links_count,
        search_title=search_title, search_site=search_site,
        default_per_page=config.DEFAULT_PER_PAGE,
        current_page="links",
    )


@bp.route("/links/toggle")
def toggle_link():
    url = request.args.get("url")
    is_send = request.args.get("is_send", "true").lower() in ("true", "1", "on")
    if not url:
        return "No url", 400

    if config.USE_DB_FOR_ARTICLES and storage.SessionLocal:
        if config.READONLY_DB:
            logger.info(f"[READONLY TOGGLE LINK] url={url} → is_send={is_send}")
            return redirect("/links")
        try:
            with storage.SessionLocal() as session:
                session.execute(text("""
                    UPDATE links SET is_send = :s, updated_at = NOW() WHERE url = :u
                """), {"s": 1 if is_send else 0, "u": url})
                session.commit()
        except Exception as e:
            logger.error(f"[LINKS TOGGLE DB] {e}")
            return f"Error: {e}", 500
    else:
        last = storage.load_last_results()
        for site_articles in last.values():
            for art in site_articles:
                if art.get("url") == url:
                    art["is_send"] = is_send
                    storage.save_last_results(last)
                    return redirect("/links")
        return "Article not found", 404
    return redirect("/links")


@bp.route("/links/delete", methods=["POST"])
def delete_link():
    data = request.get_json() or {}
    site_name = data.get("site_name")
    url = data.get("url")
    if not (site_name and url):
        return {"error": "no params"}, 400

    if config.USE_DB_FOR_ARTICLES and storage.SessionLocal:
        if config.READONLY_DB:
            logger.info(f"[READONLY DELETE LINK] url={url}")
            return {"success": True, "readonly": True}
        with storage.SessionLocal() as session:
            session.execute(text("DELETE FROM links WHERE url = :u"), {"u": url})
            session.commit()
    else:
        last = storage.load_last_results()
        if site_name in last:
            last[site_name] = [a for a in last[site_name] if a["url"] != url]
            storage.save_last_results(last)
    return {"success": True}