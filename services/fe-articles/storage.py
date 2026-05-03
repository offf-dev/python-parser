"""Хранилище: JSON-файлы и MySQL. Dispatcher по флагам USE_DB_FOR_*."""

import json
import os
import re
import threading
import traceback
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

import config
from logging_setup import get_logger


logger = get_logger()
_file_lock = threading.Lock()


# ====================== БД engine ======================
def _make_engine():
    if not all([config.DB_USERNAME, config.DB_PASSWORD, config.DB_DATABASE, config.DB_HOST]):
        logger.warning("Не все параметры БД указаны")
        return None

    cs = f"mysql+pymysql://{config.DB_USERNAME}:{config.DB_PASSWORD}@{config.DB_HOST}:{config.DB_PORT}/{config.DB_DATABASE}"
    connect_args = {"charset": "utf8mb4"}
    if config.DB_USE_SSL:
        connect_args["ssl"] = {"ssl_verify_cert": False, "ssl_verify_identity": False}
    else:
        connect_args["ssl_disabled"] = True

    return create_engine(
        cs, pool_pre_ping=True, pool_recycle=3600, pool_size=10,
        max_overflow=20, echo=False, connect_args=connect_args,
    )


engine = _make_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine) if engine else None

if engine is None and (config.USE_DB_FOR_RESOURCES or config.USE_DB_FOR_ARTICLES):
    logger.warning("Движок БД не создан — переключаемся на JSON")
    config.USE_DB_FOR_RESOURCES = False
    config.USE_DB_FOR_ARTICLES = False


def prune_old_links() -> int:
    """Удаляет отправленные (is_send=1) ссылки старше LINKS_RETENTION_DAYS дней.
    Неотправленные оставляем — это активная очередь trickle-отправки.
    Возвращает число удалённых строк.
    """
    if not SessionLocal or config.READONLY_DB:
        return 0
    days = config.LINKS_RETENTION_DAYS
    if days <= 0:
        return 0
    try:
        with SessionLocal() as session:
            # MySQL не принимает параметр для INTERVAL — подставляем число явно (после int-каста безопасно)
            result = session.execute(text(
                f"DELETE FROM links WHERE is_send = 1 AND created_at < NOW() - INTERVAL {int(days)} DAY"
            ))
            session.commit()
            return result.rowcount or 0
    except Exception as e:
        logger.error(f"prune_old_links failed: {e}")
        return 0


def ensure_schema():
    """Idempotent миграции схемы. Безопасно вызывается на каждом старте."""
    if not SessionLocal or config.READONLY_DB:
        return
    try:
        with SessionLocal() as session:
            exists = session.execute(text("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'sites'
                  AND COLUMN_NAME = 'emoji'
            """)).scalar()
            if not exists:
                logger.info("Migrating: ALTER TABLE sites ADD COLUMN emoji")
                session.execute(text(
                    "ALTER TABLE sites ADD COLUMN emoji VARCHAR(10) NULL"
                ))
                session.commit()
    except Exception as e:
        logger.error(f"ensure_schema failed: {e}")


# ====================== Утилиты ======================
def extract_domain(url: str) -> str:
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    return url.split("/")[0]


def find_or_create_domain(name: str):
    if not SessionLocal:
        return None
    try:
        with SessionLocal() as session:
            domain_id = session.execute(
                text("SELECT id FROM domains WHERE name = :name"), {"name": name}
            ).scalar()
            if not domain_id:
                if config.READONLY_DB:
                    logger.info(f"[READONLY] would CREATE domain: {name}")
                    return None
                logger.info(f"Создание нового домена: {name}")
                session.execute(text(
                    "INSERT INTO domains (name, created_at, updated_at) VALUES (:name, NOW(), NOW())"
                ), {"name": name})
                session.commit()
                domain_id = session.execute(text("SELECT LAST_INSERT_ID()")).scalar()
            return domain_id
    except Exception as e:
        logger.error(f"Ошибка find_or_create_domain для {name}: {e}\n{traceback.format_exc()}")
        return None


# ====================== Resources (JSON или sites table) ======================
def load_resources():
    return _load_resources_db() if config.USE_DB_FOR_RESOURCES else _load_resources_json()


def _load_resources_json():
    with _file_lock:
        try:
            if os.path.exists(config.DATA_FILE):
                with open(config.DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for res in data:
                        if "active" not in res:
                            res["active"] = not res.get("paused", False)
                        if "paused" in res:
                            del res["paused"]
                    logger.info(f"Загружено {len(data)} ресурсов из JSON")
                    return data
        except Exception as e:
            logger.error(f"Ошибка загрузки JSON ресурсов: {e}")
    return []


def _load_resources_db():
    if not SessionLocal:
        logger.error("БД не настроена")
        return []
    try:
        with SessionLocal() as session:
            rows = session.execute(text("""
                SELECT id, site_key, site_url AS url, articles_selector, title_selector,
                       url_selector, active, emoji
                FROM sites
                WHERE active = 1
            """)).fetchall()
            return [{
                "name": r.site_key, "url": r.url,
                "item_selector": r.articles_selector,
                "title_selector": r.title_selector,
                "link_selector": r.url_selector,
                "active": bool(r.active),
                "emoji": r.emoji,
            } for r in rows]
    except Exception as e:
        logger.error(f"Ошибка загрузки ресурсов из БД: {e}")
        return []


def save_resources(resources):
    if config.USE_DB_FOR_RESOURCES:
        _save_resources_db(resources)
    else:
        _save_resources_json(resources)


def _save_resources_json(resources):
    with _file_lock:
        try:
            os.makedirs("data", exist_ok=True)
            with open(config.DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(resources, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения ресурсов в JSON: {e}")


def _save_resources_db(resources):
    if not SessionLocal:
        return
    if config.READONLY_DB:
        logger.info(f"[READONLY] would UPDATE active for {len(resources)} sites")
        return
    try:
        with SessionLocal() as session:
            for res in resources:
                session.execute(text("""
                    UPDATE sites SET active = :active, updated_at = NOW()
                    WHERE site_key = :site_key
                """), {"active": res.get("active", False), "site_key": res.get("name")})
            session.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения ресурсов в БД: {e}")


# ====================== Last-results JSON ======================
def load_last_results():
    with _file_lock:
        try:
            if os.path.exists(config.LAST_RESULTS_FILE):
                with open(config.LAST_RESULTS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки last_results: {e}")
    return {}


def save_last_results(results):
    with _file_lock:
        try:
            os.makedirs("data", exist_ok=True)
            with open(config.LAST_RESULTS_FILE, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения last_results: {e}")


# ====================== Blocked keywords ======================
BLOCKED_KEYWORDS_FILE = "data/blocked_keywords.json"


def load_blocked_keywords() -> list:
    """Возвращает список строк-стоп-слов. Каждая запись может содержать
    запятую — разделитель в одной записи (см. docs/parser.md §3, §4.2).
    """
    if config.USE_DB_FOR_RESOURCES and SessionLocal:
        try:
            with SessionLocal() as session:
                rows = session.execute(text("SELECT keyword FROM blocked_keywords")).fetchall()
                return [r[0] for r in rows if r[0]]
        except Exception as e:
            logger.error(f"Ошибка загрузки blocked_keywords из БД: {e}")
            return []
    try:
        if os.path.exists(BLOCKED_KEYWORDS_FILE):
            with open(BLOCKED_KEYWORDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [str(x) for x in data if x]
    except Exception as e:
        logger.error(f"Ошибка загрузки blocked_keywords из JSON: {e}")
    return []


# ====================== Известные URL ======================
def get_known_urls(resource_name: str = None):
    if config.USE_DB_FOR_ARTICLES:
        return _known_urls_db()
    return _known_urls_json(resource_name)


def _known_urls_json(resource_name: str):
    last = load_last_results()
    return {a["url"] for a in last.get(resource_name, [])}


def _known_urls_db():
    if not SessionLocal:
        return set()
    try:
        with SessionLocal() as session:
            rows = session.execute(text("SELECT url FROM links")).mappings().fetchall()
            return {r["url"] for r in rows}
    except Exception as e:
        logger.error(f"Ошибка known_urls из БД: {e}")
        return set()


# ====================== Сохранение новых статей ======================
def save_new_articles(resource_name: str, new_articles: list):
    if not new_articles:
        return
    if config.USE_DB_FOR_ARTICLES:
        _save_new_articles_db(resource_name, new_articles)
    else:
        _save_new_articles_json(resource_name, new_articles)


def _save_new_articles_json(resource_name: str, new_articles: list):
    last = load_last_results()
    last.setdefault(resource_name, [])
    enriched = [{**a, "parsed_at": datetime.now().isoformat(), "is_send": False} for a in new_articles]
    last[resource_name].extend(enriched)
    save_last_results(last)


def _save_new_articles_db(resource_name: str, new_articles: list):
    if not SessionLocal or not new_articles:
        return
    if config.READONLY_DB:
        logger.info(f"[READONLY] would INSERT {len(new_articles)} links for {resource_name}")
        return
    try:
        added = 0
        with SessionLocal() as session:
            site_id = session.execute(
                text("SELECT id FROM sites WHERE site_key = :k"), {"k": resource_name}
            ).scalar()
            if not site_id:
                logger.error(f"Не найден site_id для {resource_name}")
                return
            for art in new_articles:
                exists = session.execute(
                    text("SELECT 1 FROM links WHERE url = :u"), {"u": art["url"]}
                ).scalar()
                if exists:
                    continue
                domain_id = find_or_create_domain(extract_domain(art["url"]))
                if not domain_id:
                    continue
                session.execute(text("""
                    INSERT INTO links (domain_id, site_id, title, description, url, is_send, created_at, updated_at)
                    VALUES (:domain_id, :site_id, :title, :description, :url, false, NOW(), NOW())
                """), {
                    "domain_id": domain_id, "site_id": site_id,
                    "title": art["title"],
                    "description": f"<a href='{art['url']}'>{art['title']}</a>",
                    "url": art["url"],
                })
                added += 1
            session.commit()
            logger.info(f"Сохранено в БД: {added}/{len(new_articles)} для {resource_name}")
    except SQLAlchemyError as e:
        logger.error(f"SQLAlchemy ошибка: {e}\n{traceback.format_exc()}")
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}\n{traceback.format_exc()}")


# ====================== Чтение для UI ======================
def get_all_sites():
    if config.USE_DB_FOR_RESOURCES:
        if not SessionLocal:
            return []
        try:
            with SessionLocal() as session:
                rows = session.execute(text("""
                    SELECT id, site_key, site_url AS url, articles_selector, title_selector,
                           url_selector, active, emoji
                    FROM sites
                    ORDER BY CASE WHEN articles_selector IS NULL THEN 1 ELSE 0 END, updated_at DESC
                """)).fetchall()
                return [{
                    "id": r.id, "identifier": r.id,
                    "site_key": r.site_key, "url": r.url,
                    "articles_selector": r.articles_selector,
                    "title_selector": r.title_selector,
                    "url_selector": r.url_selector,
                    "active": bool(r.active),
                    "emoji": r.emoji,
                } for r in rows]
        except Exception as e:
            logger.error(f"Ошибка get_all_sites БД: {e}")
            return []
    resources = _load_resources_json()
    return [{
        "id": i + 1, "identifier": r.get("name"),
        "site_key": r.get("name", ""), "url": r.get("url", ""),
        "articles_selector": r.get("item_selector", ""),
        "title_selector": r.get("title_selector", ""),
        "url_selector": r.get("link_selector", ""),
        "active": r.get("active", False),
        "emoji": r.get("emoji"),
    } for i, r in enumerate(resources)]


def get_all_links():
    if config.USE_DB_FOR_ARTICLES:
        if not SessionLocal:
            return []
        try:
            with SessionLocal() as session:
                rows = session.execute(text("""
                    SELECT l.id, l.title, l.url, l.is_send, l.created_at,
                           COALESCE(d.name, '—') as site_name
                    FROM links l LEFT JOIN domains d ON l.domain_id = d.id
                    ORDER BY l.id DESC
                """)).mappings().fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"Ошибка get_all_links БД: {e}\n{traceback.format_exc()}")
            return []

    last = load_last_results()
    out = []
    lid = 1
    for site_name, articles in last.items():
        for art in articles:
            parsed_at = art.get("parsed_at")
            out.append({
                "id": lid,
                "title": art.get("title", ""),
                "url": art.get("url", ""),
                "is_send": art.get("is_send", False),
                "created_at": datetime.fromisoformat(parsed_at) if parsed_at else None,
                "site_name": site_name,
            })
            lid += 1
    return out