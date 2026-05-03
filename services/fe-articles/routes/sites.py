"""/sites — список источников + toggle/delete."""

from flask import Blueprint, redirect, render_template, request
from sqlalchemy import text

import config
import storage
from logging_setup import get_logger
from routes import get_pagination


logger = get_logger()
bp = Blueprint("sites", __name__)


@bp.route("/sites")
def list_sites():
    search = request.args.get("search", "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", config.DEFAULT_PER_PAGE, type=int)
    if per_page not in config.PER_PAGE_OPTIONS:
        per_page = config.DEFAULT_PER_PAGE

    if config.USE_DB_FOR_RESOURCES and storage.SessionLocal:
        with storage.SessionLocal() as session:
            where = " WHERE site_key LIKE :search" if search else ""
            params = {"search": f"%{search}%"} if search else {}
            total = session.execute(text(f"SELECT COUNT(*) FROM sites{where}"), params).scalar() or 0
            params.update({"limit": per_page, "offset": (page - 1) * per_page})
            rows = session.execute(text(f"""
                SELECT id, site_key, site_url AS url, articles_selector, title_selector,
                       url_selector, active, updated_at
                FROM sites {where}
                ORDER BY
                    CASE WHEN articles_selector IS NULL OR articles_selector = '' OR TRIM(articles_selector) = ''
                         THEN 1 ELSE 0 END,
                    id DESC
                LIMIT :limit OFFSET :offset
            """), params).mappings().fetchall()
            sites = [{**dict(r), "identifier": r["id"]} for r in rows]
            sites_count = total
    else:
        all_sites = storage.get_all_sites()

        def sort_key(s):
            has = bool(s.get("articles_selector") and str(s.get("articles_selector")).strip())
            return (0 if has else 1, -s.get("id", 0))
        all_sites = sorted(all_sites, key=sort_key)

        filtered = [s for s in all_sites if search.lower() in s["site_key"].lower()]
        sites_count = len(filtered)
        pagination = get_pagination(sites_count, page, per_page)
        start = pagination["offset"]
        sites = filtered[start:start + per_page]

    if "pagination" not in dir():
        pagination = get_pagination(sites_count, page, per_page)

    return render_template(
        "sites.html",
        sites=sites, pagination=pagination,
        per_page_options=config.PER_PAGE_OPTIONS,
        sites_count=sites_count, search=search,
        default_per_page=config.DEFAULT_PER_PAGE,
        current_page="sites",
    )


@bp.route("/sites/toggle")
def toggle_site():
    identifier = request.args.get("identifier", "").strip()
    active = request.args.get("active", "true").lower() in ("true", "1", "on", "yes")

    if not identifier:
        logger.error("[TOGGLE] пустой identifier")
        return redirect("/sites")

    if config.USE_DB_FOR_RESOURCES and storage.SessionLocal:
        try:
            site_id = int(identifier)
        except ValueError:
            logger.error(f"[TOGGLE] невалидный identifier: {identifier!r}")
            return redirect("/sites")
        if config.READONLY_DB:
            logger.info(f"[READONLY TOGGLE] site_id={site_id} → active={active}")
            return redirect("/sites")
        try:
            with storage.SessionLocal() as session:
                session.execute(
                    text("UPDATE sites SET active = :a, updated_at = NOW() WHERE id = :id"),
                    {"a": 1 if active else 0, "id": site_id},
                )
                session.commit()
                logger.info(f"[TOGGLE DB] site_id={site_id} → active={active}")
        except Exception as e:
            logger.error(f"[TOGGLE DB] {e}")
    else:
        resources = storage.load_resources()
        for res in resources:
            if str(res.get("name")) == identifier:
                res["active"] = active
                storage.save_resources(resources)
                logger.info(f"[TOGGLE JSON] {identifier!r} → active={active}")
                break

    return redirect("/sites")


@bp.route("/sites/delete", methods=["POST"])
def delete_site():
    data = request.get_json() or {}
    site_id = data.get("id")
    if not site_id:
        return {"error": "no id"}, 400

    if config.USE_DB_FOR_RESOURCES and storage.SessionLocal:
        if config.READONLY_DB:
            logger.info(f"[READONLY DELETE] site_id={site_id}")
            return {"success": True, "readonly": True}
        with storage.SessionLocal() as session:
            session.execute(text("DELETE FROM sites WHERE id = :id"), {"id": site_id})
            session.commit()
    else:
        resources = storage.load_resources()
        if 1 <= site_id <= len(resources):
            resources.pop(site_id - 1)
            storage.save_resources(resources)
    return {"success": True}