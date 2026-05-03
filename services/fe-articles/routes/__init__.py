"""Регистрация blueprints + общие helpers."""

from math import ceil

import config


def get_pagination(total_items: int, page: int, per_page: int) -> dict:
    total_pages = ceil(total_items / per_page) if per_page else 0
    page = max(1, min(page, total_pages)) if total_pages else 1
    return {
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total_items": total_items,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "offset": (page - 1) * per_page,
    }


def register_blueprints(app):
    from routes.main import bp as main_bp
    from routes.sites import bp as sites_bp
    from routes.links import bp as links_bp
    from routes.debug import bp as debug_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(sites_bp)
    app.register_blueprint(links_bp)
    app.register_blueprint(debug_bp)

    @app.context_processor
    def inject_modes():
        return {
            "resources_mode": "DB" if config.USE_DB_FOR_RESOURCES else "JSON",
            "articles_mode": "DB" if config.USE_DB_FOR_ARTICLES else "JSON",
        }