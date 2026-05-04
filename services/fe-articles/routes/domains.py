"""/domains — таблица доменов с эмодзи и числом статей."""

from flask import Blueprint, render_template, request

import storage
from logging_setup import get_logger


logger = get_logger()
bp = Blueprint("domains", __name__)


@bp.route("/domains")
def list_domains():
    domains = storage.get_all_domains()
    return render_template(
        "domains.html",
        domains=domains,
        domains_count=len(domains),
        current_page="domains",
    )


@bp.route("/domains/<int:domain_id>/emoji", methods=["POST"])
def update_emoji(domain_id):
    data = request.get_json() or {}
    emoji = (data.get("emoji") or "").strip()
    if not emoji:
        return {"error": "empty emoji"}, 400
    if storage.update_domain_emoji(domain_id, emoji):
        return {"success": True, "emoji": emoji}
    return {"error": "update failed"}, 500